"""Diagnosis phase (ADR-0046) — reproduce-before-plan, sandbox-aware.

Inserted on the bug-fix path between exploration and framing in ``run_plan_phase``:
builds the strongest *sandbox-runnable* feedback loop, reproduces the user's
symptom, generates 3-5 ranked falsifiable hypotheses, instruments to confirm the
root cause, and emits a ``seam`` verdict that feeds the framing altitude decision
(ADR-0044). It is a GATE in spirit (reproduce-first) but NEVER a hard blocker:
mirroring :mod:`orchestrator.framing_phase`, any uncaught error degrades to a
recorded "diagnosis incomplete" pass-through so planning always continues.

Sandbox reality (``architect.md:1108``): no network beyond package registries, no
interactive TTY, no live creds. So the loop is built in a sandbox-friendly ORDER
(failing-test → replay-trace → throwaway-harness → property/fuzz → differential →
bisection → cli-snapshot); the LIVE methods (dev-server-curl / headless-browser /
HITL) become a DELIVERED ARTIFACT, never the autonomous loop. ``loop_fidelity``
is labelled honestly: it can NEVER report ``live`` on a network-less run (NFR5).

Dispatch note: ``diagnostician`` is NOT in ``REQUIRED_AGENT_ROLES`` and therefore
NOT in ``orch.registry``. It dispatches via the specialist ``load_prompt`` path
(:func:`_invoke_diagnostician`, mirroring ``framing_phase._invoke_framing_role``)
— never ``_delegate`` (which is registry-gated and would raise).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from autologging import get_logger

from adapters.types import AgentInvocation
from agents import load_prompt
from state.evidence import read_evidence, write_evidence
from state.schemas import DiagnosisEvidence, FeedbackLoop, Hypothesis

if TYPE_CHECKING:
    from orchestrator import Orchestrator

logger = get_logger()

_EVIDENCE_TASK_ID = "plan-diagnosis"

# §5.1 sandbox-ordered loop methods: the autonomous (network-less, TTY-less,
# cred-less) agent prefers these, in this order. The three LIVE methods are
# tracked separately — when only they reproduce, the loop becomes a delivered
# artifact and the autonomous signal degrades to a synthetic/replay proxy.
_SANDBOX_LOOP_ORDER: tuple[str, ...] = (
    "failing_test",
    "replay_trace",
    "throwaway_harness",
    "property_fuzz",
    "differential",
    "bisection",
    "cli_snapshot",
)
_LIVE_LOOP_METHODS: frozenset[str] = frozenset(
    {"dev_server_curl", "headless_browser", "hitl"}
)

# The diagnostician's scope markers signal whether the spec is a bug/regression.
# Reuses the SAME lexical scope markers as ``spec_validator`` so the is-bug-fix
# gate is consistent with the front-gate vocabulary. ``add``/``feature``/
# ``implement``/``refactor`` are feature-shaped and do NOT count as a bug.
_BUG_MARKERS: tuple[str, ...] = (
    "bug",
    "fix",
    "regression",
    "error",
    "crash",
    "failure",
    "broken",
    "fails",
    "incorrect",
    "wrong",
    "exception",
    "traceback",
)

# Structured-block parsing regexes (mirrors framing's skeptical line extraction).
_LOOP_METHOD_RE = re.compile(r"^\s*LOOP_METHOD:\s*([a-z_]+)\s*$", re.IGNORECASE | re.M)
_LOOP_COMMAND_RE = re.compile(r"^\s*LOOP_COMMAND:\s*(.+?)\s*$", re.IGNORECASE | re.M)
_LOOP_FIDELITY_RE = re.compile(
    r"^\s*LOOP_FIDELITY:\s*(live|synthetic|replay|none)\s*$", re.IGNORECASE | re.M
)
_LOOP_DETERMINISTIC_RE = re.compile(
    r"^\s*LOOP_DETERMINISTIC:\s*(true|false|yes|no|1|0)\s*$", re.IGNORECASE | re.M
)
_REPRODUCED_RE = re.compile(
    r"^\s*REPRODUCED:\s*(true|false|yes|no|1|0)\s*$", re.IGNORECASE | re.M
)
_SYMPTOM_RE = re.compile(r"^\s*SYMPTOM:\s*(.+?)\s*$", re.IGNORECASE | re.M)
_CONFIRMED_CAUSE_RE = re.compile(r"^\s*CONFIRMED_CAUSE:\s*(.+?)\s*$", re.IGNORECASE | re.M)
_SEAM_RE = re.compile(
    r"^\s*SEAM:\s*(correct|shallow|none|unknown)\s*$", re.IGNORECASE | re.M
)
_LIVE_ARTIFACT_RE = re.compile(
    r"^\s*LIVE_REPRO_ARTIFACT:\s*(.+?)\s*$", re.IGNORECASE | re.M
)
_RECURRENCE_RE = re.compile(
    r"^\s*RECURRENCE_AT_SEAM:\s*(true|false|yes|no|1|0)\s*$", re.IGNORECASE | re.M
)

# Hypothesis lines: ``HYPOTHESIS <rank>: <statement> || <prediction>``. The ``||``
# separates the falsifiable prediction; a hypothesis with NO prediction is a
# "vibe" hypothesis and is rejected (FR3 / §7.1).
_HYPOTHESIS_RE = re.compile(
    r"^\s*HYPOTHESIS\s+(\d+):\s*(.+)$", re.IGNORECASE | re.M
)

_TRUTHY = ("true", "yes", "1")

DegradeReason = Literal[
    "disabled",
    "not_bug_fix",
    "dispatch_error",
    "no_loop",
    "ok",
]


@dataclass
class DiagnosisOutcome:
    """In-memory result handed to the call site (the durable record is
    :class:`state.schemas.DiagnosisEvidence`).

    Integration threads ``confirmed_cause`` + the structural signals
    (``recurrence_at_seam`` / ``no_correct_seam``) into the framing inputs;
    ``seam`` and ``loop_fidelity`` carry the architectural + honesty findings.
    A degraded (skipped / errored) run still returns a well-formed outcome with
    ``reproduced=False`` and ``seam="unknown"`` so planning never branches on a
    missing object.
    """

    confirmed_cause: str | None
    seam: Literal["correct", "shallow", "none", "unknown"]
    reproduced: bool
    loop_fidelity: Literal["live", "synthetic", "replay", "none"]
    recurrence_at_seam: bool = False
    no_correct_seam: bool = False
    reason: DegradeReason = "ok"
    structural_signals: list[str] = field(default_factory=list)

    @property
    def ran(self) -> bool:
        """True iff the phase actually dispatched (not disabled / not-a-bug)."""
        return self.reason not in ("disabled", "not_bug_fix")


def _check_diagnosis_disabled() -> bool:
    """Honor the ``AUTODEV_DIAGNOSIS_DISABLED=1`` kill-switch (mirrors framing)."""
    return os.environ.get("AUTODEV_DIAGNOSIS_DISABLED", "").strip() == "1"


def _extract_scope_block(spec: str) -> str:
    """Return the ``## Scope:`` block body if present, else the whole spec.

    ADR-0046 gates on the spec's scope markers. Specs commonly carry a
    ``## Scope:`` heading (see ``spec_validator``); when present we weight the
    is-bug-fix decision toward that block but still fall back to the full text.
    """
    m = re.search(r"##\s*Scope:?\s*(.+?)(?:\n##\s|\Z)", spec, re.IGNORECASE | re.DOTALL)
    return m.group(1) if m else spec


def is_bug_fix(spec: str) -> bool:
    """Heuristic is-bug-fix gate (FR / KD5).

    Reuses ``spec_validator``-style lexical markers: a spec is a bug fix when a
    bug/regression marker appears in its ``## Scope:`` block (weighted) or
    anywhere in the body. Conservative — a spec mentioning a bug marker counts as
    a bug even if it also reads feature-ish (we'd rather diagnose than skip); a
    spec with no bug marker at all (pure feature work) is NOT a bug fix.
    """
    if not spec or not spec.strip():
        return False
    lower = spec.lower()
    scope_lower = _extract_scope_block(spec).lower()
    has_bug = any(m in scope_lower for m in _BUG_MARKERS) or any(
        m in lower for m in _BUG_MARKERS
    )
    # No bug marker anywhere ⇒ pure feature/other work, not a bug fix.
    return has_bug


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in _TRUTHY


def _parse_hypotheses(text: str, max_hypotheses: int) -> tuple[list[Hypothesis], list[str]]:
    """Parse ranked, falsifiable hypotheses (fail-safe).

    Each hypothesis line is ``HYPOTHESIS <rank>: <statement> || <prediction>``.
    A line with no ``||`` prediction is a non-falsifiable "vibe" hypothesis and
    is REJECTED with a diagnostic (FR3). Over-count truncates to
    ``max_hypotheses``. This NEVER raises.
    """
    hyps: list[Hypothesis] = []
    diagnostics: list[str] = []
    for m in _HYPOTHESIS_RE.finditer(text):
        try:
            rank = int(m.group(1))
        except ValueError:  # pragma: no cover - regex guarantees digits
            continue
        body = m.group(2).strip()
        if "||" not in body:
            diagnostics.append(f"diagnosis: hypothesis {rank} has no prediction (rejected)")
            continue
        statement, _, prediction = body.partition("||")
        statement = statement.strip()
        prediction = prediction.strip()
        if not statement or not prediction:
            diagnostics.append(f"diagnosis: hypothesis {rank} empty statement/prediction")
            continue
        hyps.append(
            Hypothesis(
                rank=rank, statement=statement, prediction=prediction, status="untested"
            )
        )
    if len(hyps) > max_hypotheses:
        hyps = hyps[:max_hypotheses]
        diagnostics.append("diagnosis: truncated hypotheses")
    return hyps, diagnostics


def _parse_loop(text: str) -> tuple[FeedbackLoop | None, list[str]]:
    """Parse the ``FeedbackLoop`` block (fail-safe).

    Returns ``(None, diagnostics)`` when no method line is present or the method
    is unrecognized. NEVER raises.
    """
    diagnostics: list[str] = []
    mm = _LOOP_METHOD_RE.search(text)
    if mm is None:
        return None, ["diagnosis: no loop method"]
    method = mm.group(1).lower()
    valid_methods = set(_SANDBOX_LOOP_ORDER) | _LIVE_LOOP_METHODS
    if method not in valid_methods:
        return None, [f"diagnosis: unknown loop method '{method}'"]
    cm = _LOOP_COMMAND_RE.search(text)
    command = cm.group(1).strip() if cm else ""
    fm = _LOOP_FIDELITY_RE.search(text)
    fidelity = fm.group(1).lower() if fm else "none"
    dm = _LOOP_DETERMINISTIC_RE.search(text)
    deterministic = _parse_bool(dm.group(1) if dm else None, default=True)
    try:
        loop = FeedbackLoop(
            method=method,  # type: ignore[arg-type]
            command=command,
            fidelity=fidelity,  # type: ignore[arg-type]
            deterministic=deterministic,
            runtime_s=None,
        )
    except Exception:  # noqa: BLE001 - never let a bad block raise into planning
        return None, ["diagnosis: malformed loop block"]
    return loop, diagnostics


def _enforce_fidelity_honesty(
    loop: FeedbackLoop | None,
    *,
    on_no_live_loop: str,
) -> tuple[FeedbackLoop | None, Literal["live", "synthetic", "replay", "none"], list[str]]:
    """NFR5 honesty gate: the autonomous run is network-less, so a ``live``
    fidelity is NEVER truthful here.

    When the diagnostician (or a live-only method) labels the loop ``live``, we
    downgrade the autonomous loop's fidelity to ``synthetic`` (the best offline
    proxy the agent can actually run) per ``on_no_live_loop="synthetic_plus_artifact"``
    and surface a diagnostic so the dishonesty is auditable. Returns the
    (possibly-downgraded) loop, the effective ``loop_fidelity``, and diagnostics.
    """
    diagnostics: list[str] = []
    if loop is None:
        return None, "none", diagnostics

    is_live_method = loop.method in _LIVE_LOOP_METHODS
    claims_live = loop.fidelity == "live"

    if claims_live or is_live_method:
        # The autonomous path cannot truly run a live loop. Honesty over green.
        if on_no_live_loop == "block":
            # The caller still degrades gracefully; we record the honest finding.
            diagnostics.append("diagnosis: live-only loop, on_no_live_loop=block")
        downgraded = loop.model_copy(update={"fidelity": "synthetic"})
        diagnostics.append(
            "diagnosis: live fidelity downgraded to synthetic (network-less run)"
        )
        return downgraded, "synthetic", diagnostics

    return loop, loop.fidelity, diagnostics


def _build_context_for_dispatch(
    spec: str, explore_ev: str, max_hypotheses: int
) -> str:
    """Render the CONTEXT block appended to the diagnostician prompt.

    The diagnostician prompt references a CONTEXT block (the specialist path does
    not call ``render_prompt`` — nothing substitutes ``{{…}}`` tokens), mirroring
    ``framing_phase._render_context``.
    """
    loop_order = " > ".join(_SANDBOX_LOOP_ORDER)
    return (
        "## CONTEXT\n"
        f"action: diagnose\n"
        f"max_hypotheses: {max_hypotheses}\n"
        f"sandbox_loop_order: {loop_order}\n"
        f"live_methods_become_artifact: {', '.join(sorted(_LIVE_LOOP_METHODS))}\n"
        f"\n### spec\n{spec}\n"
        f"\n### explorer_findings\n{explore_ev}\n"
    )


async def _invoke_diagnostician(
    orch: "Orchestrator",
    spec: str,
    explore_ev: str,
    max_hypotheses: int,
) -> str:
    """Dispatch the unregistered ``diagnostician`` role via the ``load_prompt``
    specialist path (mirrors ``framing_phase._invoke_framing_role``).

    NEVER ``_delegate`` (registry-gated). Reads model/max-turns from
    ``cfg.agents['diagnostician']``; honors ``cfg.diagnosis.diagnostician_model``.
    """
    raw_prompt = load_prompt("diagnostician")
    context_block = _build_context_for_dispatch(spec, explore_ev, max_hypotheses)
    full_prompt = "\n\n---\n".join([raw_prompt.strip(), context_block])

    dx_cfg = orch.cfg.diagnosis
    agent_cfg = orch.cfg.agents["diagnostician"]
    inv = AgentInvocation(
        role="diagnostician",
        prompt=full_prompt,
        cwd=orch.cwd,
        model=dx_cfg.diagnostician_model or agent_cfg.model,
        max_turns=agent_cfg.max_turns or 1,
    )
    result = await orch.adapter.execute(inv)
    return result.text or ""


def _outcome_from_evidence(ev: DiagnosisEvidence) -> DiagnosisOutcome:
    signals: list[str] = []
    if ev.recurrence_at_seam:
        signals.append("recurrence_at_seam")
    if ev.no_correct_seam:
        signals.append("no_correct_seam")
    return DiagnosisOutcome(
        confirmed_cause=ev.confirmed_cause,
        seam=ev.seam,
        reproduced=ev.reproduced,
        loop_fidelity=ev.loop_fidelity,
        recurrence_at_seam=ev.recurrence_at_seam,
        no_correct_seam=ev.no_correct_seam,
        reason="ok",
        structural_signals=signals,
    )


def _degraded_outcome(reason: DegradeReason) -> DiagnosisOutcome:
    """A well-formed pass-through outcome for the disabled / skipped / error
    paths. Planning continues; ``seam='unknown'`` and ``reproduced=False`` make
    the degradation legible to framing/Integration."""
    return DiagnosisOutcome(
        confirmed_cause=None,
        seam="unknown",
        reproduced=False,
        loop_fidelity="none",
        recurrence_at_seam=False,
        no_correct_seam=False,
        reason=reason,
        structural_signals=[],
    )


async def _ledger_op(orch: "Orchestrator", op: str, payload: dict) -> None:
    """Best-effort ledger breadcrumb — diagnosis must NEVER block planning.

    Mirrors framing's best-effort try/except around ``plan_manager.ledger_append``.
    The diagnosis ops (``diagnosis_loop_built``, ``bug_reproduced`` /
    ``repro_unavailable_live``, ``hypotheses_ranked``, ``cause_confirmed``,
    ``seam_finding``) are registered in ``state.ledger.LedgerOp`` + handled as
    audit-only no-ops in ``ledger._apply_op``.
    """
    try:
        await orch.plan_manager.ledger_append(op, payload)
    except Exception as exc:  # noqa: BLE001 - best-effort audit
        logger.warning("diagnosis_phase.ledger_failed", op=op, err=str(exc))


async def run_diagnosis_phase(
    orch: "Orchestrator",
    spec: str,
    explore_ev: str,
) -> DiagnosisOutcome:
    """Reproduce-before-plan diagnosis (ADR-0046).

    Off-ramps (each returns a well-formed degraded outcome, never raises):
      * disabled (config / ``AUTODEV_DIAGNOSIS_DISABLED=1``) → ``reason='disabled'``
      * not a bug fix and ``cfg.diagnosis.bug_only`` → ``reason='not_bug_fix'``
      * dispatch / parse error → ``reason='dispatch_error'`` (logged, planning continues)

    Deterministic-on-resume: re-reads ``plan-diagnosis`` evidence FIRST and
    returns WITHOUT re-dispatching (0 agent calls).
    """
    cwd = orch.cwd
    dx_cfg = orch.cfg.diagnosis

    # 1. Enable / kill-switch — both off-ramps degrade to a pass-through.
    if not dx_cfg.enabled or _check_diagnosis_disabled():
        logger.info("diagnosis_phase.disabled")
        return _degraded_outcome("disabled")

    # 2. Resume re-read FIRST — before the is-bug gate AND before dispatch.
    existing = await read_evidence(cwd, _EVIDENCE_TASK_ID, "diagnosis")
    if isinstance(existing, DiagnosisEvidence):
        logger.info(
            "diagnosis_phase.resumed",
            seam=existing.seam,
            reproduced=existing.reproduced,
        )
        return _outcome_from_evidence(existing)

    # 3. Is-bug-fix gate (KD5) — feature work skips the phase entirely (+0 cost).
    if dx_cfg.bug_only and not is_bug_fix(spec):
        logger.info("diagnosis_phase.skip_not_bug_fix")
        return _degraded_outcome("not_bug_fix")

    logger.info("diagnosis_phase.start")

    # 4. Fail-safe wrapper around the whole body: diagnosis NEVER blocks planning.
    try:
        return await _run_inner(orch, spec, explore_ev)
    except Exception as exc:  # noqa: BLE001 - degrade, never raise into planning
        logger.warning("diagnosis_phase.degraded", err=str(exc))
        await _ledger_op(
            orch, "seam_finding", {"seam": "unknown", "degraded": True, "err": str(exc)}
        )
        # ADR-0047 (B1): record the degrade as an explicit resolver decision
        # (observability-only; diagnosis still returns its degraded outcome).
        try:
            from orchestrator.blocker_resolver import record_phase_degrade

            await record_phase_degrade(orch, "diagnosis", exc)
        except Exception:  # noqa: BLE001
            pass
        return _degraded_outcome("dispatch_error")


async def _run_inner(
    orch: "Orchestrator",
    spec: str,
    explore_ev: str,
) -> DiagnosisOutcome:
    """The phase body (Phases 1-4). Separated so the fail-safe wrapper in
    :func:`run_diagnosis_phase` can catch ANYTHING this raises."""
    cwd = orch.cwd
    dx_cfg = orch.cfg.diagnosis

    # Phase 1-4: one diagnostician call (specialist dispatch — never _delegate).
    raw = await _invoke_diagnostician(orch, spec, explore_ev, dx_cfg.max_hypotheses)

    # Parse skeptically (each parser is fail-safe).
    loop, loop_diags = _parse_loop(raw)
    loop, loop_fidelity, honesty_diags = _enforce_fidelity_honesty(
        loop, on_no_live_loop=dx_cfg.on_no_live_loop
    )
    reproduced_m = _REPRODUCED_RE.search(raw)
    reproduced = _parse_bool(reproduced_m.group(1) if reproduced_m else None)
    symptom_m = _SYMPTOM_RE.search(raw)
    symptom = symptom_m.group(1).strip() if symptom_m else ""
    cause_m = _CONFIRMED_CAUSE_RE.search(raw)
    confirmed_cause = cause_m.group(1).strip() if cause_m else None
    if confirmed_cause is not None and confirmed_cause.lower() in ("none", "unknown", ""):
        confirmed_cause = None
    seam_m = _SEAM_RE.search(raw)
    seam: Literal["correct", "shallow", "none", "unknown"] = (
        seam_m.group(1).lower() if seam_m else "unknown"  # type: ignore[assignment]
    )
    artifact_m = _LIVE_ARTIFACT_RE.search(raw)
    live_repro_artifact = artifact_m.group(1).strip() if artifact_m else None
    if live_repro_artifact is not None and live_repro_artifact.lower() in ("none", ""):
        live_repro_artifact = None
    recurrence_m = _RECURRENCE_RE.search(raw)
    recurrence_at_seam = _parse_bool(
        recurrence_m.group(1) if recurrence_m else None
    )

    hypotheses, hyp_diags = _parse_hypotheses(raw, dx_cfg.max_hypotheses)

    # §5.1/FR5: "no correct seam" is the architectural finding routed to framing.
    no_correct_seam = seam in ("none", "shallow")

    diagnostics = loop_diags + honesty_diags + hyp_diags
    if diagnostics:
        logger.info("diagnosis_phase.parse_diagnostics", diagnostics=diagnostics)

    # 5. Persist evidence BEFORE any ledger op / return (crash-safety).
    ev = DiagnosisEvidence(
        task_id=_EVIDENCE_TASK_ID,
        loop=loop,
        reproduced=reproduced,
        symptom=symptom,
        hypotheses=hypotheses,
        confirmed_cause=confirmed_cause,
        seam=seam,
        loop_fidelity=loop_fidelity,
        live_repro_artifact=live_repro_artifact,
        recurrence_at_seam=recurrence_at_seam,
        no_correct_seam=no_correct_seam,
    )
    await write_evidence(cwd, _EVIDENCE_TASK_ID, ev)
    logger.info(
        "diagnosis_phase.evidence_written",
        seam=seam,
        reproduced=reproduced,
        loop_fidelity=loop_fidelity,
        n_hypotheses=len(hypotheses),
    )

    # 6. Best-effort ledger breadcrumbs (audit-only; never block planning).
    if loop is not None:
        await _ledger_op(
            orch,
            "diagnosis_loop_built",
            {
                "method": loop.method,
                "fidelity": loop.fidelity,
                "deterministic": loop.deterministic,
            },
        )
    # FR6: when the loop is a synthetic/replay proxy for a live-only bug AND a
    # live-repro artifact was delivered, the reproduction is "unavailable live".
    live_only = loop_fidelity in ("synthetic", "replay") and live_repro_artifact is not None
    if reproduced and not live_only:
        await _ledger_op(
            orch, "bug_reproduced", {"symptom": symptom, "reproduced": True}
        )
    else:
        await _ledger_op(
            orch,
            "repro_unavailable_live",
            {
                "symptom": symptom,
                "fidelity": loop_fidelity,
                "artifact": live_repro_artifact,
            },
        )
    await _ledger_op(orch, "hypotheses_ranked", {"count": len(hypotheses)})
    if confirmed_cause is not None:
        await _ledger_op(
            orch, "cause_confirmed", {"cause": confirmed_cause, "seam": seam}
        )
    await _ledger_op(
        orch,
        "seam_finding",
        {
            "seam": seam,
            "recurrence_at_seam": recurrence_at_seam,
            "no_correct_seam": no_correct_seam,
        },
    )

    logger.info(
        "diagnosis_phase.complete",
        seam=seam,
        no_correct_seam=no_correct_seam,
        loop_fidelity=loop_fidelity,
        evidence_path=str(
            cwd / ".autodev" / "evidence" / "plan-diagnosis-diagnosis.json"
        ),
    )
    return _outcome_from_evidence(ev)


__all__ = [
    "DiagnosisOutcome",
    "is_bug_fix",
    "run_diagnosis_phase",
]
