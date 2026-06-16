"""v0.32.0 Phase 1.4: hard-fail recovery tiers for the architect retry loop.

After ``_MAX_ARCHITECT_ATTEMPTS`` (3) attempts fail in
:func:`orchestrator.plan_phase.run_plan_phase`, the legacy contract was
to re-raise the most recent exception immediately. v0.32.0 inserts
four recovery tiers between the third failure and the hard-fail:

* **Tier 4 — scope degradation:** if multiple errors share a directory
  prefix, drop the highest-failure-count scope entry from the plan
  spec and re-prompt the architect with the narrower spec. Implemented
  by :func:`attempt_scope_degradation`.

* **Tier 5 — model escalation:** if still failing AND the configured
  architect model is sonnet, escalate to opus. Implemented by
  :func:`should_escalate_model`.

* **Tier 6 — user escalation:** emit a structured ``RecoveryHint``
  placeholder so the CLI can surface "architect cannot converge — see
  ``autodev status --blocked``". The full ``RecoveryHint`` schema
  lands in Phase 5; for now we attach it as a free-form ``meta``
  dict on the raised exception.

* **Tier 7 — hard-fail with forensic summary:** abort the plan phase;
  reference the archived ``architect-failed-*.md`` dumps so the
  operator has the exact rejected markdown for offline inspection.

The tiers are intentionally **best-effort**: each helper returns
``None`` (or ``False``) when it cannot produce an action, which lets
the caller fall through to the next tier without exception plumbing.
The forensic-summary helper always succeeds (it's the terminal tier).

This module deliberately avoids importing :mod:`orchestrator.plan_phase`
at module load to keep the dependency graph one-way: ``plan_phase``
imports recovery, never vice versa.

Tests: ``tests/test_plan_phase_recovery.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from state.schemas import Plan


# Models that we treat as "weaker" — i.e. eligible for an opus bump on
# Tier 5 model escalation. The check is substring-based so model
# identifiers like ``claude-sonnet-4-20250514`` register as sonnet
# without having to track every per-version suffix.
_SONNET_MODEL_TOKENS: tuple[str, ...] = ("sonnet",)
# Target opus model identifier set by Tier 5. Concrete identifier left
# unspecified here; the caller passes the resolved value (the
# orchestrator config knows what the install's opus pin is).
_OPUS_MODEL_TOKENS: tuple[str, ...] = ("opus",)


@dataclass(frozen=True)
class ScopeDegradationResult:
    """Outcome of an attempted scope-degradation pass.

    ``new_plan`` is the degraded plan to re-prompt the architect with;
    ``dropped_scope_entry`` is the path that was removed (used in the
    ledger op + the architect's retry envelope so the model knows what
    the orchestrator narrowed). ``None`` for both fields when no
    degradation was possible (caller falls through to Tier 5).
    """

    new_plan: "Plan | None" = None
    dropped_scope_entry: str | None = None
    reason: str = ""

    @property
    def did_degrade(self) -> bool:
        return self.new_plan is not None


# v0.32.0 Phase 5 (Gap G): the placeholder ``RecoveryHintStub`` that
# Phase 1.4 used as a forward reference has been replaced by the real
# :class:`state.schemas.RecoveryHint` pydantic model. The alias below
# preserves the import surface for any v0.31.x callers that imported
# ``RecoveryHintStub`` directly — they now get the structured model
# without the indirection. New callers should import ``RecoveryHint``
# from :mod:`state.schemas` directly.
def _get_recovery_hint_class() -> type:
    """Lazy import to avoid a circular import at module load time
    (``state.schemas`` does not import this module, but defer-by-default
    keeps the dependency graph one-way)."""
    from state.schemas import RecoveryHint  # noqa: PLC0415

    return RecoveryHint


def __getattr__(name: str):
    """Module-level ``__getattr__`` so ``from ... import RecoveryHintStub``
    keeps resolving to the real :class:`state.schemas.RecoveryHint`.

    PEP 562: this fires only when the regular attribute lookup misses,
    so DO NOT bind ``RecoveryHintStub`` at module scope — that would
    short-circuit the lookup and return ``None`` instead of the real
    class. The ``__all__`` re-export below is fine because ``__all__``
    is consulted only by ``from module import *`` (a code path the
    test suite never takes), not by ``from module import RecoveryHintStub``.
    """
    if name == "RecoveryHintStub":
        return _get_recovery_hint_class()
    raise AttributeError(name)


def attempt_scope_degradation(
    plan: "Plan",
    errors_seen: dict[tuple[str, str], int],
) -> ScopeDegradationResult:
    """Tier 4: drop the highest-failure-count scope entry from ``plan``.

    Scans ``errors_seen`` for ``(raw, reason)`` keys whose ``raw`` is
    a path-shaped value (vs. an exception class name token used by
    :func:`orchestrator.plan_phase.run_plan_phase` to count parse-class
    recurrences). Picks the entry with the highest count and removes
    its directory prefix from every scope-bearing field on ``plan``
    via :func:`orchestrator.plan_phase._drop_entry_from_plan`.

    Returns a :class:`ScopeDegradationResult` with ``new_plan=None``
    when no path-shaped error qualifies (no scope to degrade) — the
    caller should fall through to Tier 5.

    The threshold for "high count" is intentionally permissive (>=2):
    by the time Tier 4 fires, the architect has burned three full
    attempts, so any path that recurred at all is a recovery
    candidate.
    """
    # Filter to path-shaped keys. The plan_phase outer loop uses
    # ``(type(exc).__name__, "")`` for non-PathValidationError
    # exception classes; the empty-string ``reason`` field is the
    # cheap discriminator.
    path_failures = [
        (raw, reason, count)
        for (raw, reason), count in errors_seen.items()
        if reason  # non-empty reason → PathValidationError-shaped key
    ]
    if not path_failures:
        return ScopeDegradationResult(reason="no_path_failures")
    # Pick the highest-count entry; ties broken by raw string for
    # deterministic behaviour across runs.
    path_failures.sort(key=lambda t: (-t[2], t[0]))
    top_raw, _, top_count = path_failures[0]
    if top_count < 2:
        return ScopeDegradationResult(reason="below_recurrence_threshold")

    # Defer the import — see the module docstring's one-way contract.
    from orchestrator.plan_phase import _drop_entry_from_plan  # noqa: PLC0415

    new_plan, was_dropped, _ = _drop_entry_from_plan(
        plan, top_raw, include_files_new=True
    )
    if not was_dropped:
        return ScopeDegradationResult(
            reason="entry_not_present_in_any_scope_site"
        )
    # Empty-scope guard: silently widening the plan-level edit_scope
    # to the whole-repo sentinel (``[]``) would be a P0 risk; same
    # logic as ``_validate_with_persistent_drop``'s guard. Refuse the
    # degradation in that case so the caller hard-fails through
    # tiers 5-7 rather than running with an unbounded scope.
    if plan.edit_scope and not new_plan.edit_scope:
        return ScopeDegradationResult(
            reason="degradation_would_widen_to_whole_repo"
        )
    # v0.27 Phase 4 mirror: a phase-level edit_scope override that
    # was non-empty pre-drop and is empty post-drop silently widens
    # the phase back to plan scope — same P0 risk class. Refuse.
    for old_phase, new_phase in zip(plan.phases, new_plan.phases):
        if (
            old_phase.edit_scope is not None
            and old_phase.edit_scope
            and new_phase.edit_scope is not None
            and not new_phase.edit_scope
        ):
            return ScopeDegradationResult(
                reason="degradation_would_widen_phase_scope"
            )
    return ScopeDegradationResult(
        new_plan=new_plan,
        dropped_scope_entry=top_raw,
        reason="recurrent_path_failure",
    )


def should_escalate_model(current_model: str | None) -> bool:
    """Tier 5: should the architect's model bump from sonnet to opus?

    Returns ``True`` when ``current_model`` looks like a sonnet
    identifier (substring match); ``False`` otherwise (already on
    opus, or on an unknown model the orchestrator shouldn't override).

    Substring matching keeps the helper resilient against version
    suffixes (``claude-sonnet-4-20250514``) without needing to track
    every per-release identifier.
    """
    if not current_model:
        return False
    lowered = current_model.lower()
    if any(tok in lowered for tok in _OPUS_MODEL_TOKENS):
        return False
    return any(tok in lowered for tok in _SONNET_MODEL_TOKENS)


# v0.36.0 D3: error classes that map to a structural failure (the
# architect's reasoning is fine; the path list isn't). Sonnet handles
# these as well as opus and saves money per attempt.
_STRUCTURAL_REJECTION_CLASSES: frozenset[str] = frozenset(
    {"missing_on_disk", "new_md_deliverable"}
)


def should_change_model_for_class(
    current_model: str | None,
    error_class: str,
    structural_retry_model: str,
) -> str | None:
    """v0.36.0 D3: pick a cheaper architect model for structural retries.

    Returns the configured ``structural_retry_model`` (e.g. ``sonnet``)
    when the current architect model is on opus AND the most-recent
    failure's error_class is structural (missing-on-disk or
    new-md-deliverable). Returns ``None`` otherwise (caller keeps the
    current model unchanged).

    Substring matching on ``"claude-opus"`` keeps the helper resilient
    against version suffixes — the v0.32.0 fixture's opus pin was
    ``claude-opus-4-7`` and future releases will append more.
    """
    if not current_model or not structural_retry_model:
        return None
    if error_class not in _STRUCTURAL_REJECTION_CLASSES:
        return None
    if not current_model.lower().startswith("claude-opus"):
        return None
    return structural_retry_model


def surface_user_intervention_hint(
    archived_dumps: list[str],
):
    """Tier 6: build the structured :class:`RecoveryHint` for the CLI.

    v0.32.0 Phase 5 (Gap G): replaces the Phase 1.4 placeholder
    ``RecoveryHintStub``. Returns a real
    :class:`state.schemas.RecoveryHint` populated with the architect-
    unconvergent class and an actionable user-facing message. The
    archived ``architect-failed-*.md`` paths are surfaced via
    ``relevant_debug_files`` so ``autodev status --blocked`` renders
    the exact paths the operator can ``cat``.

    ``archived_dumps`` is the list of ``architect-failed-*.md`` paths
    accumulated across the failed attempts.
    """
    RecoveryHint = _get_recovery_hint_class()
    return RecoveryHint(
        class_="architect_unconvergent",
        recommended_user_action=(
            "Architect cannot produce a valid plan. Inspect archived "
            ".autodev/debug/architect-failed-*.md dumps and either narrow "
            "the spec or run `autodev plan --force` with a smaller intent."
        ),
        relevant_debug_files=list(archived_dumps),
        commands_to_try=[
            "autodev status --blocked",
            "autodev plan --force '<smaller intent>'",
        ],
    )


def build_forensic_summary(
    *,
    last_exception: BaseException | None,
    archived_dumps: list[str],
    attempts: int,
) -> str:
    """Tier 7: render the human-readable forensic summary.

    The summary is the message body of the final hard-fail exception
    raised by :func:`orchestrator.plan_phase.run_plan_phase`. Mentions
    each archived dump path (best-effort relative-to-cwd for
    readability), the attempt count, and the last exception's class
    name. No tracebacks — those live in the structured-log stream.
    """
    cwd = os.getcwd()
    rendered_dumps = []
    for path in archived_dumps:
        try:
            rendered_dumps.append(os.path.relpath(path, cwd))
        except ValueError:
            rendered_dumps.append(path)
    parts: list[str] = [
        f"Architect plan phase failed after {attempts} attempts.",
    ]
    if rendered_dumps:
        parts.append("Archived rejected markdown dumps:")
        for dump in rendered_dumps:
            parts.append(f"  - {dump}")
    if last_exception is not None:
        parts.append(
            f"Last error: {type(last_exception).__name__}: {last_exception}"
        )
    parts.append(
        "Run `autodev status --blocked` for diagnostic + recovery options."
    )
    return "\n".join(parts)


@dataclass
class RecoveryOutcome:
    """Aggregated outcome of running tiers 4-7.

    ``degraded_plan`` is set when Tier 4 produced a narrower plan
    spec; the caller re-prompts the architect with it. ``escalated_model``
    carries the resolved opus identifier when Tier 5 fires — the
    caller propagates it through the next ``_delegate`` call. The
    ``recovery_hint`` is always populated (Tier 6 is unconditional);
    the ``forensic_summary`` is always populated (Tier 7 is
    unconditional).

    Returned by :func:`run_recovery_tiers` so the caller can route on
    typed fields rather than a dict.
    """

    degraded_plan: "Plan | None" = None
    dropped_scope_entry: str | None = None
    escalated_model: str | None = None
    # v0.32.0 Phase 5 (Gap G): typed as ``Any`` to keep the import
    # graph one-way (this module must not import :mod:`state.schemas`
    # at module load — the lazy ``_get_recovery_hint_class`` helper
    # constructs the model on demand). At runtime this is always a
    # :class:`state.schemas.RecoveryHint` or ``None``.
    recovery_hint: Any = None
    forensic_summary: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def run_recovery_tiers(
    *,
    plan: "Plan | None",
    errors_seen: dict[tuple[str, str], int],
    archived_dumps: list[str],
    last_exception: BaseException | None,
    attempts: int,
    current_architect_model: str | None,
    opus_model_id: str = "claude-opus-4-7",
) -> RecoveryOutcome:
    """Run all four recovery tiers, returning a typed outcome.

    The caller (the architect-retry loop in
    :func:`orchestrator.plan_phase.run_plan_phase`) consumes the
    returned outcome by:

    1. If ``degraded_plan`` is set: re-validate it and re-prompt the
       architect; on success, exit the recovery path. On failure,
       continue with Tier 5.
    2. If ``escalated_model`` is set: re-prompt the architect once
       more under the bumped model.
    3. Always: emit the recovery_hint via the ledger / status CLI.
    4. Always: hard-fail with the forensic summary as the exception
       message.

    ``opus_model_id`` is the resolved identifier the orchestrator
    config pins — passed in rather than hard-coded here so the
    plan-phase caller can read it from ``orch.cfg`` without this
    module taking a config dependency.
    """
    outcome = RecoveryOutcome()

    # Tier 4 — scope degradation.
    if plan is not None:
        deg = attempt_scope_degradation(plan, errors_seen)
        if deg.did_degrade:
            outcome.degraded_plan = deg.new_plan
            outcome.dropped_scope_entry = deg.dropped_scope_entry
            outcome.meta["tier4_reason"] = deg.reason
        else:
            outcome.meta["tier4_skipped_reason"] = deg.reason

    # Tier 5 — model escalation.
    if should_escalate_model(current_architect_model):
        outcome.escalated_model = opus_model_id
        outcome.meta["tier5_reason"] = "sonnet_to_opus"

    # Tier 6 — user-intervention hint. v0.32.0 Phase 5 (Gap G): now a
    # real :class:`state.schemas.RecoveryHint` model (the placeholder
    # ``RecoveryHintStub`` shape was retired). The plan-phase caller
    # stashes the model directly on the raised exception so the CLI
    # surfacing layer can render it without re-marshalling.
    outcome.recovery_hint = surface_user_intervention_hint(archived_dumps)
    outcome.meta["recovery_hint_class"] = outcome.recovery_hint.class_
    outcome.meta["recovery_hint_action"] = (
        outcome.recovery_hint.recommended_user_action
    )

    # Tier 7 — forensic summary.
    outcome.forensic_summary = build_forensic_summary(
        last_exception=last_exception,
        archived_dumps=archived_dumps,
        attempts=attempts,
    )
    return outcome


async def record_phase_degrade(
    orch: "Any", last_exception: BaseException | None
) -> None:
    """v0.42.1 F1b (ADR-0047): route the Tier-7 plan-recovery hard-fail through
    the resolver as an EXPLICIT phase degrade.

    ``run_recovery_tiers`` is a pure, synchronous function with no ``orch`` / no
    event loop, so it cannot record the degrade itself. This thin async helper
    (invoked by the async plan-phase caller once recovery is exhausted) keeps
    the ``record_phase_degrade(`` call inside this module — the single, enforced
    degrade setter — while threading it onto the async boundary the caller owns.
    Observability only; best-effort; never raises.
    """
    try:
        from orchestrator.blocker_resolver import (
            record_phase_degrade as _record,
        )

        await _record(
            orch,
            "plan_recovery",
            last_exception or RuntimeError("plan_recovery_exhausted"),
        )
    except Exception:  # noqa: BLE001 - never break the plan-phase hard-fail
        pass


__all__ = [
    "RecoveryHintStub",
    "RecoveryOutcome",
    "ScopeDegradationResult",
    "attempt_scope_degradation",
    "build_forensic_summary",
    "record_phase_degrade",
    "run_recovery_tiers",
    "should_change_model_for_class",
    "should_escalate_model",
    "surface_user_intervention_hint",
]
