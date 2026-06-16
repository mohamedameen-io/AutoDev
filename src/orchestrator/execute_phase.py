"""Execute-phase loop and conflict-escalation helpers.

For each pending task (or a specific task when ``task_id`` is given):

  1. Build a :class:`DelegationEnvelope` from the task.
  2. developer -> :class:`CoderEvidence`. Retry on adapter failure up to
     ``qa_retry_limit``; on exhaustion, escalate.
  3. test_engineer -> :class:`TestEvidence`. Any failure retries test_engineer.
  4. auto-gates (syntax/lint/build/run_tests/secretscan). ``TODO(v0.26+)``:
     replace the current always-pass placeholder with real gate execution.
  5. reviewer -> :class:`ReviewEvidence`. NEEDS_CHANGES / REJECTED counts
     as a retry back to developer with the issue list injected as context.
  6. ``TODO(v0.26+)``: wire :class:`ImplementationTournament` into the
     execute-phase FSM. Today's execute path skips it; the impl-tournament
     module itself exists and works via the dedicated CLI surface.
  7. Mark task complete.

On retry exhaustion, call ``critic_sounding_board`` once, flag the task as
escalated, mark it blocked, and stop the loop.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from adapters.git_utils import _git_rev_parse_head, extract_files_from_diff
from adapters.types import AgentInvocation, AgentResult
from errors import AutodevError, GuardrailExceededError, TournamentError
from autologging import get_logger
from orchestrator import failure_classes as _fcls
from orchestrator.blocker_guard import block_task
from orchestrator.delegation_envelope import DelegationEnvelope
from orchestrator.worktree import WorktreeError, WorktreeManager
from state.evidence import write_evidence, write_patch
from qa import (
    GateResult,
    detect_language,
    run_build_check,
    run_lint,
    run_hallucination_guard,
    run_mutation_test,
    run_secretscan,
    run_syntax_check,
    run_tests,
)
from qa.code_size import run_code_size
from qa.debug_tag_gate import run_debug_tag_gate
from qa.reproduce_gate import run_reproduce_gate
from state.paths import AUTODEV_DIR, autodev_root
from state.schemas import (
    CoderEvidence,
    CriticEvidence,
    Phase,
    Plan,
    ReviewEvidence,
    Task,
    TestEvidence,
)
from orchestrator.test_result_classifier import (
    classify_test_result,
    redact_stderr_tail,
)
from tournament.effort import resolve_role_effort
from tournament.errors import (
    AuthenticationFailedError,
    InfrastructureCircuitOpenError,
)
from tournament.task_overrides import (
    resolve_task_max_turns,
    resolve_task_timeout_s,
)


if TYPE_CHECKING:
    from orchestrator import Orchestrator


logger = get_logger(__name__)


# Default subprocess timeout for the developer adapter when ``Task.complexity``
# is ``None`` (legacy / pre-v0.8.0 plans). 900s = 15 minutes — picked to give
# untagged tasks comfortable runway without slipping into the 1800s reserved
# for the architect-tagged ``complex`` bucket. Reused by the per-task
# resolver's ``spec_default`` argument for symmetry; the resolver itself
# returns ``None`` and ``delegate`` falls back to this constant.
_DEFAULT_DEVELOPER_TIMEOUT_S = 900


# v0.9.0: terminal task statuses that count toward "phase done" for
# phase-review trigger detection. All three pause forward progress for
# the task in question; ``complete`` and ``skipped`` are intentional
# terminal states; ``blocked`` is escalation. The phase-review tournament
# fires once the LAST task in the phase reaches one of these.
_TERMINAL_TASK_STATUSES: tuple[str, ...] = ("complete", "blocked", "skipped")


class TaskEscalatedError(AutodevError):
    """Raised (and logged) when a task is escalated to critic_sounding_board.

    The execute loop catches this internally — it is surfaced to the CLI
    only when the user explicitly targets a task that ends up escalated.
    """


# v0.11.0: conflict-escalation helpers.

ConflictAction = Literal["rebase-and-retry", "abandon-task", "rewrite"]


@dataclass
class ConflictResolution:
    """Parsed result from ``critic_sounding_board`` in CONFLICT ESCALATION MODE.

    Attributes:
        action: One of ``rebase-and-retry`` / ``abandon-task`` /
            ``rewrite``. Defaults to ``abandon-task`` when the parser
            cannot extract a valid directive (defensive — the worker
            then blocks the task with a parser-fallback reason).
        rewrite_guidance: Free-form text from the critic intended to
            steer a re-invoked developer pass. Only meaningful when
            ``action == "rewrite"``. Empty for the other two branches.
    """

    action: ConflictAction = "abandon-task"
    rewrite_guidance: str = ""


# Regex matching the trailing ``RESOLUTION:`` directive on its own line.
# The directive may be followed by trailing whitespace / blank lines.
_CONFLICT_DIRECTIVE_RE = re.compile(
    r"^RESOLUTION:\s*(rebase-and-retry|abandon-task|rewrite)\s*$",
    re.MULTILINE,
)


# v0.29.0 Bug 6: subtypes that classify a guardrail-trip as
# infrastructure (the LLM was unavailable) rather than ``"cap"`` (the
# agent legitimately ran out of turns/budget). Mirrors the constants
# tournament.llm uses to classify retry behaviour for adapter results.
_INFRA_ADAPTER_SUBTYPES: frozenset[str] = frozenset(
    {"auth_failed", "rate_limited", "server_error"}
)


# v0.32.0 (Phase 5, Gap G): RecoveryHint builders. Each soft-block
# site in this module populates a structured hint via these helpers
# before calling ``update_task_status(..., "blocked", meta={..., "recovery_hint": ...})``
# so ``autodev status --blocked`` can render an actionable panel
# without forcing the user to hand-read evidence files.

_GuardrailClassToHintClass: dict[
    Literal["verdict", "infrastructure", "cap"],
    Literal[
        "missing_test_output",
        "thin_review_evidence",
        "architect_unconvergent",
        "model_capacity_exhausted",
        "user_decision_required",
        "network_transient",
    ],
] = {
    "verdict": "user_decision_required",
    "infrastructure": "network_transient",
    "cap": "model_capacity_exhausted",
}


def maybe_enable_auto_soft_pass(orch: "Orchestrator", diagnosis: str) -> bool:
    """Track consecutive ``capture_failed``; auto-enable soft-pass on a huge repo.

    After >=2 consecutive test-runner ``capture_failed`` diagnoses on a
    **huge** repo, auto-enable ``treat_unrunnable_tests_as_no_tests``
    in-memory (idempotent) so the current task — and the rest of the
    session — can soft-pass an infra-class capture failure instead of
    churning the test-diagnosis breaker.

    No-op (returns ``False``) when:

    * the ``huge_repo_overrides_disabled`` escape hatch is set;
    * the repo is not huge (honors the tier invariant — small repos keep
      strict gating);
    * the flag is already enabled (the idempotency guard).

    A clean ``ok`` / ``no_tests_found`` diagnosis resets the counter.

    Returns ``True`` iff this call just flipped the flag.
    """
    # Escape hatch wins — restore pre-tier behaviour on huge repos.
    if getattr(orch.cfg, "huge_repo_overrides_disabled", False):
        return False
    # Small repos never auto-soft-pass: honors the tier invariant. Use the
    # capacity signal so this mirrors the task-role huge-scaling path.
    if not getattr(getattr(orch, "_repo_capacity", None), "is_huge", False):
        return False

    if diagnosis == "capture_failed":
        orch._consecutive_capture_failed = (
            getattr(orch, "_consecutive_capture_failed", 0) + 1
        )
    elif diagnosis in ("ok", "no_tests_found"):
        orch._consecutive_capture_failed = 0
        return False
    # Any other diagnosis (collection_failed / runtime_crash / ...) neither
    # increments nor resets — only a clean run clears the streak.

    if getattr(orch, "_consecutive_capture_failed", 0) < 2:
        return False
    if getattr(orch.cfg, "treat_unrunnable_tests_as_no_tests", False):
        # Already enabled — idempotent no-op.
        return False

    # Flip in-memory only — the profile is never written to disk.
    orch.cfg.treat_unrunnable_tests_as_no_tests = True
    logger.warning(
        "execute_phase.auto_soft_pass_enabled",
        reason="consecutive_capture_failed",
        consecutive_capture_failed=orch._consecutive_capture_failed,
    )
    # Best-effort forensic ledger op (fire-and-forget). Guarded so test
    # stubs without a plan_manager / running loop never raise.
    if getattr(orch, "plan_manager", None) is not None:
        try:
            asyncio.ensure_future(
                orch.plan_manager.ledger_append(
                    op="auto_soft_pass_enabled",
                    payload={
                        "reason": "consecutive_capture_failed",
                        "consecutive_capture_failed": (
                            orch._consecutive_capture_failed
                        ),
                    },
                )
            )
        except Exception:  # noqa: BLE001 — telemetry never blocks
            pass
    return True


async def maybe_emit_under_decomposed_runtime(
    orch: "Orchestrator",
    task: "Task",
    developer_result: "AgentResult",
) -> bool:
    """v0.39.0 (Cluster C2c): emit ``task_under_decomposed`` on a budget-bust.

    When a developer task burns its (already huge-scaled) ``max_turns`` budget
    with ``subtype="error_max_turns"`` on a **huge** repo, on its first one or
    two attempts (``retry_count in (0, 1)``), that is a strong signal the
    architect should have split it into smaller ``medium`` tasks. Emit a
    best-effort, ``plan_manager``-guarded ``task_under_decomposed`` breadcrumb
    (``source="runtime"``) for offline retrospectives.

    Purely observational: returns ``True`` iff it just emitted, but the caller
    ignores the return — control flow is unchanged either way. No-op when:

    * the failure subtype is not ``error_max_turns``;
    * ``retry_count >= 2`` (later attempts; the early signal is what matters);
    * the repo is not huge (honors the tier invariant);
    * there is no ``plan_manager`` (test stubs / pre-init).

    Never raises — any ledger error is swallowed with a warning.
    """
    if developer_result.subtype != "error_max_turns":
        return False
    if task.retry_count not in (0, 1):
        return False
    if not getattr(getattr(orch, "_repo_capacity", None), "is_huge", False):
        return False
    if getattr(orch, "plan_manager", None) is None:
        return False
    try:
        await orch.plan_manager.ledger_append(
            op="task_under_decomposed",
            payload={
                "task_id": task.id,
                "source": "runtime",
                "attempt": int(task.retry_count),
                "file_count": len(task.files),
                "files": task.files[:10],
                "complexity": task.complexity,
            },
        )
        return True
    except Exception as exc:  # noqa: BLE001 — telemetry never blocks
        logger.warning(
            "execute_phase.ledger_append_failed",
            op="task_under_decomposed",
            err=str(exc),
        )
        return False


def _build_recovery_hint(
    *,
    task_id: str,
    hint_class: Literal[
        "missing_test_output",
        "thin_review_evidence",
        "architect_unconvergent",
        "model_capacity_exhausted",
        "user_decision_required",
        "network_transient",
    ],
    action: str,
    evidence_files: list[str] | None = None,
    debug_files: list[str] | None = None,
    commands: list[str] | None = None,
):
    """Construct a :class:`state.schemas.RecoveryHint` for a soft block.

    Defaults the evidence path to ``.autodev/evidence/<task>-*.json``
    and adds ``autodev requeue --task <id>`` to the commands list when
    the caller does not override it. Populated paths are repo-relative
    so the CLI surfacing layer can render them as copy-paste-ready
    inputs to ``cat`` / editor commands.
    """
    from state.schemas import RecoveryHint  # noqa: PLC0415 — break cycle

    evid = (
        list(evidence_files)
        if evidence_files is not None
        else [
            f".autodev/evidence/{task_id}-coder.json",
            f".autodev/evidence/{task_id}-review.json",
            f".autodev/evidence/{task_id}-test.json",
        ]
    )
    dbg = list(debug_files) if debug_files is not None else []
    cmds = (
        list(commands)
        if commands is not None
        else [
            f"autodev requeue --task {task_id}",
            "autodev status --blocked",
        ]
    )
    return RecoveryHint(
        class_=hint_class,
        recommended_user_action=action,
        relevant_evidence_files=evid,
        relevant_debug_files=dbg,
        commands_to_try=cmds,
    )


# v0.37.0 H1 / v0.38.0 HK1: map user-facing ``include_kinds`` labels to
# the on-disk evidence file kinds. v0.38.0 unified the user-facing label
# ``"coder"`` with the on-disk
# :class:`state.schemas.CoderEvidence.kind` discriminator
# (``"developer"``) — the indirection now resolves to identity, but the
# dict shape is preserved so future divergences (e.g. multi-evidence per
# kind) can rewire without touching the call site.
_RECENT_EVIDENCE_KIND_TO_DISK: dict[str, str] = {
    "review": "review",
    "test": "test",
    "developer": "developer",
}
_RECENT_EVIDENCE_KIND_TO_LABEL: dict[str, str] = {
    "review": "REVIEWER_RAW",
    "test": "TEST_RAW",
    "developer": "DEVELOPER_RAW",
}


async def _build_recent_evidence_block(
    orch: "Orchestrator",
    task: Task,
    reason: str,
    web_context_block: str = "",
) -> str:
    """v0.37.0 H1: thread reviewer / test / developer ``raw_response``
    bodies into the ``recent_evidence`` block sent to stuck-recovery
    prompts.

    The legacy one-liner (``reason`` plus optional ``web_context_block``)
    gave architect-consult and sounding-board agents only the verdict
    token — they could not refine without seeing what was actually
    rejected. This helper reads the latest evidence files for ``task.id``,
    extracts each kind's ``raw_response`` (falling back to
    ``output_text``), truncates to
    ``cfg.recent_evidence_max_chars_per_kind`` chars (reviewer / test:
    tail-only because the verdict + reasoning sit at the bottom;
    developer: head + tail per v0.38.0 HK2 because diffs + tool
    transcripts often place the failing call site near the top),
    and renders labelled blocks under ``REVIEWER_RAW:``, ``TEST_RAW:``,
    ``DEVELOPER_RAW:``.

    Returns the legacy one-liner verbatim when the feature is disabled
    (per-kind cap = 0 OR include_kinds = []). Tolerates missing /
    unreadable evidence per kind — the kind is silently skipped, never
    raised.
    """
    from state.evidence import read_evidence  # noqa: PLC0415 — break cycle

    cap_base = int(getattr(orch.cfg, "recent_evidence_max_chars_per_kind", 0))
    # v0.37.0 H5: auto-scale the per-kind cap on huge repos. Resolver
    # short-circuits (returns base) when repo is small or the escape
    # hatch is set — sync wrapper (no ledger) because this helper is
    # called synchronously from many sites and the telemetry op is
    # already emitted at the orchestrator-init / cap-check call sites.
    try:
        from orchestrator.huge_repo_overrides import resolve_huge_repo_value

        cap_eff, _ = resolve_huge_repo_value(
            key="recent_evidence_max_chars_per_kind",
            base_value=float(cap_base),
            cwd=orch._cwd,
            cfg=orch.cfg,
        )
        cap = int(round(cap_eff))
    except Exception:  # noqa: BLE001 — defensive: never block evidence path
        cap = cap_base
    include_kinds = list(
        getattr(orch.cfg, "recent_evidence_include_kinds", []) or []
    )
    if cap <= 0 or not include_kinds:
        return f"{web_context_block}{reason}" if web_context_block else reason

    populated_kinds: list[str] = []
    blocks: list[str] = []
    for label_kind in include_kinds:
        disk_kind = _RECENT_EVIDENCE_KIND_TO_DISK.get(label_kind)
        if disk_kind is None:
            continue
        try:
            ev = await read_evidence(orch.cwd, task.id, disk_kind)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.recent_evidence_read_failed",
                task_id=task.id,
                kind=label_kind,
                err=str(exc),
            )
            continue
        if ev is None:
            continue
        blob = getattr(ev, "raw_response", None) or getattr(ev, "output_text", "") or ""
        if not blob:
            continue
        # v0.38.0 HK2: developer raw_response tails truncate by both
        # head and tail. Reviewer / test bodies are verdict-first
        # (tail-biased) so tail-only stays correct; developer diffs +
        # tool transcripts often place the failing tool-call near the
        # top with the final error near the bottom, so seeing only the
        # tail loses the call site that matters for refinement.
        if len(blob) <= cap:
            truncated = blob
        elif label_kind == "developer":
            half = cap // 2
            truncated = (
                f"{blob[:half]}"
                f"\n[...truncated {len(blob) - cap} bytes...]\n"
                f"{blob[-half:]}"
            )
        else:
            truncated = blob[-cap:]
        section_label = _RECENT_EVIDENCE_KIND_TO_LABEL.get(
            label_kind, label_kind.upper() + "_RAW"
        )
        blocks.append(f"{section_label}:\n{truncated}")
        populated_kinds.append(label_kind)

    if not blocks:
        legacy = (
            f"{web_context_block}{reason}" if web_context_block else reason
        )
        logger.info(
            "execute_phase.recent_evidence_built",
            task_id=task.id,
            kinds=[],
            total_chars=len(legacy),
        )
        return legacy

    joined = "\n\n".join(blocks)
    if web_context_block:
        rendered = f"{web_context_block}{reason}\n\n{joined}"
    else:
        rendered = f"{reason}\n\n{joined}"
    logger.info(
        "execute_phase.recent_evidence_built",
        task_id=task.id,
        kinds=populated_kinds,
        total_chars=len(rendered),
    )
    return rendered


def _build_recovery_hint_from_reason(
    *, task_id: str, reason: str
) -> "state.schemas.RecoveryHint":  # noqa: F821 — string annotation, lazy import
    """Map a free-form ``reason`` string to a typed :class:`RecoveryHint`.

    Used by sites that already classify via the ``reason`` text passed
    through :func:`_try_retry_or_escalate` (developer adapter failure,
    reviewer NEEDS_CHANGES / REJECTED / MALFORMED, tests failed,
    review-tournament max-rounds, …). Conservative fallback: when the
    text matches none of the known patterns, classify as
    ``"user_decision_required"`` so the operator still gets actionable
    text instead of a bare "blocked" message.
    """
    lowered = (reason or "").lower()
    if "review_tournament" in lowered or "reviewer" in lowered:
        return _build_recovery_hint(
            task_id=task_id,
            hint_class="thin_review_evidence",
            action=(
                f"Reviewer rejected the implementation. Inspect the "
                f"rejection in .autodev/evidence/{task_id}-review.json, "
                f"update the implementation to address the issues, then "
                f"`autodev requeue --task {task_id}`."
            ),
        )
    if "tests" in lowered:
        return _build_recovery_hint(
            task_id=task_id,
            hint_class="missing_test_output",
            action=(
                f"Tests failed across retries. Inspect "
                f".autodev/evidence/{task_id}-test.json for the failure "
                f"signature, fix the underlying code or test, then "
                f"`autodev requeue --task {task_id}`."
            ),
        )
    if "adapter" in lowered or "qa_gate" in lowered:
        return _build_recovery_hint(
            task_id=task_id,
            hint_class="network_transient",
            action=(
                "An adapter / QA gate failure persisted across retries. "
                "Refresh credentials / connection (run `autodev doctor` "
                f"to diagnose) and `autodev requeue --task {task_id}`."
            ),
            commands=[
                "autodev doctor",
                f"autodev requeue --task {task_id}",
            ],
        )
    return _build_recovery_hint(
        task_id=task_id,
        hint_class="user_decision_required",
        action=(
            "Multiple refinement cycles produced no improvement. Manual "
            "review needed — inspect evidence and decide whether to "
            "retry, narrow the task, or skip."
        ),
    )


def _build_guardrail_block_meta(
    *, orch: "Orchestrator", task_id: str, exc: Exception
) -> dict:
    """v0.32.0 (Phase 5, Gap G): build the ``meta`` dict for the four
    ``guardrail_exceeded`` soft-block sites in this module.

    Centralises the previously-duplicated block payload (typed reason
    class, forensic adapter status / subtype) AND populates a
    structured :class:`state.schemas.RecoveryHint` so
    ``autodev status --blocked`` renders an actionable user message
    without forcing the user to hand-read evidence files.
    """
    last_subtype = getattr(orch, "_last_adapter_subtype", None)
    block_class = _classify_guardrail_block(last_subtype)
    hint_class = _GuardrailClassToHintClass[block_class]
    if block_class == "infrastructure":
        action = (
            "Infrastructure failure (auth / rate-limit / 5xx) tripped the "
            "guardrail. Refresh credentials and `autodev resume`."
        )
        commands = [
            "autodev doctor",
            f"autodev requeue --task {task_id}",
            "autodev resume",
        ]
    elif block_class == "cap":
        action = (
            "The agent exhausted its budget (turns / tokens / time). "
            "Either widen the per-task cap in .autodev/config.json under "
            f"`guardrails`, narrow the task scope, or `autodev requeue --task {task_id}`."
        )
        commands = [
            "autodev doctor",
            f"autodev requeue --task {task_id}",
        ]
    else:  # "verdict"
        action = (
            "The agent reached a guardrail trip with no clean recovery "
            f"path. Inspect .autodev/evidence/{task_id}-*.json and "
            "decide whether to retry, narrow the task, or skip."
        )
        commands = [f"autodev requeue --task {task_id}"]
    hint = _build_recovery_hint(
        task_id=task_id,
        hint_class=hint_class,
        action=action,
        commands=commands,
    )
    return {
        "blocked_reason": f"guardrail_exceeded: {exc}",
        "block_reason_class": block_class,
        "api_error_status": getattr(orch, "_last_adapter_api_error_status", None),
        "last_adapter_subtype": last_subtype,
        "recovery_hint": hint,
    }


def _classify_guardrail_block(
    last_subtype: str | None,
) -> Literal["verdict", "infrastructure", "cap"]:
    """v0.29.0 Bug 6: classify a guardrail-tripped block.

    The guardrail can fire for two materially different reasons:

      * The agent legitimately consumed its budget on the work
        (subtype is ``None`` or something like ``error_max_turns``).
        Classify as ``"cap"``: requeueing without widening the cap
        would just re-burn the budget.
      * The most recent adapter result was an auth / rate-limit /
        5xx failure that the retry layer kept colliding with until
        the guardrail tripped. Classify as ``"infrastructure"``:
        the operator can fix the environment and ``autodev requeue
        --infrastructure`` will pick the task back up.

    The ``last_subtype`` parameter is the ``subtype`` of the most
    recent :class:`AgentResult` the orchestrator saw, stashed on
    ``orch._last_adapter_subtype`` by :func:`delegate`.
    """
    if last_subtype in _INFRA_ADAPTER_SUBTYPES:
        return "infrastructure"
    return "cap"


def _parse_conflict_resolution(critic_response: str) -> ConflictResolution:
    """Extract a :class:`ConflictResolution` from the critic's text.

    Looks for a ``RESOLUTION: <action>`` line, anchored to its own line.
    On parse failure (no directive found, multiple conflicting
    directives, etc.) returns ``ConflictResolution(action="abandon-task")``
    so the caller can safely block the task.

    For ``rewrite``, captures everything BEFORE the matched directive
    line as ``rewrite_guidance`` (stripped of trailing whitespace). The
    guidance is what the orchestrator passes back to the developer
    when re-invoking.
    """
    if not critic_response:
        return ConflictResolution()

    matches = list(_CONFLICT_DIRECTIVE_RE.finditer(critic_response))
    if not matches:
        return ConflictResolution()
    # Take the LAST directive — the prompt instructs ending with one
    # directive, so the last match is the authoritative answer.
    last = matches[-1]
    action_raw = last.group(1)
    if action_raw not in ("rebase-and-retry", "abandon-task", "rewrite"):
        return ConflictResolution()
    action: ConflictAction = cast("ConflictAction", action_raw)

    guidance = ""
    if action == "rewrite":
        # Everything before the directive's start position is guidance.
        guidance = critic_response[: last.start()].strip()
    return ConflictResolution(action=action, rewrite_guidance=guidance)


async def _escalate_conflict_to_critic(
    orch: "Orchestrator",
    task: Task,
    worktree: Path,
    conflict_diff: str,
    already_applied_diff: str = "",
    conflict_files: list[str] | None = None,
) -> ConflictResolution:
    """Invoke ``critic_sounding_board`` in CONFLICT ESCALATION MODE.

    Builds a :class:`DelegationEnvelope` carrying a ``CONFLICT_CONTEXT:``
    block (failing task id, conflict file paths, both diffs) so the
    prompt's gated CONFLICT ESCALATION MODE section activates. Parses
    the response via :func:`_parse_conflict_resolution`.

    Returns a :class:`ConflictResolution`. The caller (typically the
    worker after ``apply_patch_to_main`` raises) branches on
    ``resolution.action``:

    * ``rebase-and-retry`` → retry apply with ``three_way=True``.
    * ``abandon-task`` → mark the task blocked.
    * ``rewrite`` → re-invoke the developer with
      ``resolution.rewrite_guidance`` injected as context.
    """
    files_block = "\n".join(
        f"  - {f}" for f in (conflict_files or [str(worktree)])
    )
    conflict_context = (
        "CONFLICT_CONTEXT:\n"
        f"failing_task_id: {task.id}\n"
        f"conflict_files:\n{files_block}\n"
        "already_applied_diff: |\n"
        f"{_indent_block(already_applied_diff)}\n"
        "attempted_diff: |\n"
        f"{_indent_block(conflict_diff)}\n"
    )

    env = DelegationEnvelope(
        task_id=task.id,
        target_agent="critic_sounding_board",
        action="critique",
        acceptance=(
            "End your response with exactly one RESOLUTION: directive "
            "(rebase-and-retry / abandon-task / rewrite)."
        ),
        context={
            "task_id": task.id,
            "conflict_context_marker": True,
        },
    )
    result = await delegate(
        orch,
        "critic_sounding_board",
        env,
        extra_context=conflict_context,
    )
    return _parse_conflict_resolution(result.text or "")


# v0.15.0: stuck-recovery escalation parser + helper. Mirrors the conflict
# escalation pattern above so the structural code shape is consistent
# across both gated critic-escalation modes.

StuckAction = Literal["refine", "pivot", "soft-blocker"]


@dataclass
class StuckResolution:
    """Parsed result from ``critic_sounding_board`` in STUCK RECOVERY MODE.

    Attributes:
        action: One of ``refine`` / ``pivot`` / ``soft-blocker``.
            Defaults to ``refine`` (the least-disruptive fallback —
            matches the documented prompt fallback).
        guidance: Free-form text from the critic intended for the next
            developer attempt (or, in the soft-blocker case, the
            description of what the human needs to decide). Empty when
            the parser could not extract guidance.
    """

    action: StuckAction = "refine"
    guidance: str = ""


# Regex matching the trailing ``RESOLUTION:`` directive on its own line.
# Mirrors :data:`_CONFLICT_DIRECTIVE_RE` shape.
_STUCK_DIRECTIVE_RE = re.compile(
    r"^RESOLUTION:\s*(refine|pivot|soft-blocker)\s*$",
    re.MULTILINE,
)


def _parse_stuck_resolution(critic_response: str) -> StuckResolution:
    """Extract a :class:`StuckResolution` from the critic's text.

    Looks for a ``RESOLUTION: <action>`` line, anchored to its own line.
    On parse failure (no directive found, multiple-but-final-is-invalid,
    etc.) returns ``StuckResolution(action="refine")`` so the caller
    safely falls through to the least-disruptive next step.

    Captures everything BEFORE the matched directive line as ``guidance``
    (stripped of trailing whitespace). The guidance is what the
    orchestrator passes back to the developer when re-invoking (refine /
    pivot) or surfaces in the blocked_reason text (soft-blocker).
    """
    if not critic_response:
        return StuckResolution()

    matches = list(_STUCK_DIRECTIVE_RE.finditer(critic_response))
    if not matches:
        return StuckResolution()
    last = matches[-1]
    action_raw = last.group(1)
    if action_raw not in ("refine", "pivot", "soft-blocker"):
        return StuckResolution()
    action: StuckAction = cast("StuckAction", action_raw)

    guidance = critic_response[: last.start()].strip()
    return StuckResolution(action=action, guidance=guidance)


# v0.26.1 patch G: architect-consult parser + helper. The directive set
# is distinct from the critic's STUCK RECOVERY MODE ("refine" / "pivot"
# / "soft-blocker"). The architect emits ONE of three outcomes:
#
# * ``refine-tasks`` — bullet list of sub-tasks. Orchestrator injects
#   them via the existing ``parse_corrective_direction`` pipeline.
# * ``infrastructure`` — environment / tooling failure; orchestrator
#   flags ``escalated_infra`` and falls through to SOFT_BLOCKER.
# * ``continue`` — the developer was on the right track; reset the
#   retry budget once and let it try again.

ArchitectAction = Literal[
    "architect-refine",
    "architect-infra",
    "architect-continue",
]


@dataclass
class ArchitectResolution:
    """Parsed result from ``architect_b`` in CONSULT MODE.

    Attributes:
        action: One of ``architect-refine`` / ``architect-infra`` /
            ``architect-continue``. Defaults to ``architect-infra`` —
            the least-trusting fallback if the parser cannot extract a
            directive (treats an unparseable response as an environment
            problem and routes to human action via the SOFT_BLOCKER
            follow-up).
        guidance: Free-form text from the architect. For
            ``architect-refine`` this is the bullet list passed to
            :func:`parse_corrective_direction`. For ``architect-infra``
            it's the one-line diagnosis surfaced as ``blocked_reason``.
            For ``architect-continue`` it's the approval line surfaced
            in the next developer's ``last_issues`` context.
    """

    action: ArchitectAction = "architect-infra"
    guidance: str = ""


# Regex matching the trailing ``RESOLUTION: <action>`` directive on its
# own line. The architect emits ``refine-tasks`` / ``infrastructure`` /
# ``continue`` (without the ``architect-`` prefix); the parser maps
# them onto the typed ``ArchitectAction`` values.
_ARCHITECT_DIRECTIVE_RE = re.compile(
    r"^RESOLUTION:\s*(refine-tasks|infrastructure|continue)\s*$",
    re.MULTILINE,
)
_ARCHITECT_ACTION_MAP: dict[str, ArchitectAction] = {
    "refine-tasks": "architect-refine",
    "infrastructure": "architect-infra",
    "continue": "architect-continue",
}


def _parse_architect_resolution(architect_response: str) -> ArchitectResolution:
    """Extract an :class:`ArchitectResolution` from the architect's text.

    Looks for a ``RESOLUTION: <action>`` line, anchored to its own line.
    On parse failure (no directive, unrecognised action, etc.) returns
    ``ArchitectResolution(action="architect-infra", guidance=<raw response>)``
    — the most conservative fallback that routes to human action rather
    than silently retrying.

    The guidance captured is everything FOLLOWING the matched directive
    line. The architect_b_consult.md prompt format places the
    actionable content (corrective sub-task bullets / one-line
    diagnosis / one-line approval) AFTER the directive — distinct from
    the critic's STUCK RECOVERY MODE format where the analysis lives
    BEFORE the directive. The full response is preserved as guidance on
    parse failure so operators can inspect what the architect said.
    """
    if not architect_response:
        return ArchitectResolution(action="architect-infra", guidance="")

    matches = list(_ARCHITECT_DIRECTIVE_RE.finditer(architect_response))
    if not matches:
        return ArchitectResolution(
            action="architect-infra",
            guidance=architect_response.strip(),
        )
    last = matches[-1]
    action_raw = last.group(1)
    action = _ARCHITECT_ACTION_MAP.get(action_raw)
    if action is None:
        return ArchitectResolution(
            action="architect-infra",
            guidance=architect_response.strip(),
        )

    # Guidance lives AFTER the directive line (architect prompt format).
    guidance = architect_response[last.end():].strip()
    return ArchitectResolution(action=action, guidance=guidance)


async def _escalate_stuck_to_architect(
    orch: "Orchestrator",
    task: Task,
    *,
    stuck_state: object,
    ladder_step: str,
    recent_evidence: str = "",
    prior_attempts: list[str] | None = None,
    typed_errors: list[str] | None = None,
    web_search_summary: str = "",
    reviewer_feedback: str = "",
) -> ArchitectResolution:
    """v0.26.1 patch G: invoke ``architect_b`` in CONSULT MODE.

    Mirrors :func:`_escalate_stuck_to_critic` but targets the architect
    (the agent that designed the plan in the first place) and uses a
    structurally different directive set (``refine-tasks`` /
    ``infrastructure`` / ``continue``). The consult-mode prompt is
    injected via ``extra_context`` since the existing :func:`delegate`
    flow does not support per-call prompt swapping.

    Args:
        orch: Orchestrator instance.
        task: Failing task whose autonomous budget is exhausted.
        stuck_state: :class:`StuckState` for the task — counters surface
            in the ARCHITECT_CONTEXT block.
        ladder_step: Always ``"ARCHITECT_CONSULT"`` for the canonical
            entry point. Stamped into the context block for forensics.
        recent_evidence: Freshest excerpt of failing-test / adapter
            output. Empty string allowed.
        prior_attempts: Optional list of one-line summaries of the
            most recent coder attempts.
        typed_errors: Optional list of typed error signatures
            (``qa_gate_encoding_error`` / ``error_max_turns`` / etc.)
            extracted from prior worker exceptions or adapter results.
        web_search_summary: Optional rendered summary of WEB_SEARCH rung
            results from the prior escalations. Empty when none.
        reviewer_feedback: Most recent reviewer output if any.

    Returns the parsed :class:`ArchitectResolution`. On any delegate /
    parse failure the result falls back to
    ``ArchitectResolution(action="architect-infra", guidance=<raw>)``,
    which causes the caller to route to SOFT_BLOCKER with the typed
    flag — the conservative "we asked, and we did not get a usable
    answer" path.
    """
    # Load the consult-mode prompt once; failure to read it is a
    # build-time bug (the file ships in the package) but we degrade
    # safely if a downstream operator has removed it.
    try:
        from agents import load_prompt as _load_prompt

        consult_prompt = _load_prompt("architect_b_consult")
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.architect_consult_prompt_load_failed",
            err=str(exc),
        )
        consult_prompt = ""

    # Indented YAML-ish prior_attempts block; empty when no attempts.
    if prior_attempts:
        attempts_block = "\n".join(f"  - {a}" for a in prior_attempts)
    else:
        attempts_block = "  - (no prior attempts recorded)"

    typed_errors_block = (
        "\n".join(f"  - {e}" for e in typed_errors) if typed_errors else "  - (none)"
    )

    discard_count = int(getattr(stuck_state, "discard_count", 0))
    pivot_count = int(getattr(stuck_state, "pivot_count", 0))
    search_count = int(getattr(stuck_state, "search_count", 0))
    architect_count = int(getattr(stuck_state, "architect_count", 0))
    last_event = str(getattr(stuck_state, "last_event", "") or "")

    # Surface the original task definition verbatim so the architect
    # does not need to re-derive intent from the failing diffs.
    task_definition_block = (
        f"  title: {task.title}\n"
        f"  description: |\n{_indent_block(task.description, prefix='    ')}\n"
        f"  files: {list(task.files)}\n"
        f"  acceptance:\n"
        + (
            "\n".join(f"    - {ac.description}" for ac in task.acceptance)
            if task.acceptance
            else "    - (none declared)"
        )
        + "\n"
    )

    architect_context = (
        (consult_prompt + "\n\n---\n\n" if consult_prompt else "")
        + "ARCHITECT_CONTEXT:\n"
        f"failing_task_id: {task.id}\n"
        f"discard_count: {discard_count}\n"
        f"pivot_count: {pivot_count}\n"
        f"search_count: {search_count}\n"
        f"architect_count: {architect_count}\n"
        f"last_event: {last_event}\n"
        f"ladder_step: {ladder_step}\n"
        f"task_definition:\n{task_definition_block}"
        f"developer_attempts:\n{attempts_block}\n"
        f"typed_errors:\n{typed_errors_block}\n"
        "reviewer_feedback: |\n"
        f"{_indent_block(reviewer_feedback)}\n"
        "web_search_summary: |\n"
        f"{_indent_block(web_search_summary)}\n"
        "recent_evidence: |\n"
        f"{_indent_block(recent_evidence)}\n"
    )

    env = DelegationEnvelope(
        task_id=task.id,
        target_agent="architect_b",
        action="consult",
        acceptance=(
            "End your response with exactly one RESOLUTION: directive "
            "(refine-tasks / infrastructure / continue)."
        ),
        context={
            "task_id": task.id,
            "architect_consult_marker": True,
            "ladder_step": ladder_step,
        },
    )
    try:
        result = await delegate(
            orch,
            "architect_b",
            env,
            extra_context=architect_context,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.architect_consult_delegate_failed",
            task_id=task.id,
            err=str(exc),
        )
        return ArchitectResolution(
            action="architect-infra",
            guidance=f"architect_b delegate raised: {exc}",
        )

    return _parse_architect_resolution(result.text or "")


def _dump_architect_consult_envelope(
    orch: "Orchestrator",
    task: Task,
    *,
    reason: str,
    prior_attempts: list[str] | None,
    recent_evidence: str,
) -> None:
    """v0.38.0 HK3: dump the ARCHITECT_CONSULT envelope to ``.autodev/debug/``.

    Mirrors :meth:`adapters.claude_code.ClaudeCodeAdapter._dump_empty_result`
    and :func:`orchestrator.plan_phase._persist_failed_architect_plan`
    so post-mortems can reconstruct what the architect was asked to
    consult on without tailing the orchestrator stdout. Filename:
    ``architect_consult-{task_id}-{unix_ms}.json`` — pass-num-orderable
    and grep-friendly.

    Gated on :attr:`AutodevConfig.dump_architect_consult_envelopes`
    (default True). Tolerates I/O / serialization errors silently —
    forensics is observability, not correctness.
    """
    if not bool(getattr(orch.cfg, "dump_architect_consult_envelopes", True)):
        return
    try:
        import json
        import time

        from state.paths import debug_dir

        target_dir = debug_dir(orch.cwd)
        target_dir.mkdir(parents=True, exist_ok=True)
        ts_ms = int(time.time() * 1000)
        safe_id = task.id.replace("/", "_").replace(" ", "_")
        target = target_dir / f"architect_consult-{safe_id}-{ts_ms}.json"
        iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        payload = {
            "note": (
                "ARCHITECT_CONSULT envelope dispatched to architect_b "
                "via _dispatch_architect_consult; replay-only forensics"
            ),
            "task_id": task.id,
            "phase_id": task.phase_id,
            "reason": reason,
            "prior_attempts": list(prior_attempts or []),
            "recent_evidence": recent_evidence,
            "timestamp": iso,
        }
        target.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 — defensive: never mask consult
        logger.warning(
            "execute_phase.architect_consult_dump_failed",
            task_id=task.id,
            err=str(exc),
        )


# v0.38.0 I3 (HK5): plan-global corrective ceiling diagnostics.
# ``skip_corrective_count`` lives on ``Phase.metadata`` and is bumped
# each time a corrective round takes the ``skip_corrective_round``
# branch — both the architect-refine cap-hit path AND the phase-review
# cap-hit path. It resets to 0 on a successful corrective injection.
# When the counter reaches ``_SKIP_CORRECTIVE_LOOP_THRESHOLD`` (3) the
# orchestrator emits the diagnostic-only ``skip_corrective_loop_suspected``
# warning + ``skip_corrective_loop_detected`` ledger op. v0.38.0 does
# NOT auto-soft-block on this — the goal is to gather frequency data
# before locking in a recovery policy.
_SKIP_CORRECTIVE_LOOP_THRESHOLD = 3


async def _bump_skip_corrective_counter(
    orch: "Orchestrator",
    *,
    phase_id: str,
    cap_action: str,
) -> None:
    """Increment the per-phase ``skip_corrective_count`` counter under
    the plan_manager's lock so concurrent observers always see the
    persisted value. Emits the loop-suspected warning + ledger op when
    the counter crosses :data:`_SKIP_CORRECTIVE_LOOP_THRESHOLD`.

    Defensive: a failure here must never mask the upstream cap-hit
    decision (the executor still proceeds to its terminal-skip
    handling). All exceptions log + swallow.
    """
    try:
        plan = await orch.plan_manager.load()
        if plan is None:
            return
        phase = next((p for p in plan.phases if p.id == phase_id), None)
        if phase is None:
            return
        prior = int((phase.metadata or {}).get("skip_corrective_count", 0))
        new_count = prior + 1
        await orch.plan_manager.update_phase_meta(
            phase_id,
            metadata={"skip_corrective_count": new_count},
        )
        if new_count >= _SKIP_CORRECTIVE_LOOP_THRESHOLD:
            logger.warning(
                "execute_phase.skip_corrective_loop_suspected",
                phase_id=phase_id,
                count=new_count,
                action=cap_action,
            )
            try:
                await orch.plan_manager.ledger_append(
                    op="skip_corrective_loop_detected",
                    payload={
                        "phase_id": phase_id,
                        "count": new_count,
                        "action": cap_action,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.ledger_append_failed",
                    op="skip_corrective_loop_detected",
                    err=str(exc),
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.skip_corrective_counter_bump_failed",
            phase_id=phase_id,
            err=str(exc),
        )


async def _reset_skip_corrective_counter(
    orch: "Orchestrator",
    *,
    phase_id: str,
) -> None:
    """Reset ``Phase.metadata['skip_corrective_count']`` to 0 after a
    successful corrective round. No-op when the counter is already 0
    (avoids a redundant ledger entry on the happy path)."""
    try:
        plan = await orch.plan_manager.load()
        if plan is None:
            return
        phase = next((p for p in plan.phases if p.id == phase_id), None)
        if phase is None:
            return
        prior = int((phase.metadata or {}).get("skip_corrective_count", 0))
        if prior == 0:
            return
        await orch.plan_manager.update_phase_meta(
            phase_id,
            metadata={"skip_corrective_count": 0},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.skip_corrective_counter_reset_failed",
            phase_id=phase_id,
            err=str(exc),
        )


async def _dispatch_architect_consult(
    orch: "Orchestrator",
    task: Task,
    *,
    stuck_state: object,
    reason: str,
    prior_attempts: list[str] | None,
    web_context_block: str,
) -> Task | None:
    """v0.26.1 patch G: drive the ARCHITECT_CONSULT rung end-to-end.

    Increments the per-task ``architect_count`` (so the next ladder step
    routes to SOFT_BLOCKER), invokes :func:`_escalate_stuck_to_architect`,
    applies the parsed :class:`ArchitectResolution`, and returns the
    updated :class:`Task` (or ``None`` if the dispatch must fall through
    to the legacy retry path — e.g. the delegate raised before the
    architect could weigh in).

    The three return paths:

    * ``architect-refine`` — corrective sub-tasks injected via
      :meth:`PlanManager.append_corrective_tasks`. The failing task is
      transitioned to ``skipped`` with ``metadata.reason =
      "architect_consult_refine_replacement"``. Task is returned.
    * ``architect-infra`` — task marked ``escalated`` + ``blocked``
      (mirrors the soft-blocker path). The ``blocked_reason`` carries
      the architect's one-line diagnosis with an ``architect_consult:``
      prefix and an ``infrastructure`` flag in the metadata.
    * ``architect-continue`` — retry budget reset; task transitioned to
      ``in_progress`` so the outer loop picks it up again. Resolution's
      guidance is appended to the next developer ``last_issues``.
    """
    from orchestrator.corrective_parser import parse_corrective_direction

    # 1) Bump the counter under lock + emit the audit ledger op so the
    # ladder's one-shot accounting holds across crash / replay.
    await orch.plan_manager.increment_architect_consult(task.id)

    # 2) Invoke the architect in consult mode.
    # v0.37.0 H1: fold reviewer/test/developer raw_response bodies into
    # the evidence block so the architect can refine on substance, not
    # just the verdict token.
    recent_evidence = await _build_recent_evidence_block(
        orch, task, reason, web_context_block
    )
    # v0.38.0 HK3: snapshot the consult envelope to .autodev/debug/ so
    # post-mortems can reconstruct the architect's input without
    # re-running the orchestrator.
    _dump_architect_consult_envelope(
        orch,
        task,
        reason=reason,
        prior_attempts=prior_attempts,
        recent_evidence=recent_evidence,
    )
    arch_resolution = await _escalate_stuck_to_architect(
        orch,
        task,
        stuck_state=stuck_state,
        ladder_step="ARCHITECT_CONSULT",
        recent_evidence=recent_evidence,
        prior_attempts=prior_attempts,
    )

    # 3) Always log the architect_consult ledger op, regardless of the
    # action taken — operators want a single grep target for "did the
    # consult fire on this task?".
    try:
        await orch.plan_manager.ledger_append(
            op="architect_consult",
            payload={
                "task_id": task.id,
                "reason": reason,
                "action": arch_resolution.action,
                "architect_response_excerpt": (arch_resolution.guidance or "")[:500],
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.ledger_append_failed",
            op="architect_consult",
            err=str(exc),
        )

    # 4) Branch on the resolution action.
    if arch_resolution.action == "architect-refine":
        # Inject corrective sub-tasks via the existing pipeline.
        phase_id = task.phase_id
        # v0.37.0 H2: compute the phase's remaining corrective-task budget
        # BEFORE parsing so plan inflation observed in real-world runs
        # cannot bypass the cap by emitting a long bullet list in a
        # single architect-refine round. The budget is cumulative across
        # all corrective rounds for this phase.
        # v0.37.0 H5: auto-scale on huge repos via the knob-keyed
        # multiplier dict (effective value emitted as
        # ``huge_repo_multiplier_applied`` telemetry op). Falls back to
        # the base value when the orchestrator stub omits ``_cwd``
        # (unit-test fakes) so test fixtures predating H5 keep working.
        cap_base = int(
            getattr(orch.cfg, "max_corrective_tasks_per_phase", 8)
        )
        _cwd_for_h5 = getattr(orch, "_cwd", None)
        if _cwd_for_h5 is not None:
            from orchestrator.huge_repo_overrides import apply_and_log_huge_repo_value

            cap_eff = await apply_and_log_huge_repo_value(
                key="max_corrective_tasks_per_phase",
                base_value=float(cap_base),
                cwd=_cwd_for_h5,
                cfg=orch.cfg,
                ledger_append=orch.plan_manager.ledger_append,
            )
            cap = int(round(cap_eff))
        else:
            cap = cap_base
        cap_action = str(
            getattr(orch.cfg, "corrective_cap_action", "soft_block_phase")
        )
        # v0.38.0 I3: plan-scope cap is read directly off the config
        # (not currently auto-scaled inline because the per-knob H5
        # resolver fires on the per-phase key; the plan-scope multiplier
        # rides on the same dict entry but applies at config-resolution
        # time for operator-configured values). Default 24 keeps three
        # 8-task phases of headroom on a normal repo.
        plan_cap = int(
            getattr(orch.cfg, "max_corrective_tasks_per_plan", 24)
        )
        base_task_count = 0
        phase_corrective_count = 0
        total_plan_corrective = 0
        try:
            plan = await orch.plan_manager.load()
            if plan is not None:
                phase = next(
                    (p for p in plan.phases if p.id == phase_id), None
                )
                if phase is not None:
                    base_task_count = len(phase.tasks)
                    phase_corrective_count = len(phase.corrective_task_ids or [])
                total_plan_corrective = sum(
                    len(p.corrective_task_ids or []) for p in plan.phases
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.architect_refine_load_failed",
                task_id=task.id,
                err=str(exc),
            )
        per_phase_remaining = max(0, cap - phase_corrective_count)
        per_plan_remaining = max(0, plan_cap - total_plan_corrective)
        remaining_budget = min(per_phase_remaining, per_plan_remaining)
        cap_scope = "plan" if per_plan_remaining < per_phase_remaining else "phase"
        binding_cap = plan_cap if cap_scope == "plan" else cap

        if remaining_budget == 0:
            # Cap reached: operator-recoverable soft block (default) or
            # silent skip, controlled by ``corrective_cap_action``.
            # v0.38.0 I3: ``cap_scope`` records WHICH ceiling fired (the
            # plan-wide budget or the per-phase budget) so dashboards can
            # attribute the cap-hit. The smaller remaining budget wins;
            # ties go to "phase" (the older ceiling).
            logger.info(
                "execute_phase.corrective_cap_reached",
                phase_id=phase_id,
                task_id=task.id,
                cap=binding_cap,
                scope=cap_scope,
                action=cap_action,
                site="architect_refine",
            )
            try:
                await orch.plan_manager.ledger_append(
                    op="corrective_cap_reached",
                    payload={
                        "phase_id": phase_id,
                        "task_id": task.id,
                        "cap": binding_cap,
                        "scope": cap_scope,
                        "action": cap_action,
                        "site": "architect_refine",
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.ledger_append_failed",
                    op="corrective_cap_reached",
                    err=str(exc),
                )
            if cap_action == "soft_block_phase":
                if cap_scope == "plan":
                    cap_action_text = (
                        f"Plan reached the plan-wide correction-task cap "
                        f"({binding_cap}); manual triage required."
                    )
                    cap_reason_text = (
                        f"corrective_cap_reached: plan hit the "
                        f"{binding_cap}-task plan-wide budget"
                    )
                else:
                    cap_action_text = (
                        f"Phase {phase_id} reached the correction-task cap "
                        f"({binding_cap}); manual triage required."
                    )
                    cap_reason_text = (
                        f"corrective_cap_reached: phase {phase_id} "
                        f"hit the {binding_cap}-task budget"
                    )
                cap_hint = _build_recovery_hint(
                    task_id=task.id,
                    hint_class="user_decision_required",
                    action=cap_action_text,
                    commands=[
                        f"autodev requeue --task {task.id}",
                        f"autodev rewind --to-phase {phase_id}",
                    ],
                )
                try:
                    return await block_task(
                        orch,
                        task,
                        failure_class=_fcls.UNKNOWN,
                        raw_error=cap_reason_text,
                        meta={
                            "blocked_reason": cap_reason_text,
                            "architect_consult_action": "cap_reached",
                            "recovery_hint": cap_hint,
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.corrective_cap_soft_block_failed",
                        task_id=task.id,
                        err=str(exc),
                    )
                    return task
            # ``skip_corrective_round``: drop silently and fall through
            # to the existing terminal-skip handling so the executor
            # treats this round as a no-op corrective pass.
            # v0.38.0 I3 (HK5): track skip frequency on Phase.metadata to
            # surface stuck loops via the diagnostic-only
            # ``skip_corrective_loop_detected`` warning + ledger op.
            await _bump_skip_corrective_counter(
                orch, phase_id=phase_id, cap_action=cap_action
            )
            return task

        try:
            corrective_tasks = parse_corrective_direction(
                arch_resolution.guidance,
                phase_id=phase_id,
                base_task_count=base_task_count,
                phase_complexity=task.complexity,
                max_tasks=remaining_budget,
            )
            if corrective_tasks:
                # v0.37.0 H2: pass the cap to plan_manager for
                # defence-in-depth even though we already truncated
                # upstream.
                # v0.38.0 I3: pass the plan-scope cap too so the
                # defensive layer can fire ``scope="plan"`` on bypass.
                await orch.plan_manager.append_corrective_tasks(
                    phase_id,
                    corrective_tasks,
                    max_corrective_tasks_per_phase=cap,
                    max_corrective_tasks_per_plan=plan_cap,
                )
                # v0.38.0 I3 (HK5): a successful corrective injection
                # resets the skip-loop counter so we only warn on
                # consecutive skips.
                await _reset_skip_corrective_counter(orch, phase_id=phase_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.architect_refine_inject_failed",
                task_id=task.id,
                err=str(exc),
            )
        # Mark the failing task as ``skipped`` so the executor moves on
        # to the corrective sub-tasks. (``superseded`` is not a valid
        # TaskStatus — we use ``skipped`` with a typed metadata reason
        # per the v0.26.1 plan's risk mitigation.)
        try:
            return await orch.plan_manager.update_task_status(
                task.id,
                "skipped",
                meta={
                    "blocked_reason": (
                        f"architect_consult: refined into corrective sub-tasks "
                        f"(see phase {phase_id})"
                    ),
                    "architect_consult_action": "refine",
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.architect_refine_skip_failed",
                task_id=task.id,
                err=str(exc),
            )
            return task

    if arch_resolution.action == "architect-infra":
        # Mark escalated + blocked with the typed infra flag. Mirrors
        # the SOFT_BLOCKER outcome path so downstream operators see a
        # consistent shape, with the ``escalated_infra`` marker in
        # metadata distinguishing the architect-diagnosed case from the
        # plain soft-blocker. v0.29.0 Bug 6: stamp typed
        # ``block_reason_class="infrastructure"`` — the architect
        # explicitly diagnosed this as outside-the-loop, so operators
        # can safely select it via ``autodev requeue --infrastructure``.
        await orch.plan_manager.mark_escalated(task.id)
        diagnosis = arch_resolution.guidance or "architect diagnosed infrastructure failure"
        # v0.32.0 (Phase 5, Gap G): structured recovery hint mirroring
        # the architect's diagnosis text.
        infra_hint = _build_recovery_hint(
            task_id=task.id,
            hint_class="network_transient",
            action=(
                f"Architect diagnosed an infrastructure failure: "
                f"{diagnosis[:200]}. Refresh credentials/connection and "
                "`autodev resume`."
            ),
            commands=[
                "autodev doctor",
                f"autodev requeue --task {task.id} --infrastructure",
                "autodev resume",
            ],
        )
        return await block_task(
            orch,
            task,
            failure_class=_fcls.INFRA_CIRCUIT_OPEN,
            raw_error=f"architect_consult: infrastructure: {diagnosis}",
            meta={
                "blocked_reason": f"architect_consult: infrastructure: {diagnosis}",
                "architect_consult_action": "infrastructure",
                "escalated_infra": True,
                "block_reason_class": "infrastructure",
                "recovery_hint": infra_hint,
            },
        )

    if arch_resolution.action == "architect-continue":
        # Reset the developer's retry budget once and put the task back
        # into ``in_progress`` so the outer loop picks it up. The
        # ``update_task_status`` meta carries the retry reset; the
        # caller's ``last_issues`` is appended on the next iteration.
        target_status = "in_progress" if task.status != "in_progress" else task.status
        await orch.plan_manager.update_task_status(
            task.id,
            target_status,
            meta={
                "retry_count": 0,
                "architect_consult_action": "continue",
            },
        )
        return await orch.plan_manager.get_task(task.id) or task

    # Defensive default — should be unreachable given the parser's
    # exhaustive fallback. Treat as infrastructure so the operator sees
    # the architect's raw output and can act.
    # v0.32.0 (Phase 5, Gap G): user_decision_required hint — the
    # architect produced text the parser couldn't structure.
    unparseable_hint = _build_recovery_hint(
        task_id=task.id,
        hint_class="user_decision_required",
        action=(
            "Architect consult returned unparseable text. Inspect "
            ".autodev/debug/architect-consult-*.txt and decide whether "
            "to retry the consult, narrow the task, or skip."
        ),
    )
    return await block_task(
        orch,
        task,
        failure_class=_fcls.UNKNOWN,
        raw_error=(
            f"architect_consult: unparseable response — {(arch_resolution.guidance or '')[:200]}"
        ),
        meta={
            "blocked_reason": (
                f"architect_consult: unparseable response — {(arch_resolution.guidance or '')[:200]}"
            ),
            "architect_consult_action": "unparseable",
            "recovery_hint": unparseable_hint,
        },
    )


# ---------------------------------------------------------------------------
# ADR-0047: Universal Blocker Resolver chokepoint (Part B wiring).
#
# Every terminal failure site routes its blocker through ``_maybe_resolve_blocker``
# BEFORE it blocks/degrades. The resolver (orchestrator.blocker_resolver) picks a
# bounded action; ``_apply_resolution`` maps that action onto the SAME recovery
# primitives the architect-consult rung already uses (continue = retry-reset;
# refine = corrective injection). FAIL-SAFE invariant: when the resolver is
# disabled (cfg.resolver.enabled is False / ``AUTODEV_RESOLVER_DISABLED``), errors,
# or declines (``fall_through`` / ``ask_human`` / no usable recovery), the helper
# returns ``None`` and the caller does EXACTLY its legacy block/degrade. The helper
# NEVER raises. Loop-safety is enforced inside ``resolve_blocker`` (per-blocker
# cycle budget + ladder advancement via ``recovery_already_tried``).
# ---------------------------------------------------------------------------

# The bounded action vocabulary surfaced to the resolver as context. Mirrors
# state.schemas.ResolutionActionType (informational for the LLM path).
_DEFAULT_RESOLVER_ACTIONS: tuple[str, ...] = (
    "retry_with_changes",
    "split_task",
    "narrow_scope",
    "re_architect",
    "re_plan",
    "reroute",
    "repair_environment",
    "relax_constraint",
    "escalate_budget",
    "escalate_model",
    "soft_pass_with_evidence",
    "consult_knowledge",
    "web_search",
    "ask_human",
    "fall_through",
)


def _prior_resolution_actions(
    orch: "Orchestrator", task_id: str | None, fclass: str
) -> list[str]:
    """Resume-safe: the resolver actions already applied for this blocker key.

    Read from the ledger (``resolution_chosen`` ops) so the deterministic ladder
    advances across retries / process restarts instead of re-picking its first
    rung forever. Mirrors the key used by ``blocker_resolver.blocker_key``.
    Best-effort — any failure yields ``[]`` (the per-blocker cycle budget is a
    separate hard stop, so an empty list cannot cause an unbounded loop).
    """
    if task_id is None:
        return []
    try:
        from state.ledger import read_entries

        key = f"{task_id}:{fclass}"
        out: list[str] = []
        for entry in read_entries(orch.cwd):
            if entry.op != "resolution_chosen":
                continue
            payload = entry.payload if isinstance(entry.payload, dict) else {}
            if payload.get("blocker_key") == key:
                act = payload.get("action")
                if isinstance(act, str):
                    out.append(act)
        return out
    except Exception:  # noqa: BLE001 - audit read must never block recovery
        return []


async def _resolver_retry(
    orch: "Orchestrator", task: Task, *, note: str
) -> Task | None:
    """Re-enable ``task`` for another attempt (the architect-continue pattern).

    Resets the developer retry budget and transitions the task back to
    ``in_progress`` so the outer loop re-dispatches it; stamps a typed
    ``resolver_note`` into the task metadata for forensics + next-attempt
    guidance. Returns the refreshed Task, or ``None`` if the transition failed
    (caller falls through to its legacy block).
    """
    try:
        target = "in_progress" if task.status != "in_progress" else task.status
        await orch.plan_manager.update_task_status(
            task.id,
            target,
            meta={
                "retry_count": 0,
                "resolver_action": "retry",
                "resolver_note": note[:500],
            },
        )
        return await orch.plan_manager.get_task(task.id) or task
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.resolver_retry_failed", task_id=task.id, err=str(exc)
        )
        return None


async def _resolver_corrective(
    orch: "Orchestrator", task: Task, direction: str
) -> Task | None:
    """Inject corrective sub-tasks from ``direction`` and skip ``task`` (the
    architect-refine pattern). Returns the skipped Task on success, or ``None``
    (caller falls through to its legacy block) when no direction is usable, no
    plan/phase is loadable, or injection fails.
    """
    if not direction or not direction.strip():
        return None
    try:
        from orchestrator.corrective_parser import parse_corrective_direction

        phase_id = task.phase_id
        cap = int(getattr(orch.cfg, "max_corrective_tasks_per_phase", 8))
        plan_cap = int(getattr(orch.cfg, "max_corrective_tasks_per_plan", 24))
        plan = await orch.plan_manager.load()
        if plan is None:
            return None
        phase = next((p for p in plan.phases if p.id == phase_id), None)
        if phase is None:
            return None
        base_task_count = len(phase.tasks)
        corrective_tasks = parse_corrective_direction(
            direction,
            phase_id=phase_id,
            base_task_count=base_task_count,
            phase_complexity=task.complexity,
            max_tasks=cap,
        )
        if not corrective_tasks:
            return None
        await orch.plan_manager.append_corrective_tasks(
            phase_id,
            corrective_tasks,
            max_corrective_tasks_per_phase=cap,
            max_corrective_tasks_per_plan=plan_cap,
        )
        return await orch.plan_manager.update_task_status(
            task.id,
            "skipped",
            meta={
                "blocked_reason": (
                    f"resolver: refined into corrective sub-tasks "
                    f"(see phase {phase_id})"
                ),
                "resolver_action": "refine",
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.resolver_corrective_failed",
            task_id=task.id,
            err=str(exc),
        )
        return None


async def _apply_resolution(
    orch: "Orchestrator",
    task: Task | None,
    ctx: object,
    action: object,
) -> Task | None:
    """Map a ``ResolutionAction`` onto an existing recovery primitive.

    Returns a non-``None`` Task when the blocker was actively re-enabled (caller
    SKIPS its legacy block), or ``None`` to fall through to the legacy block.
    Conservative by design: the heavyweight / human-decision actions
    (``ask_human``/``fall_through``/``web_search``/``reroute``) fall through; the
    structural re-plan actions only act when a task + direction are available.
    """
    a = getattr(action, "action", "fall_through")
    rationale = (getattr(action, "rationale", "") or "")[:300]
    params = getattr(action, "params", {}) or {}
    if task is None:
        # Plan-level structural site (no task to mutate) — observability only.
        return None
    if a in ("ask_human", "fall_through", "web_search", "reroute"):
        return None
    if a in ("split_task", "narrow_scope", "re_architect", "re_plan"):
        direction = str(params.get("direction") or rationale or "")
        return await _resolver_corrective(orch, task, direction)
    if a == "consult_knowledge":
        try:
            from orchestrator import blocker_resolver as _br

            summary = await _br.consult_knowledge(orch, ctx)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            summary = ""
        note = (
            f"consult_knowledge — prior failures:\n{summary[:800]}"
            if summary
            else f"consult_knowledge: {rationale}"
        )
        return await _resolver_retry(orch, task, note=note)
    if a in (
        "retry_with_changes",
        "escalate_budget",
        "relax_constraint",
        "escalate_model",
        "repair_environment",
        "soft_pass_with_evidence",
    ):
        return await _resolver_retry(orch, task, note=f"{a}: {rationale}")
    # Unknown action string — fail safe to legacy block.
    return None


async def _maybe_resolve_blocker(
    orch: "Orchestrator",
    task: Task | None,
    *,
    failure_class: str,
    raw_error: str = "",
    failing_role: str | None = None,
    phase_id: str | None = None,
    evidence_refs: list[str] | None = None,
) -> Task | None:
    """ADR-0047 chokepoint. Route a terminal blocker through the resolver before
    the caller's legacy block/degrade.

    Returns a non-``None`` Task when the resolver actively recovered the blocker
    (caller MUST use it and SKIP the legacy block); ``None`` otherwise (caller
    does its legacy block unchanged). NEVER raises. The
    ``cfg.resolver.enabled`` + ``AUTODEV_RESOLVER_DISABLED`` gate lives here so a
    disabled resolver is a zero-cost no-op at every call site.
    """
    import os

    try:
        rcfg = getattr(orch.cfg, "resolver", None)
        if rcfg is None or not getattr(rcfg, "enabled", False):
            return None
        if os.environ.get("AUTODEV_RESOLVER_DISABLED", "").strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            return None
    except Exception:  # noqa: BLE001
        return None

    # Loop-safety backstop (B5), independent of the ledger: an in-memory
    # per-(task, failure_class) cap on the orchestrator instance. ``resolve_blocker``
    # also enforces a resume-safe ledger-based budget, but that reads 0 forever
    # when the PlanManager's ledger is a unit-test fake — so a resolver recovery
    # that re-enables a task whose underlying failure persists would loop
    # unboundedly. This counter guarantees the chokepoint stops recovering and
    # falls through to the legacy block after ``max_cycles_per_blocker`` hits,
    # regardless of ledger state.
    try:
        max_cycles = int(getattr(rcfg, "max_cycles_per_blocker", 3))
        guard_key = f"{task.id if task is not None else '-'}:{failure_class}"
        counts = getattr(orch, "_resolver_cycle_counts", None)
        if counts is None:
            counts = {}
            try:
                orch._resolver_cycle_counts = counts  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                counts = {}
        if counts.get(guard_key, 0) >= max_cycles:
            return None
        counts[guard_key] = counts.get(guard_key, 0) + 1
    except Exception:  # noqa: BLE001
        pass

    try:
        from orchestrator import blocker_resolver as _br
        from orchestrator import failure_classes as _fc
        from state.schemas import BlockerContext

        fclass = _fc.classify(failure_class)
        task_id = task.id if task is not None else None
        ph = phase_id
        if ph is None and task is not None:
            ph = getattr(task, "phase_id", None)
        ctx = BlockerContext(
            failure_class=fclass,
            raw_error=(raw_error or "")[:2000],
            failing_role=failing_role,
            task_id=task_id,
            phase_id=ph,
            attempt_history=[],
            recovery_already_tried=_prior_resolution_actions(orch, task_id, fclass),
            evidence_refs=evidence_refs or [],
            available_actions=list(_DEFAULT_RESOLVER_ACTIONS),
        )
        action = await _br.resolve_blocker(orch, ctx)
    except Exception as exc:  # noqa: BLE001 - resolver must never break the loop
        logger.warning(
            "execute_phase.resolver_dispatch_failed",
            failure_class=failure_class,
            err=str(exc),
        )
        return None

    try:
        recovered = await _apply_resolution(orch, task, ctx, action)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.resolver_apply_failed",
            action=getattr(action, "action", None),
            err=str(exc),
        )
        recovered = None

    act_name = getattr(action, "action", "fall_through")
    if recovered is not None:
        outcome = "recovered"
    elif act_name == "ask_human":
        outcome = "ask_human"
    else:
        outcome = "fell_through"
    try:
        await orch.plan_manager.ledger_append(
            op="resolution_outcome",
            payload={
                "task_id": getattr(ctx, "task_id", None),
                "action": act_name,
                "outcome": outcome,
                "reason": (getattr(action, "rationale", "") or "")[:300],
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.ledger_append_failed",
            op="resolution_outcome",
            err=str(exc),
        )
    if recovered is not None:
        logger.info(
            "execute_phase.blocker_resolved",
            task_id=getattr(ctx, "task_id", None),
            failure_class=getattr(ctx, "failure_class", None),
            action=act_name,
        )
    return recovered


async def _escalate_stuck_to_critic(
    orch: "Orchestrator",
    task: Task,
    *,
    stuck_state: object,
    ladder_step: str,
    recent_evidence: str = "",
    prior_attempts: list[str] | None = None,
) -> StuckResolution:
    """Invoke ``critic_sounding_board`` in STUCK RECOVERY MODE.

    Builds a :class:`DelegationEnvelope` carrying a ``STUCK_CONTEXT:``
    block (failing task id, current discard / pivot counts, ladder
    step, prior attempts, freshest evidence) so the prompt's gated
    STUCK RECOVERY MODE section activates. Parses the response via
    :func:`_parse_stuck_resolution`.

    Args:
        orch: Orchestrator instance.
        task: Task whose retries have stalled.
        stuck_state: :class:`orchestrator.escalation_ladder.StuckState`
            for the failing task. Typed ``object`` here to avoid a
            cycle with the ladder module at import time.
        ladder_step: One of ``"REFINE" | "PIVOT" | "SOFT_BLOCKER"`` —
            the ladder's recommendation. Surfaced in the prompt as a
            hint to the critic about which response mode to take.
        recent_evidence: Freshest excerpt of failing-test / reviewer /
            error output. Empty string is allowed.
        prior_attempts: Optional list of one-line summaries of the
            most recent attempts.

    Returns a :class:`StuckResolution`. The caller (typically
    :func:`_try_retry_or_escalate`) branches on ``resolution.action``:

    * ``refine`` → re-invoke the developer with ``resolution.guidance``
      injected as context.
    * ``pivot`` → re-invoke the developer with the radical-direction
      guidance as context (and the orchestrator increments
      ``pivot_count``).
    * ``soft-blocker`` → mark the task blocked with the guidance text
      as the ``blocked_reason``; emit a ``soft_blocker_handoff``
      ledger op for forensics.
    """
    # Indented YAML-ish prior_attempts block; empty when no attempts.
    if prior_attempts:
        attempts_block = "\n".join(f"  - {a}" for a in prior_attempts)
    else:
        attempts_block = "  - (no prior attempts recorded)"

    # ``stuck_state`` is typed ``object`` to avoid a cyclic import at
    # module-load time. We accept anything with the attributes we need.
    discard_count = int(getattr(stuck_state, "discard_count", 0))
    pivot_count = int(getattr(stuck_state, "pivot_count", 0))
    last_event = str(getattr(stuck_state, "last_event", "") or "")

    stuck_context = (
        "STUCK_CONTEXT:\n"
        f"failing_task_id: {task.id}\n"
        f"discard_count: {discard_count}\n"
        f"pivot_count: {pivot_count}\n"
        f"last_event: {last_event}\n"
        f"ladder_step: {ladder_step}\n"
        f"prior_attempts:\n{attempts_block}\n"
        "recent_evidence: |\n"
        f"{_indent_block(recent_evidence)}\n"
    )

    env = DelegationEnvelope(
        task_id=task.id,
        target_agent="critic_sounding_board",
        action="critique",
        acceptance=(
            "End your response with exactly one RESOLUTION: directive "
            "(refine / pivot / soft-blocker)."
        ),
        context={
            "task_id": task.id,
            "stuck_context_marker": True,
            "ladder_step": ladder_step,
        },
    )
    result = await delegate(
        orch,
        "critic_sounding_board",
        env,
        extra_context=stuck_context,
    )
    return _parse_stuck_resolution(result.text or "")


def _indent_block(text: str, prefix: str = "  ") -> str:
    """Indent every line of ``text`` by ``prefix`` for the YAML-ish block.

    Returns the empty string verbatim so the CONFLICT_CONTEXT: block is
    well-formed even when one of the diffs is missing.
    """
    if not text:
        return ""
    return "\n".join(prefix + line for line in text.splitlines())


async def _credit_injected_lessons_for_task(
    orch: "Orchestrator", task_id: str
) -> None:
    """v0.35.0 C1 prerequisite: drain + credit lessons that landed for a task.

    Walks every ``(task_id, role)`` slot the dispatcher populated for
    this task, asks the knowledge store to increment
    ``succeeded_after_count`` for each entry id, and removes the slot
    from the in-memory correlation map so re-entry on a resumed
    session does not double-credit.
    """
    correlation = getattr(orch, "_injected_lessons_by_task", None)
    if correlation is None:
        return
    keys_to_drain = [key for key in list(correlation.keys()) if key[0] == task_id]
    for key in keys_to_drain:
        entry_ids = correlation.pop(key, [])
        if not entry_ids:
            continue
        credit = getattr(orch.knowledge, "credit_task_success", None)
        if credit is None:
            continue
        await credit(entry_ids, task_id=task_id, role=key[1])


# Maximum number of "rewrite" rounds before forcing abandon. Caps the
# critic-developer ping-pong so a misbehaving critic cannot loop
# indefinitely.
_CONFLICT_REWRITE_RETRY_CAP = 2


async def _apply_with_conflict_escalation(
    orch: "Orchestrator",
    task: Task,
    worktree: Path,
    worktree_mgr: WorktreeManager,
) -> bool:
    """Apply the worktree diff to main with critic-escalation on conflict.

    Returns ``True`` if the diff was applied (cleanly OR via 3-way
    merge or after a rewrite round), ``False`` if the task was
    abandoned. On ``False`` the caller transitions the task to
    ``blocked`` (caller-side update_task_status, this helper records
    the blocked reason via the worker contract).

    Branches on the critic's RESOLUTION directive:

    * ``rebase-and-retry`` → re-attempt apply with ``three_way=True``.
      If the 3-way also fails, escalate again (capped by retry cap).
    * ``abandon-task`` → block + cascade-block descendants, return False.
    * ``rewrite`` → re-invoke the developer with the merge guidance
      injected as ``extra_context`` and retry the apply.

    The cap of two rewrite rounds prevents critic-developer ping-pong
    on pathological cases.
    """
    rewrite_rounds = 0
    # v0.25.1 Bug #2: persistent integration. Commit per task so the next
    # task's per-task worktree (created at HEAD) sees prior tasks' changes.
    commit_msg = f"autodev: task {task.id} ({task.title})"
    while True:
        try:
            await worktree_mgr.apply_patch_to_main(
                worktree, base_ref="HEAD", commit_message=commit_msg
            )
            return True
        except WorktreeError as exc:
            logger.warning(
                "execute_phase.apply_patch_conflict",
                task_id=task.id,
                err=str(exc),
            )

            try:
                conflict_diff = await worktree_mgr.get_diff_vs_base(worktree)
            except WorktreeError:
                conflict_diff = ""
            resolution = await _escalate_conflict_to_critic(
                orch,
                task,
                worktree,
                conflict_diff=conflict_diff,
                already_applied_diff="",
                conflict_files=list(task.files),
            )

            if resolution.action == "rebase-and-retry":
                # Try 3-way apply. If THIS also fails, fall through to
                # abandon (no infinite loop on persistent conflicts).
                try:
                    await worktree_mgr.apply_patch_to_main(
                        worktree,
                        base_ref="HEAD",
                        three_way=True,
                        commit_message=commit_msg,
                    )
                    logger.info(
                        "execute_phase.conflict_resolved_3way",
                        task_id=task.id,
                    )
                    return True
                except WorktreeError as exc2:
                    logger.warning(
                        "execute_phase.conflict_3way_failed",
                        task_id=task.id,
                        err=str(exc2),
                    )
                    # v0.41.0 (A3): a failed 3-way apply can leave the main
                    # working tree in a merge-in-progress or partially-applied
                    # state (conflict markers / staged hunks). Restore a clean
                    # tree BEFORE marking the task blocked so those artifacts
                    # don't bleed into the next task's per-task worktree
                    # (created at HEAD of the main repo). Best-effort and
                    # idempotent — never masks the underlying conflict.
                    await worktree_mgr.abort_failed_apply(
                        targets=list(task.files)
                    )
                    await block_task(
                        orch,
                        task,
                        failure_class=_fcls.CONFLICT_3WAY_FAILED,
                        raw_error=f"conflict_escalation:3way_failed: {exc2}",
                        meta={
                            "blocked_reason": f"conflict_escalation:3way_failed: {exc2}"
                        },
                    )
                    return False

            if resolution.action == "abandon-task":
                await block_task(
                    orch,
                    task,
                    failure_class=_fcls.CONFLICT_ABANDON,
                    raw_error="conflict_escalation:abandon",
                    meta={
                        "blocked_reason": "conflict_escalation:abandon"
                    },
                )
                return False

            # rewrite: re-invoke developer with guidance, capped retries.
            if rewrite_rounds >= _CONFLICT_REWRITE_RETRY_CAP:
                logger.warning(
                    "execute_phase.conflict_rewrite_cap_exceeded",
                    task_id=task.id,
                    cap=_CONFLICT_REWRITE_RETRY_CAP,
                )
                await block_task(
                    orch,
                    task,
                    failure_class=_fcls.CONFLICT_REWRITE_CAP_EXCEEDED,
                    raw_error="conflict_escalation:rewrite_cap_exceeded",
                    meta={
                        "blocked_reason": "conflict_escalation:rewrite_cap_exceeded"
                    },
                )
                return False

            rewrite_rounds += 1
            developer_env = _developer_envelope(
                task, extra_issues=[resolution.rewrite_guidance]
            )
            try:
                await delegate(
                    orch,
                    "developer",
                    developer_env,
                    extra_context=resolution.rewrite_guidance,
                    retry_count=task.retry_count,
                    last_issues=[resolution.rewrite_guidance],
                    task=task,
                    cwd_override=worktree,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.conflict_rewrite_developer_failed",
                    task_id=task.id,
                    err=str(exc),
                )
                await block_task(
                    orch,
                    task,
                    failure_class=_fcls.CONFLICT_REWRITE_CAP_EXCEEDED,
                    raw_error=f"conflict_escalation:rewrite_developer_failed: {exc}",
                    meta={
                        "blocked_reason": f"conflict_escalation:rewrite_developer_failed: {exc}"
                    },
                )
                return False
            # Loop and retry apply with the developer's new diff.


async def run_execute_phase(
    orch: "Orchestrator", task_id: str | None = None
) -> list[Task]:
    """Run the execute loop. Returns the list of tasks processed (in order).

    v0.11.0: replaces the serial ``next_pending_task`` walk with a
    DAG-aware worker pool. Within a phase, independent tasks execute
    concurrently up to ``cfg.tournaments.execute_max_parallel_tasks``
    (auto-resolved via the v0.10.0 resource probe when ``None``). Tasks
    with overlapping ``files`` are serialized; failed tasks
    cascade-block their dependents via
    :meth:`PlanManager.mark_blocked_descendants`.

    v0.9.0: when the loop processes the last task in a phase (all tasks
    terminal), the per-phase code-review tournament fires. The tournament
    decides whether to accept the phase as-is (winner=A) or inject
    corrective sub-tasks (winner=B/AB) — corrective tasks land at the end
    of the same phase and execute BEFORE the next phase begins.

    Single-task path (``task_id`` set) bypasses the DAG dispatcher and
    runs the legacy serial path, with a NEW per-task worktree if the
    repo is git-initialized (otherwise falls back to running in
    ``orch.cwd`` directly).
    """
    # v0.26.0: the v0.25.4 InlineAdapter+tournaments preflight check was
    # removed alongside InlineAdapter itself — no inline adapter exists,
    # so no mismatch is possible.
    processed: list[Task] = []

    # v0.42.1 F1: register the resolver chokepoint so ``blocker_guard.block_task``
    # routes every terminal block through ``_maybe_resolve_blocker`` (the
    # "registered at startup" path). ``block_task``'s call-time fallback import
    # covers code paths / tests that do not enter via ``run_execute_phase``.
    try:
        if getattr(orch, "block_hook", None) is None:
            setattr(orch, "block_hook", _maybe_resolve_blocker)
    except Exception:  # noqa: BLE001 - best-effort registration; never fatal
        pass

    if task_id is not None:
        task = await orch.plan_manager.get_task(task_id)
        if task is None:
            raise AutodevError(f"task_id={task_id!r} not found in plan")
        if task.status in ("complete", "skipped"):
            logger.info("execute_phase.skip", task_id=task_id, status=task.status)
            return processed
        # Single-task path: still record baseline_commit on first entry to
        # the phase so a later (whole-loop) run picks up the right
        # baseline. This is idempotent (the meta-update only runs when
        # baseline_commit is unset).
        await _maybe_record_phase_entry(orch, task.phase_id)
        try:
            processed.append(await _execute_one(orch, task))
        except (AuthenticationFailedError, InfrastructureCircuitOpenError) as exc:
            # v0.29.0 Bug 7 / v0.30.0 Bug 5: typed halt on auth failure
            # OR a tripped cross-task infrastructure circuit breaker.
            # Both stamp the offending task as ``quarantined`` (non-
            # terminal so ``Orchestrator.resume`` picks it back up) with
            # the typed reason retained for forensics, log the
            # structured halt event, surface a clear console message,
            # and re-raise so the CLI driver exits non-zero. Do NOT
            # call ``_maybe_run_phase_review`` — the phase must not be
            # force-accepted on the halt path; the aggregator's pause-
            # on-quarantined check would short-circuit anyway, but
            # skipping the call entirely is the belt-and-suspenders form.
            await _halt_task_for_auth_failed(orch, task.id, exc)
            raise
        # Single-task path also triggers review when the targeted task
        # was the last terminal one in its phase.
        await _maybe_run_phase_review(orch, task.phase_id)
        return processed

    # v0.22.2 B3: reconcile evidence-vs-ledger BEFORE the reaper so any
    # orphan evidence (success=true on disk but no coded ledger op) is
    # promoted to coded first — preventing the reaper from reverting a
    # task whose work actually completed (D-3 finding from the
    # 2026-05-09 Unity stall).
    try:
        await orch.plan_manager.reconcile_evidence_vs_ledger()
    except Exception as exc:  # noqa: BLE001 — log + continue
        logger.warning(
            "execute_phase.reconcile_evidence_failed", err=str(exc)
        )

    # v0.22.2 B1: reap orphan in-flight tasks before any dispatch. An
    # interrupted run leaves tasks frozen in non-terminal-non-pending
    # states (``coded``, ``in_progress``, ``reviewed``, etc.) — the
    # dispatcher's pending-only filter cannot pick them up. The reaper
    # reverts orphans to ``pending`` so they re-dispatch fresh. Idempotent
    # (no-op when there are no orphans). After B3 reconciliation runs
    # first, only genuine orphans (no evidence) reach this path.
    try:
        reaped = await orch.plan_manager.reap_orphans()
        if reaped:
            logger.info(
                "execute_phase.reaped_orphans",
                count=len(reaped),
                task_ids=reaped,
            )
    except Exception as exc:  # noqa: BLE001 — log + continue; do not block run
        logger.warning("execute_phase.reap_orphans_failed", err=str(exc))

    # v0.11.0: DAG-aware worker pool over all phases.
    plan = await orch.plan_manager.load()
    if plan is None:
        return processed

    # Resolve worker-pool cap once per run.
    parallelism = _resolve_execute_parallelism(orch)

    # Build a worktree manager rooted under the autodev root. Skip
    # worktree-isolation when the repo is not git-initialized — tests
    # commonly use bare tmp dirs and the legacy serial path applies.
    #
    # v0.21.0 A1: when ``cfg.worktree_pool_enabled`` is True, substitute
    # a :class:`WorktreePool` for the :class:`WorktreeManager`. The pool
    # implements the same ``create_per_task`` / ``remove_per_task`` /
    # ``get_diff_vs_base`` / ``apply_patch_to_main`` surface so the
    # worker (``_execute_one``) is unaware of the substitution. Cold-
    # start happens here so the upfront cost is amortized over the
    # entire phase.
    worktree_mgr: WorktreeManager | None = None
    if _is_git_repo(orch.cwd):
        if getattr(orch.cfg, "worktree_pool_enabled", False):
            from orchestrator.worktree_pool import WorktreePool

            pool = WorktreePool(
                main_repo=orch.cwd,
                pool_dir=autodev_root(orch.cwd) / "execute_worktrees_pool",
                size=parallelism,
            )
            try:
                await pool.cold_start()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.worktree_pool_cold_start_failed",
                    err=str(exc),
                )
            # Duck-type as WorktreeManager (it exposes the same surface).
            worktree_mgr = pool  # type: ignore[assignment]
        else:
            # v0.22.1 A3 + v0.23.0 C1: huge-repo mode resolution.
            # ``"auto"`` keys off ``RepoCapacity.is_huge``; ``"on"``/``"off"``
            # are operator overrides. When huge mode is on we extend the
            # worktree-add timeout (was the bug from 2026-05-09 Unity:
            # 60 s ceiling killed 80-180 s full checkouts).
            _is_huge = bool(
                getattr(getattr(orch, "_repo_capacity", None), "is_huge", False)
            )
            _huge_mode_cfg = getattr(
                orch.cfg, "worktree_huge_repo_mode", "auto"
            )
            if _huge_mode_cfg == "on":
                _wm_huge_mode = True
            elif _huge_mode_cfg == "off":
                _wm_huge_mode = False
            else:  # "auto"
                _wm_huge_mode = _is_huge
            _huge_create_timeout_s = float(
                getattr(orch.cfg, "worktree_huge_create_timeout_s", 600)
            )
            worktree_mgr = WorktreeManager(
                main_repo=orch.cwd,
                tournament_dir=autodev_root(orch.cwd) / "execute_worktrees",
                huge_mode=_wm_huge_mode,
                huge_create_timeout_s=_huge_create_timeout_s,
            )

    try:
        # v0.14.0: validate edit_scope first — a plan that declares a
        # narrowed scope but lists out-of-scope task files is a fail-fast
        # condition. We block every pending task in offending phases (so
        # the run terminates cleanly) and emit a structured warning that
        # includes the offending task id, file, and resolved scope. When
        # ``Plan.edit_scope`` is empty (legacy / no scope declared), the
        # validator is a no-op and the loop proceeds unchanged.
        from orchestrator.dag import (
            DagValidationError,
            EditScopeViolation,
            collect_edit_scope_violations,
            validate_dag_cycles_global,
            validate_dag_undefined_refs,
        )

        # v0.27 Phase 3 (audit §3): per-task scoped block. The v0.26.2
        # behaviour was to block EVERY pending task in EVERY phase on a
        # single violation, which over-reached when only one task was
        # mis-scoped. The collector returns every violation; we block
        # only the offending task ids and emit a granular ledger op
        # per block. The blanket-block fallback fires only when ALL
        # pending tasks across all phases are in the violation set.
        violations = collect_edit_scope_violations(
            plan, tracked_files=getattr(orch, "tracked_files", None)
        )
        if violations:
            violator_ids: set[str] = set()
            for v in violations:
                logger.warning(
                    "execute_phase.edit_scope_violation",
                    err=str(v),
                )
                tid = getattr(v, "task_id", None)
                if isinstance(tid, str):
                    violator_ids.add(tid)
            pending_ids = {
                t.id
                for phase in plan.phases
                for t in phase.tasks
                if t.status == "pending"
            }
            # If every pending task is a violator, preserve the v0.26.2
            # blanket-block contract — there's nothing safe left to run.
            block_all = pending_ids and violator_ids >= pending_ids
            to_block = pending_ids if block_all else violator_ids

            for phase in plan.phases:
                for t in phase.tasks:
                    if t.status != "pending" or t.id not in to_block:
                        continue
                    # Find the specific violation for this task to
                    # carry into the ledger payload (best-effort: a
                    # task may have multiple violations; pick the first).
                    matching = next(
                        (
                            v
                            for v in violations
                            if getattr(v, "task_id", None) == t.id
                        ),
                        None,
                    )
                    msg = (
                        f"edit_scope_violation: {matching}"
                        if matching is not None
                        else f"edit_scope_violation (blanket): {violations[0]}"
                    )
                    try:
                        await block_task(
                            orch,
                            t,
                            failure_class=_fcls.EDIT_SCOPE_VIOLATION,
                            raw_error=msg,
                            meta={"blocked_reason": msg},
                        )
                        await orch.plan_manager.ledger_append(
                            op="task_blocked_scope_violation",
                            payload={
                                "task_id": t.id,
                                "phase_id": getattr(matching, "phase_id", "")
                                or t.phase_id,
                                "file_path": getattr(matching, "file_path", "")
                                or "",
                                "message": str(matching or violations[0]),
                            },
                        )
                    except Exception:  # noqa: BLE001
                        pass
            return processed

        # v0.21.0 B1: when cross-phase parallelism is enabled, ``Task.
        # depends_on`` may legitimately reference tasks in EARLIER phases.
        # The per-phase DAG validator rejects those as "undefined
        # references", so under the cross-phase flag we run a relaxed
        # plan-level DAG check that accepts cross-phase deps as long as
        # every dep resolves SOMEWHERE in the plan.
        if getattr(orch.cfg, "cross_phase_parallelism_enabled", False):
            try:
                _validate_cross_phase_dag(plan)
            except DagValidationError as exc:
                logger.warning(
                    "execute_phase.cross_phase_dag_invalid", err=str(exc)
                )
                # ADR-0047: route the structural blocker through the resolver
                # for observability (re_plan is the recommendation); the plan-
                # level site has no single task to mutate, so it falls through
                # to the legacy block below.
                await _maybe_resolve_blocker(
                    orch,
                    None,
                    failure_class="cross_phase_dag_invalid",
                    raw_error=str(exc),
                )
                for phase in plan.phases:
                    for t in phase.tasks:
                        if t.status == "pending":
                            try:
                                await block_task(
                                    orch,
                                    t,
                                    failure_class=_fcls.CROSS_PHASE_DAG_INVALID,
                                    raw_error=str(exc),
                                    meta={
                                        "blocked_reason": (
                                            f"cross_phase_dag_invalid: {exc}"
                                        )
                                    },
                                )
                            except Exception:  # noqa: BLE001
                                pass
                return processed
        else:
            # v0.42.0 (C3): validate ``depends_on`` against ALL plan tasks so a
            # legitimate cross-phase dep (e.g. task 2.1 depends_on 1.1, which the
            # architect now emits via A2) is NOT falsely rejected as "undefined"
            # by the per-phase validator. The scheduler
            # (``PlanManager.next_pending_tasks``) already resolves cross-phase
            # deps globally; this lifts the validation gate to match. Undefined-
            # ref check first (precondition for the cycle pass), then plan-wide
            # cycle detection. A genuine DAG error anywhere means the dependency
            # graph is broken, so we block all pending tasks and terminate
            # cleanly (matching the cross-phase path above).
            try:
                validate_dag_undefined_refs(plan)
                validate_dag_cycles_global(plan)
            except DagValidationError as exc:
                logger.warning("execute_phase.dag_invalid", err=str(exc))
                # ADR-0047: route the structural blocker through the resolver
                # for observability before the legacy block.
                await _maybe_resolve_blocker(
                    orch, None, failure_class="dag_invalid", raw_error=str(exc)
                )
                for phase in plan.phases:
                    for t in phase.tasks:
                        if t.status == "pending":
                            try:
                                await block_task(
                                    orch,
                                    t,
                                    failure_class=_fcls.DAG_INVALID,
                                    raw_error=str(exc),
                                    meta={
                                        "blocked_reason": f"dag_invalid: {exc}"
                                    },
                                )
                            except Exception:  # noqa: BLE001
                                pass
                return processed

        # v0.21.0 B1: cross-phase parallelism. When enabled, run a
        # single dispatcher across all phases — tasks from phase N+1
        # may begin executing while phase N's tail is in-flight,
        # provided their deps are terminal AND their files don't
        # conflict. Phase-review still fires per-phase as each phase
        # observes all-terminal, using ``end_checkpoint_commit`` for
        # diff isolation.
        if getattr(orch.cfg, "cross_phase_parallelism_enabled", False):
            for phase in plan.phases:
                await _maybe_record_phase_entry(orch, phase.id)
            cross_processed = await _execute_cross_phase_dag(
                orch, worktree_mgr, parallelism
            )
            processed.extend(cross_processed)
        else:
            for phase in plan.phases:
                await _maybe_record_phase_entry(orch, phase.id)
                # Run the worker pool for this phase, then phase-review.
                # If phase-review injects corrective tasks (review_status
                # transitions to ``corrective_required``), loop again so
                # those tasks execute within this phase before advancing
                # to the next. Cap at a small number of iterations as a
                # defensive net against pathological architect_b output.
                for _round in range(3):
                    phase_processed = await _execute_phase_dag(
                        orch, phase.id, worktree_mgr, parallelism
                    )
                    processed.extend(phase_processed)
                    await _maybe_run_phase_review(orch, phase.id)
                    # Did phase-review accept? If so we're done with this
                    # phase. Otherwise (corrective_required) the next loop
                    # iteration will run any newly-injected tasks.
                    latest_plan = await orch.plan_manager.load()
                    if latest_plan is None:
                        break
                    latest_phase = next(
                        (p for p in latest_plan.phases if p.id == phase.id), None
                    )
                    if latest_phase is None:
                        break
                    # v0.29.0 Bug 7: ``"paused"`` joins the terminal-for-
                    # this-loop set so a quarantine-induced halt cannot
                    # spin the round-loop. The phase will resume via
                    # :meth:`Orchestrator.resume` instead.
                    if latest_phase.review_status in (
                        "accepted",
                        "skipped",
                        "paused",
                    ):
                        break
                    # Any new pending tasks? If not, stop looping.
                    if not any(t.status == "pending" for t in latest_phase.tasks):
                        break
    except (AuthenticationFailedError, InfrastructureCircuitOpenError) as exc:
        # v0.29.0 Bug 7 / v0.30.0 Bug 5: typed halt during the whole-
        # plan loop. The worker has already stamped its own task as
        # ``quarantined`` with the typed reason retained for forensics
        # (``auth_failed:`` for a single-shot auth failure,
        # ``infra_circuit_open:`` for a tripped breaker); we just emit
        # the structured halt event and re-raise so the CLI driver
        # exits non-zero. Phase review is NOT triggered for any in-
        # flight phase — the phase aggregator's pause-on-quarantined
        # check parks the phase at ``review_status="paused"`` instead
        # of force-accepting on a halt path (the production stall this
        # fix exists to prevent).
        await _halt_for_auth_failed(orch, exc)
        raise
    finally:
        if worktree_mgr is not None:
            try:
                await worktree_mgr.cleanup_all()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.cleanup_all_failed", err=str(exc)
                )
    return processed


def _halt_reason_prefix(
    exc: AuthenticationFailedError | InfrastructureCircuitOpenError,
) -> str:
    """Return the ``blocked_reason`` prefix for the typed halt path.

    Differentiates the v0.28.0 single-shot auth halt from the v0.30.0
    Bug 5 cross-task circuit-breaker halt so post-mortems can ``grep``
    the ledger for either pattern. Both paths share the same
    quarantine + paused-phase wiring; only the prefix differs.
    """
    if isinstance(exc, InfrastructureCircuitOpenError):
        return "infra_circuit_open"
    return "auth_failed"


def _halt_console_message(
    exc: AuthenticationFailedError | InfrastructureCircuitOpenError,
) -> str:
    """Compose the operator-facing console message for the halt path.

    Both exception types produce the same shape ("aborting on … —
    refresh credentials and ``autodev resume``") because the operator
    response is identical: clear the underlying issue, re-run. The
    leading clause names the failure mode so the operator knows which
    knob to twist (a bad token vs. a flaky upstream).
    """
    if isinstance(exc, InfrastructureCircuitOpenError):
        clause = f"aborting on {exc}"
    else:
        clause = f"aborting on authentication failure ({exc})"
    return (
        f"\nautodev execute: {clause}.\n"
        "  Task is quarantined; refresh your credentials and "
        "run `autodev resume` to continue.\n"
    )


async def _halt_task_for_auth_failed(
    orch: "Orchestrator",
    task_id: str,
    exc: AuthenticationFailedError | InfrastructureCircuitOpenError,
) -> None:
    """v0.29.0 Bug 7 / v0.30.0 Bug 5: shared halt routine for typed
    quarantine-class failures.

    Idempotently stamps the task as ``quarantined`` (NOT ``blocked``)
    with a structured ``blocked_reason`` retained for forensics
    (prefix ``auth_failed:`` for the v0.28.0 single-shot path,
    ``infra_circuit_open:`` for the v0.30.0 cross-task circuit-breaker
    path), and emits the :data:`execute_phase.auth_failed_halt`
    structured-log event so the operator sees one clear line in the
    run log linking the failure to the halt. Failures inside this
    helper are swallowed (logged) so we never mask the original
    typed exception on the way back up to the CLI driver.

    v0.29.0 supersedes the v0.28.0 contract: the halt is now resumable.
    ``quarantined`` is non-terminal so :meth:`Orchestrator.resume` picks
    the task up automatically once the operator clears the underlying
    issue. ``block_reason_class`` (Bug 6) is intentionally NOT stamped
    — that field is reserved for true ``blocked`` tasks; a quarantined
    task is awaiting recovery, not classification.
    """
    prefix = _halt_reason_prefix(exc)
    try:
        await orch.plan_manager.update_task_status(
            task_id,
            "quarantined",
            meta={"blocked_reason": f"{prefix}: {exc}"},
        )
    except Exception as exc2:  # noqa: BLE001 — best-effort idempotent stamp
        logger.warning(
            "execute_phase.auth_failed_quarantine_failed",
            task_id=task_id,
            err=str(exc2),
        )
    # v0.29.0 Bug 7: park the owning phase at ``review_status="paused"``
    # so the phase aggregator's run-loop and the resume path both see a
    # clear "halt-recovery pending" signal. Looking up the phase id is
    # best-effort; failure here just means the next ``_maybe_run_phase
    # _review`` poll will set the pause stamp itself once it sees the
    # quarantined task.
    try:
        plan = await orch.plan_manager.load()
        if plan is not None:
            phase_id_for_pause: str | None = None
            current_review_status: str | None = None
            for phase in plan.phases:
                for t in phase.tasks:
                    if t.id == task_id:
                        phase_id_for_pause = phase.id
                        current_review_status = phase.review_status
                        break
                if phase_id_for_pause is not None:
                    break
            if phase_id_for_pause is not None:
                await _pause_phase_for_quarantine(
                    orch, phase_id_for_pause, current_review_status
                )
    except Exception as exc3:  # noqa: BLE001 — never mask the original
        logger.warning(
            "execute_phase.auth_failed_pause_lookup_failed",
            task_id=task_id,
            err=str(exc3),
        )
    logger.error(
        "execute_phase.auth_failed_halt",
        task_id=task_id,
        err=str(exc),
        halt_kind=prefix,
    )
    # Operator-facing message — distinct from the structured log so
    # ``autodev execute`` users see one clear line on the console.
    print(_halt_console_message(exc))


async def _halt_for_auth_failed(
    orch: "Orchestrator",
    exc: AuthenticationFailedError | InfrastructureCircuitOpenError,
) -> None:
    """Whole-plan variant of :func:`_halt_task_for_auth_failed`.

    The worker that originally raised has already stamped its own task
    as ``quarantined`` (v0.29.0 Bug 7); here we just emit the
    structured halt event and the operator-facing message. Walks the
    plan to find the halted task — preferring ``quarantined`` (the
    canonical post-halt status) and falling back to ``in_progress`` for
    the brief window before the worker's stamp lands. Best-effort and
    falls back to ``task_id="<unknown>"`` if the plan can't be loaded
    (e.g. ledger corruption mid-halt).

    v0.30.0 Bug 5: also handles
    :class:`InfrastructureCircuitOpenError` — the breaker raises this
    from the same ``delegate()`` site and the catch-and-treat-identically
    contract is preserved here via the union type.
    """
    # v0.38.0 I4 (HK7): prefer the typed identifier on
    # :class:`InfrastructureCircuitOpenError` when present (avoids
    # the race-prone plan-walk in the parallel pool where the worker
    # stamp may not have landed yet). Fall back to the legacy
    # plan-walk lookup for ``AuthenticationFailedError`` and for
    # legacy callers that haven't been migrated.
    explicit_task_id = getattr(exc, "halted_task_id", None)
    in_flight_task_id = explicit_task_id or "<unknown>"
    halted_phase_id: str | None = None
    halted_phase_review_status: str | None = None
    try:
        plan = await orch.plan_manager.load()
        if plan is not None:
            for phase in plan.phases:
                for t in phase.tasks:
                    if explicit_task_id is not None:
                        # We already know which task — only need to
                        # find its owning phase for the pause step.
                        if t.id == explicit_task_id:
                            halted_phase_id = phase.id
                            halted_phase_review_status = phase.review_status
                            break
                    elif t.status in ("quarantined", "in_progress"):
                        in_flight_task_id = t.id
                        halted_phase_id = phase.id
                        halted_phase_review_status = phase.review_status
                        break
                if halted_phase_id is not None:
                    break
                if (
                    explicit_task_id is None
                    and in_flight_task_id != "<unknown>"
                ):
                    break
    except Exception as exc2:  # noqa: BLE001 — never mask the original
        logger.warning(
            "execute_phase.auth_failed_plan_load_failed",
            err=str(exc2),
        )
    # v0.29.0 Bug 7: park the owning phase at ``review_status="paused"``
    # so the resume path sees a clear "halt-recovery pending" signal.
    if halted_phase_id is not None:
        try:
            await _pause_phase_for_quarantine(
                orch, halted_phase_id, halted_phase_review_status
            )
        except Exception as exc3:  # noqa: BLE001 — never mask the original
            logger.warning(
                "execute_phase.auth_failed_pause_lookup_failed",
                phase_id=halted_phase_id,
                err=str(exc3),
            )
    logger.error(
        "execute_phase.auth_failed_halt",
        task_id=in_flight_task_id,
        err=str(exc),
        halt_kind=_halt_reason_prefix(exc),
    )
    print(_halt_console_message(exc))


def _resolve_execute_parallelism(orch: "Orchestrator") -> int:
    """Resolve the per-task worker pool cap via runtime.resource_probe.

    Forwards ``cfg.tournaments.execute_max_parallel_tasks`` (None =
    auto-resolve) into :func:`runtime.resource_probe.resolve_parallelism`
    with ``role_mix='execute'``. ``num_judges=16`` (the absolute
    ceiling) when no explicit cap is configured — the dispatcher polls
    greedily, so this just sets the upper bound.
    """
    from orchestrator.huge_repo_overrides import resolve_huge_repo_parallelism
    from runtime.resource_probe import probe_host, resolve_parallelism

    configured = orch.cfg.tournaments.execute_max_parallel_tasks
    capacity = probe_host()
    base = resolve_parallelism(
        configured=configured,
        capacity=capacity,
        role_mix="execute",
        num_judges=configured if configured is not None else 16,
    )
    # v0.39.0 B3: halve auto-resolved parallelism on huge repos (operator
    # pin bypasses; no-op on small repos / when the escape hatch is set).
    return resolve_huge_repo_parallelism(
        base=base,
        configured=configured,
        cwd=orch.cwd,
        cfg=orch.cfg,
    )


def _is_git_repo(cwd: Path) -> bool:
    """Return True iff ``cwd`` looks like a git repo (has a ``.git`` entry).

    Worktree-isolation requires git, so the dispatcher falls back to
    the legacy in-place execution path for non-git fixtures.
    """
    try:
        gp = cwd / ".git"
        return gp.exists()
    except OSError:
        return False


async def _all_phase_tasks_terminal_async(
    orch: "Orchestrator", phase_id: str
) -> bool:
    """Read the latest plan and return True iff every task in the phase
    is in a terminal state. Distinct from the synchronous helper
    :func:`_all_phase_tasks_terminal` which takes a Phase object."""
    plan = await orch.plan_manager.load()
    if plan is None:
        return False
    phase = next((p for p in plan.phases if p.id == phase_id), None)
    if phase is None or not phase.tasks:
        return False
    return _all_phase_tasks_terminal(phase)


async def _execute_phase_dag(
    orch: "Orchestrator",
    phase_id: str,
    worktree_mgr: WorktreeManager | None,
    parallelism: int,
) -> list[Task]:
    """Run the worker pool for one phase. Returns processed tasks.

    Polls :meth:`PlanManager.next_pending_tasks` for runnable tasks
    (deps met, no file overlap with in-flight) and spawns asyncio
    workers up to ``parallelism``. Awaits the first to complete via
    :func:`asyncio.wait` with ``FIRST_COMPLETED``, drains, and resumes
    polling. Failures route into ``mark_blocked_descendants`` to
    cascade-block dependents.
    """
    import asyncio

    in_flight: dict[str, asyncio.Task[Task]] = {}
    processed: list[Task] = []

    while True:
        # Termination: phase is fully terminal AND no workers running.
        if not in_flight and await _all_phase_tasks_terminal_async(orch, phase_id):
            return processed

        # Try to spawn new workers up to the cap.
        if len(in_flight) < parallelism:
            slots = parallelism - len(in_flight)
            excluded = await orch.plan_manager.in_flight_files()
            tasks_to_run = await orch.plan_manager.next_pending_tasks(
                limit=slots, exclude_files=excluded
            )
            # Filter to tasks that belong to THIS phase (others wait
            # for their phase's turn — phase-major scheduling).
            tasks_to_run = [t for t in tasks_to_run if t.phase_id == phase_id]
            for t in tasks_to_run:
                if t.id in in_flight:
                    continue
                await orch.plan_manager.mark_in_flight(t.id)
                in_flight[t.id] = asyncio.create_task(
                    _execute_one_worker(orch, t, worktree_mgr)
                )

        if not in_flight:
            # Nothing to spawn AND not all terminal — likely waiting on
            # an in-flight task in a different phase; brief sleep then
            # re-poll. Bounded short delay keeps unit tests snappy.
            # If the ENTIRE phase is in_progress (no pending, no
            # in_flight in our set), another runner / external mutation
            # is in play — break out so we don't spin forever.
            if await _all_phase_tasks_terminal_async(orch, phase_id):
                return processed
            # Defensive: also break if the phase has ZERO pending tasks
            # at this point — there's nothing to wait for.
            phase_has_pending = False
            stuck_task_ids: list[str] = []
            plan = await orch.plan_manager.load()
            if plan is not None:
                phase_obj = next(
                    (p for p in plan.phases if p.id == phase_id), None
                )
                if phase_obj is not None:
                    phase_has_pending = any(
                        t.status == "pending" for t in phase_obj.tasks
                    )
                    # v0.22.2 B2: collect non-terminal non-pending task IDs for
                    # the PhaseStuckError surface.
                    stuck_task_ids = [
                        t.id
                        for t in phase_obj.tasks
                        if t.status not in _TERMINAL_TASK_STATUSES
                        and t.status != "pending"
                    ]
            if not phase_has_pending:
                # v0.22.2 B2: if non-terminal non-pending tasks exist, this is
                # a wedged FSM (typical cause: an interrupted run that left
                # tasks in ``coded``/``in_progress``). Pre-B2 this returned
                # silently and the dispatcher reported success. Now we raise
                # PhaseStuckError so the operator sees the stuck tasks. If
                # everything is genuinely terminal, the legacy clean return
                # still applies.
                if stuck_task_ids:
                    from errors import PhaseStuckError

                    raise PhaseStuckError(phase_id, stuck_task_ids)
                return processed
            await asyncio.sleep(0.05)
            continue

        # Wait for any worker to finish.
        done, _pending = await asyncio.wait(
            list(in_flight.values()), return_when=asyncio.FIRST_COMPLETED
        )
        for d in done:
            # Reverse-lookup the task id from the asyncio.Task instance.
            finished_id = next(
                (tid for tid, h in in_flight.items() if h is d), None
            )
            if finished_id is None:
                continue
            del in_flight[finished_id]
            await orch.plan_manager.clear_in_flight(finished_id)
            try:
                completed_task = d.result()
                processed.append(completed_task)
            except (AuthenticationFailedError, InfrastructureCircuitOpenError):
                # v0.29.0 Bug 7 / v0.30.0 Bug 5: drain remaining in-
                # flight workers so we don't leave orphan asyncio tasks
                # dangling, then re-raise so ``run_execute_phase`` can
                # log the structured halt event and abort the phase
                # loop. The worker itself has already stamped the
                # offending task as ``quarantined`` with
                # ``blocked_reason="auth_failed: ..."`` (or
                # ``infra_circuit_open:`` for Bug 5) retained for
                # forensics before re-raising.
                #
                # v0.38.0 I4 (HK6): bounded drain. Pre-I4 the unbounded
                # ``asyncio.gather`` stalled the process for ~30s on
                # slow-teardown adapters (real-world enterprise runs);
                # the H3 integration test was the canary. The drain
                # now cancels, awaits up to ``drain_timeout_s``, and
                # absorbs any straggler via
                # ``gather(return_exceptions=True)`` so
                # ``CancelledError`` doesn't propagate.
                _drain_timeout_s = getattr(
                    orch.cfg, "parallel_pool_drain_timeout_s", 10.0
                )
                for _other_id, other in list(in_flight.items()):
                    if other.done():
                        continue
                    other.cancel()
                if in_flight:
                    try:
                        _done_drain, _pending_drain = await asyncio.wait(
                            list(in_flight.values()),
                            timeout=_drain_timeout_s,
                        )
                        if _pending_drain:
                            logger.warning(
                                "execute_phase.parallel_pool.drain_slow",
                                pending_count=len(_pending_drain),
                                timeout_s=_drain_timeout_s,
                            )
                            await asyncio.gather(
                                *_pending_drain, return_exceptions=True
                            )
                    except Exception as exc_drain:  # noqa: BLE001
                        logger.warning(
                            "execute_phase.parallel_pool.drain_error",
                            err=str(exc_drain),
                        )
                    for other_id in list(in_flight.keys()):
                        try:
                            await orch.plan_manager.clear_in_flight(
                                other_id
                            )
                        except Exception as exc2:  # noqa: BLE001
                            logger.warning(
                                "execute_phase.clear_in_flight_failed",
                                task_id=other_id,
                                err=str(exc2),
                            )
                raise
            except Exception as exc:  # noqa: BLE001
                # Worker raised — cascade-block descendants. The worker
                # itself should have caught and routed to update_task_status,
                # so this is a defensive net.
                logger.warning(
                    "execute_phase.worker_unhandled_exception",
                    task_id=finished_id,
                    err=str(exc),
                )
                try:
                    await orch.plan_manager.mark_blocked_descendants(
                        phase_id, finished_id, str(exc)
                    )
                except Exception as exc2:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.mark_blocked_descendants_failed",
                        phase_id=phase_id,
                        task_id=finished_id,
                        err=str(exc2),
                    )


def _validate_cross_phase_dag(plan: "Plan") -> None:
    """v0.21.0 B1: relaxed DAG validation accepting cross-phase deps.

    Mirrors :func:`orchestrator.dag.validate_phase_dag` but operates at
    plan-level: every ``Task.depends_on`` id must resolve somewhere in
    the plan (not just within its own phase). Cycle detection runs
    across the unified graph too — a backward dep from phase 1 to
    phase 2 would still count as a cycle.

    Raises :class:`orchestrator.dag.DagValidationError` on:

    * an undefined dep (no task with that id anywhere in the plan)
    * any cycle in the unified DAG
    """
    from orchestrator.dag import DagValidationError

    by_id: dict[str, "Task"] = {}
    for phase in plan.phases:
        for t in phase.tasks:
            by_id[t.id] = t

    # Pass 1: undefined references.
    for tid, t in by_id.items():
        for dep in t.depends_on:
            if dep not in by_id:
                raise DagValidationError(
                    f"task {tid!r} depends_on undefined task {dep!r} "
                    "(plan-level)"
                )

    # Pass 2: cycle detection across the unified graph.
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {tid: WHITE for tid in by_id}

    def dfs(node: str, path: list[str]) -> None:
        color[node] = GRAY
        path.append(node)
        for dep in by_id[node].depends_on:
            if color[dep] == GRAY:
                start = path.index(dep)
                cycle = " -> ".join(path[start:] + [dep])
                raise DagValidationError(
                    f"cycle detected (plan-level): {cycle}"
                )
            if color[dep] == WHITE:
                dfs(dep, path)
        path.pop()
        color[node] = BLACK

    for tid in by_id:
        if color[tid] == WHITE:
            dfs(tid, [])


async def _execute_cross_phase_dag(
    orch: "Orchestrator",
    worktree_mgr: WorktreeManager | None,
    parallelism: int,
) -> list[Task]:
    """v0.21.0 B1: cross-phase parallel dispatcher.

    Runs a single worker pool over ALL phases of the plan, allowing tasks
    from phase N+1 to begin executing while phase N's tail is still
    in-flight, subject to:

    * dependencies in :attr:`Task.depends_on` are all in a terminal state
      (the existing :meth:`PlanManager.next_pending_tasks` already enforces
      this and is unchanged), AND
    * the new task's files don't intersect any in-flight task's files
      (also enforced by :meth:`next_pending_tasks` via ``exclude_files``).

    Phase-review fires per-phase as each phase observes all-terminal,
    using :func:`_maybe_run_phase_review` and :func:`_maybe_record_phase_checkpoint`
    so the diff range honors the captured ``end_checkpoint_commit``
    rather than live HEAD (preserves correctness when phase N+1 is
    landing commits during phase N's review).

    Termination: every task across every phase is in a terminal state
    AND no workers are in-flight. Then any phases without phase-review
    completion are walked once more so corrective tasks (if any) get
    a chance to run.
    """
    in_flight: dict[str, asyncio.Task[Task]] = {}
    in_flight_phase_id: dict[str, str] = {}  # task_id → phase_id
    processed: list[Task] = []

    # v0.21.0 B2: speculative-execution bookkeeping. Maps speculative
    # task_id → parent task_id so the rollback path can find the
    # parent on failure. Capped at 1 active speculative task per
    # phase per the v0.21.0 plan.
    speculative_parents: dict[str, str] = {}
    speculative_phase: set[str] = set()  # phases with active speculative
    speculative_enabled = getattr(
        orch.cfg, "speculative_execution_enabled", False
    )

    async def _all_terminal_across_plan() -> bool:
        plan = await orch.plan_manager.load()
        if plan is None:
            return True
        for phase in plan.phases:
            for t in phase.tasks:
                if t.status not in _TERMINAL_TASK_STATUSES:
                    return False
        return True

    while True:
        if not in_flight and await _all_terminal_across_plan():
            # Fire phase-reviews for every phase that's now terminal.
            plan = await orch.plan_manager.load()
            if plan is not None:
                for phase in plan.phases:
                    await _maybe_run_phase_review(orch, phase.id)
            return processed

        # Spawn workers up to parallelism cap. The dispatcher honors
        # the existing file-overlap and depends-on guards, but does NOT
        # restrict by phase_id (the cross-phase contract).
        if len(in_flight) < parallelism:
            slots = parallelism - len(in_flight)
            excluded = await orch.plan_manager.in_flight_files()
            tasks_to_run = await orch.plan_manager.next_pending_tasks(
                limit=slots, exclude_files=excluded
            )
            for t in tasks_to_run:
                if t.id in in_flight:
                    continue
                await orch.plan_manager.mark_in_flight(t.id)
                in_flight[t.id] = asyncio.create_task(
                    _execute_one_worker(orch, t, worktree_mgr)
                )
                in_flight_phase_id[t.id] = t.phase_id

            # v0.21.0 B2: after dispatching pending tasks, opportunistically
            # speculate ONE child task per phase whose parent is in-flight.
            # Conditions are fully enforced by ``speculable_candidate``;
            # we just gate at "max 1 speculative per phase" here.
            if speculative_enabled and len(in_flight) < parallelism:
                # Pick one in-flight task that doesn't already have a
                # speculative child running in its phase.
                for parent_id in list(in_flight.keys()):
                    parent_phase = in_flight_phase_id.get(parent_id, "")
                    if parent_phase in speculative_phase:
                        continue
                    candidate = await orch.plan_manager.speculable_candidate(
                        parent_id
                    )
                    if candidate is None:
                        continue
                    if candidate.id in in_flight:
                        continue
                    await orch.plan_manager.mark_in_flight(candidate.id)
                    await orch.plan_manager.ledger_append(
                        op="speculative_started",
                        payload={
                            "task_id": candidate.id,
                            "parent_task_id": parent_id,
                        },
                    )
                    in_flight[candidate.id] = asyncio.create_task(
                        _execute_one_worker(orch, candidate, worktree_mgr)
                    )
                    in_flight_phase_id[candidate.id] = candidate.phase_id
                    speculative_parents[candidate.id] = parent_id
                    speculative_phase.add(candidate.phase_id)
                    logger.info(
                        "execute_phase.speculative_started",
                        task_id=candidate.id,
                        parent_task_id=parent_id,
                    )
                    break  # max 1 speculative per polling round

        if not in_flight:
            # Nothing dispatchable but not all terminal — likely waiting
            # on a different worker; brief sleep then re-poll.
            if await _all_terminal_across_plan():
                continue
            await asyncio.sleep(0.05)
            continue

        # Wait for any worker to finish.
        done, _pending = await asyncio.wait(
            list(in_flight.values()), return_when=asyncio.FIRST_COMPLETED
        )
        finished_phases: set[str] = set()
        # v0.21.0 B2: track failed parents so we can roll back any
        # speculative children that depended on them in this round.
        failed_parents: set[str] = set()
        for d in done:
            finished_id = next(
                (tid for tid, h in in_flight.items() if h is d), None
            )
            if finished_id is None:
                continue
            finished_phase_id = in_flight_phase_id.pop(finished_id, "")
            del in_flight[finished_id]
            await orch.plan_manager.clear_in_flight(finished_id)
            # If this was a speculative task, pop its bookkeeping.
            if finished_id in speculative_parents:
                speculative_parents.pop(finished_id, None)
                speculative_phase.discard(finished_phase_id)
            try:
                completed_task = d.result()
                processed.append(completed_task)
                # If this task was non-speculative AND it failed (status
                # blocked) AND a speculative child was tracked, mark
                # the parent as failed so the rollback runs after the
                # main loop body.
                if (
                    completed_task.status == "blocked"
                    and finished_id not in speculative_parents.values()
                ):
                    pass  # caught by the speculative_parents check below
                if completed_task.status == "blocked":
                    failed_parents.add(finished_id)
            except (AuthenticationFailedError, InfrastructureCircuitOpenError):
                # v0.29.0 Bug 7 / v0.30.0 Bug 5: cross-phase variant of
                # the typed halt. Cancel and drain remaining workers so
                # we don't strand asyncio tasks across the cross-phase
                # pool, then re-raise into ``run_execute_phase`` for
                # the structured log + abort. The worker has already
                # stamped the offending task as ``quarantined`` with
                # the typed prefix (``auth_failed:`` or
                # ``infra_circuit_open:``) retained for forensics.
                #
                # v0.38.0 I4 (HK6): bounded drain — see the matching
                # block in :func:`_execute_phase_dag` for the rationale.
                _drain_timeout_s = getattr(
                    orch.cfg, "parallel_pool_drain_timeout_s", 10.0
                )
                for _other_id, other in list(in_flight.items()):
                    if other.done():
                        continue
                    other.cancel()
                if in_flight:
                    try:
                        _done_drain, _pending_drain = await asyncio.wait(
                            list(in_flight.values()),
                            timeout=_drain_timeout_s,
                        )
                        if _pending_drain:
                            logger.warning(
                                "execute_phase.parallel_pool.drain_slow",
                                pending_count=len(_pending_drain),
                                timeout_s=_drain_timeout_s,
                            )
                            await asyncio.gather(
                                *_pending_drain, return_exceptions=True
                            )
                    except Exception as exc_drain:  # noqa: BLE001
                        logger.warning(
                            "execute_phase.parallel_pool.drain_error",
                            err=str(exc_drain),
                        )
                    for other_id in list(in_flight.keys()):
                        try:
                            await orch.plan_manager.clear_in_flight(
                                other_id
                            )
                        except Exception as exc2:  # noqa: BLE001
                            logger.warning(
                                "execute_phase.clear_in_flight_failed",
                                task_id=other_id,
                                err=str(exc2),
                            )
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.cross_phase_worker_unhandled_exception",
                    task_id=finished_id,
                    err=str(exc),
                )
                failed_parents.add(finished_id)
                if finished_phase_id:
                    try:
                        await orch.plan_manager.mark_blocked_descendants(
                            finished_phase_id, finished_id, str(exc)
                        )
                    except Exception as exc2:  # noqa: BLE001
                        logger.warning(
                            "execute_phase.cross_phase_cascade_block_failed",
                            phase_id=finished_phase_id,
                            task_id=finished_id,
                            err=str(exc2),
                        )
            if finished_phase_id:
                finished_phases.add(finished_phase_id)

        # v0.21.0 B2: roll back any speculative children whose parents
        # just failed. Walk a copy of speculative_parents so we can
        # mutate the dict mid-iteration.
        if speculative_enabled and failed_parents:
            from orchestrator.speculative import rollback_speculative_task

            for spec_id, parent_id in list(speculative_parents.items()):
                if parent_id not in failed_parents:
                    continue
                # Look up the speculative task.
                plan = await orch.plan_manager.load()
                if plan is None:
                    continue
                spec_task = next(
                    (
                        t
                        for ph in plan.phases
                        for t in ph.tasks
                        if t.id == spec_id
                    ),
                    None,
                )
                if spec_task is None:
                    continue
                # If the speculative task is still in flight here, we
                # can't roll back yet — wait until next round.
                if spec_id in in_flight:
                    continue
                try:
                    await rollback_speculative_task(
                        orch,
                        spec_task,
                        parent_task_id=parent_id,
                        reason="parent_blocked",
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.speculative_rollback_failed",
                        task_id=spec_id,
                        err=str(exc),
                    )
                speculative_parents.pop(spec_id, None)
                # Phase may now be free for another speculative attempt.
                phase_id_of_spec = next(
                    (
                        ph.id
                        for ph in plan.phases
                        for t in ph.tasks
                        if t.id == spec_id
                    ),
                    None,
                )
                if phase_id_of_spec is not None:
                    speculative_phase.discard(phase_id_of_spec)

        # After draining, fire phase-review for any phase that just
        # observed all-terminal. ``_maybe_run_phase_review`` is
        # idempotent + race-safe (in_flight count check).
        for phid in finished_phases:
            await _maybe_run_phase_review(orch, phid)


async def _execute_one_worker(
    orch: "Orchestrator",
    task: Task,
    worktree_mgr: WorktreeManager | None,
) -> Task:
    """Worker entry: run :func:`_execute_one` and route all exceptions.

    Workers must NEVER raise plan-fatal exceptions back to the
    dispatcher — instead they catch any exception, transition the task
    to ``blocked`` with a structured reason, and call
    ``mark_blocked_descendants`` so dependents are cascade-blocked.
    The dispatcher reads the worker's return value (the final Task)
    and only sees "done, status was X".

    v0.26.0: previously, :class:`DelegationPendingSignal` (raised by
    the inline adapter to suspend a run pending an external response)
    was re-raised here so the caller could persist suspend state. With
    InlineAdapter gone the special-case is gone; every exception is
    routed through the cascade-block path.

    v0.26.1 patch E: exceptions are classified by ``isinstance`` so the
    ``blocked_reason`` prefix carries typed semantics (encoding error vs
    IO error vs timeout vs generic). The full traceback is persisted to
    ``.autodev/debug/worker-exception-<task>-<ts>.txt`` for operator
    diagnosis.
    """
    try:
        return await _execute_one(orch, task, worktree_mgr)
    except (AuthenticationFailedError, InfrastructureCircuitOpenError) as exc:
        # v0.29.0 Bug 7 / v0.30.0 Bug 5: stamp the task as
        # ``quarantined`` (NOT ``blocked``) with the typed prefix
        # retained in ``blocked_reason`` for forensics BEFORE re-
        # raising so the top-level ``run_execute_phase`` catch site
        # sees a fully-persisted plan state on the way out.
        # Quarantined is non-terminal so ``Orchestrator.resume()``
        # picks the task up automatically once the operator clears the
        # underlying issue. Re-raising propagates through
        # ``_execute_phase_dag`` (which has its own typed catch) up to
        # ``run_execute_phase`` for the structured-log + abort.
        try:
            await orch.plan_manager.update_task_status(
                task.id,
                "quarantined",
                meta={"blocked_reason": f"{_halt_reason_prefix(exc)}: {exc}"},
            )
        except Exception as exc2:  # noqa: BLE001 — never mask the original
            logger.warning(
                "execute_phase.auth_failed_quarantine_failed",
                task_id=task.id,
                err=str(exc2),
            )
        raise
    except Exception as exc:  # noqa: BLE001
        import asyncio
        import traceback

        from state.paths import debug_dir

        if isinstance(exc, UnicodeDecodeError):
            prefix = "qa_gate_encoding_error"
        elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            prefix = "qa_gate_timeout"
        elif isinstance(exc, OSError):
            prefix = "qa_gate_io_error"
        else:
            prefix = "worker_exception"
        blocked_reason = f"{prefix}: {exc}"
        # v0.29.0 Bug 6: classify the typed block category. Network /
        # auth-class exceptions (timeouts, OS-level network errors)
        # surface as ``"infrastructure"`` so the operator can safely
        # ``autodev requeue --infrastructure`` once the environment is
        # healthy. Encoding errors and bare ``worker_exception``
        # (developer-bug bucket) classify as ``"verdict"`` —
        # requeueing without code changes won't help.
        if prefix in ("qa_gate_timeout", "qa_gate_io_error"):
            block_reason_class: Literal[
                "verdict", "infrastructure", "cap"
            ] = "infrastructure"
        else:
            block_reason_class = "verdict"

        # Persist traceback for operator diagnosis. Failure to write is
        # logged but never re-raised — the block path is already a
        # degraded-mode return.
        try:
            dbg = debug_dir(orch.cwd)
            dbg.mkdir(parents=True, exist_ok=True)
            ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            safe_id = task.id.replace("/", "_").replace(" ", "_")
            tb_path = dbg / f"worker-exception-{safe_id}-{ts}.txt"
            tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            tb_path.write_text(tb_text, encoding="utf-8", errors="replace")
        except Exception as exc_tb:  # noqa: BLE001
            logger.warning(
                "execute_phase.worker_traceback_write_failed",
                task_id=task.id,
                err=str(exc_tb),
            )

        logger.warning(
            "execute_phase.worker_caught_exception",
            task_id=task.id,
            prefix=prefix,
            err=str(exc),
        )
        # v0.32.0 (Phase 5, Gap G): structured recovery hint. Network /
        # io / timeout exceptions get the network_transient class;
        # encoding / unclassified ones land in user_decision_required.
        if block_reason_class == "infrastructure":
            worker_hint = _build_recovery_hint(
                task_id=task.id,
                hint_class="network_transient",
                action=(
                    f"Worker hit a transient {prefix} ({type(exc).__name__}). "
                    "Refresh credentials/connection and `autodev resume`."
                ),
                debug_files=[
                    f".autodev/debug/worker-exception-"
                    f"{task.id.replace('/', '_').replace(' ', '_')}-*.txt"
                ],
                commands=[
                    "autodev doctor",
                    f"autodev requeue --task {task.id} --infrastructure",
                    "autodev resume",
                ],
            )
        else:
            worker_hint = _build_recovery_hint(
                task_id=task.id,
                hint_class="user_decision_required",
                action=(
                    f"Worker raised an unclassified exception "
                    f"({type(exc).__name__}). Inspect the captured "
                    f"traceback in .autodev/debug/worker-exception-*.txt "
                    f"and decide whether the underlying code or "
                    f"configuration needs a fix."
                ),
                debug_files=[
                    f".autodev/debug/worker-exception-"
                    f"{task.id.replace('/', '_').replace(' ', '_')}-*.txt"
                ],
            )
        # ADR-0047 / v0.42.1 F1: route the worker crash through the single
        # chokepoint; a recovery (e.g. retry_with_changes / consult_knowledge)
        # re-enables the task and ``block_task`` returns it non-blocked.
        try:
            blocked = await block_task(
                orch,
                task,
                failure_class=_fcls.WORKER_EXCEPTION,
                raw_error=str(exc),
                meta={
                    "blocked_reason": blocked_reason,
                    "block_reason_class": block_reason_class,
                    # v0.30.0 Bug 4: forensic-only payload extension —
                    # carry the most recent adapter status / subtype
                    # so post-mortems can grep the ledger directly.
                    "api_error_status": getattr(
                        orch, "_last_adapter_api_error_status", None
                    ),
                    "last_adapter_subtype": getattr(
                        orch, "_last_adapter_subtype", None
                    ),
                    "recovery_hint": worker_hint,
                },
            )
        except Exception as exc2:  # noqa: BLE001
            logger.warning(
                "execute_phase.worker_block_failed",
                task_id=task.id,
                err=str(exc2),
            )
            return task
        if blocked.status != "blocked":
            # Resolver recovered the task — skip the cascade-block.
            return blocked
        try:
            await orch.plan_manager.mark_blocked_descendants(
                task.phase_id, task.id, str(exc)
            )
        except Exception as exc2:  # noqa: BLE001
            logger.warning(
                "execute_phase.cascade_block_failed",
                task_id=task.id,
                err=str(exc2),
            )
        return blocked


async def _maybe_record_phase_entry(orch: "Orchestrator", phase_id: str) -> None:
    """Record ``Phase.baseline_commit`` once at first entry to the phase.

    Idempotent: if the phase already has a ``baseline_commit``, this is a
    no-op. Failure to read HEAD (no git repo, etc.) is logged and skipped
    rather than raising — phase review will degrade gracefully to an
    empty-diff bundle.
    """
    plan = await orch.plan_manager.load()
    if plan is None:
        return
    phase = next((p for p in plan.phases if p.id == phase_id), None)
    if phase is None or phase.baseline_commit is not None:
        return
    sha = _git_rev_parse_head(orch.cwd)
    if sha is None:
        logger.info(
            "execute_phase.baseline_commit_unavailable", phase_id=phase_id
        )
        return
    try:
        await orch.plan_manager.update_phase_meta(phase_id, baseline_commit=sha)
    except Exception as exc:  # noqa: BLE001 — never let phase metadata
        logger.warning(
            "execute_phase.update_phase_meta_failed",
            phase_id=phase_id,
            err=str(exc),
        )


async def _maybe_record_phase_checkpoint(
    orch: "Orchestrator", phase_id: str
) -> None:
    """v0.21.0 B1: capture ``Phase.end_checkpoint_commit`` once at phase
    completion.

    Idempotent: phases already carrying an end_checkpoint_commit are
    skipped. This SHA is the ``tip_commit`` for the phase-review
    tournament's diff range — captured at the moment ALL tasks in the
    phase reach a terminal state. Critically, this happens BEFORE the
    next phase's tasks start landing commits (which is possible under
    cross-phase parallelism), so the diff window is phase-isolated.

    Reads the latest plan; if every task in the phase is terminal AND
    end_checkpoint_commit is unset, captures HEAD and persists.
    """
    plan = await orch.plan_manager.load()
    if plan is None:
        return
    phase = next((p for p in plan.phases if p.id == phase_id), None)
    if phase is None or phase.end_checkpoint_commit is not None:
        return
    if not _all_phase_tasks_terminal(phase):
        return
    sha = _git_rev_parse_head(orch.cwd)
    if sha is None:
        logger.info(
            "execute_phase.end_checkpoint_unavailable", phase_id=phase_id
        )
        return
    try:
        await orch.plan_manager.update_phase_meta(
            phase_id, end_checkpoint_commit=sha
        )
        logger.info(
            "execute_phase.end_checkpoint_captured",
            phase_id=phase_id,
            sha=sha[:12],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.end_checkpoint_capture_failed",
            phase_id=phase_id,
            err=str(exc),
        )


def _all_phase_tasks_terminal(phase: Phase) -> bool:
    """Return ``True`` iff every task in the phase is in a terminal state.

    Terminal = complete | blocked | skipped. The phase-review tournament
    fires only when ALL tasks are terminal — partial progress doesn't
    trigger review.
    """
    if not phase.tasks:
        return False
    return all(t.status in _TERMINAL_TASK_STATUSES for t in phase.tasks)


def _phase_has_quarantined_task(phase: Phase) -> bool:
    """v0.29.0 Bug 7: ``True`` iff any task in the phase is ``quarantined``.

    Used by :func:`_maybe_run_phase_review` as the early-bail guard that
    refuses to auto-accept (or even re-evaluate) a phase whose execution
    was halted by a typed infrastructure failure
    (:class:`AuthenticationFailedError` etc.). When this returns
    ``True``, the aggregator parks the phase at ``review_status="paused"``
    instead of firing the phase-review tournament; the
    :meth:`Orchestrator.resume` path clears the paused state once the
    quarantined tasks resolve and re-execute.
    """
    return any(t.status == "quarantined" for t in phase.tasks)


async def _pause_phase_for_quarantine(
    orch: "Orchestrator", phase_id: str, current_status: str | None
) -> None:
    """v0.29.0 Bug 7: idempotently park the phase at ``review_status="paused"``.

    No-op when the phase is already paused so the per-worker drain
    callbacks don't write a redundant ledger entry on every poll.
    Failures are swallowed (logged) so the auth-failed halt path
    cannot be masked by a phase-meta update problem.
    """
    if current_status == "paused":
        return
    try:
        await orch.plan_manager.update_phase_meta(
            phase_id, review_status="paused"
        )
        logger.info(
            "execute_phase.phase_review_paused_for_quarantine",
            phase_id=phase_id,
            prior_status=current_status,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.phase_review_pause_failed",
            phase_id=phase_id,
            err=str(exc),
        )


def _phase_has_infrastructure_block(phase: Phase) -> bool:
    """v0.30.0 Bug 3: ``True`` iff any task in the phase is ``blocked``
    with ``block_reason_class="infrastructure"``.

    Generalises the v0.29.0 Bug 7 quarantined-check from the non-
    terminal halt path (``AuthenticationFailedError`` -> ``quarantined``)
    to the terminal infrastructure-class block path (timeouts, OS-level
    network errors -> ``blocked`` with the typed
    ``"infrastructure"`` class stamped by ``_execute_one_worker``).
    Both signals are operator-recoverable (``autodev requeue
    --infrastructure``) and neither should be silently force-accepted
    by the phase-review tournament. Distinct from the ``"verdict"``
    and ``"cap"`` classes, which legitimately need review (the agent
    reached a real negative verdict, or budget was exhausted).
    """
    return any(
        t.status == "blocked" and t.block_reason_class == "infrastructure"
        for t in phase.tasks
    )


async def _pause_phase_for_infrastructure(
    orch: "Orchestrator", phase_id: str, current_status: str | None
) -> None:
    """v0.30.0 Bug 3: idempotently park a phase that holds an
    ``"infrastructure"``-class blocked task at ``review_status="paused"``.

    Sibling of :func:`_pause_phase_for_quarantine` with a distinct
    structured-log signal (``phase_aggregate_paused_due_to_infrastructure``
    vs ``phase_review_paused_for_quarantine``) so post-mortems can tell
    the two recovery paths apart. No-op when the phase is already
    paused so repeated aggregator polls don't churn the ledger.
    Failures are swallowed (logged) so the pause path cannot mask the
    underlying block.
    """
    if current_status == "paused":
        return
    try:
        await orch.plan_manager.update_phase_meta(
            phase_id, review_status="paused"
        )
        logger.info(
            "execute_phase.phase_aggregate_paused_due_to_infrastructure",
            phase_id=phase_id,
            prior_status=current_status,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.phase_aggregate_pause_failed",
            phase_id=phase_id,
            err=str(exc),
        )


async def _maybe_run_phase_review(
    orch: "Orchestrator", phase_id: str
) -> None:
    """Trigger the phase-review tournament if the phase is fully terminal.

    Critical loop guard: phases with ``review_status`` already in
    ``{"accepted", "corrective_required", "skipped"}`` are skipped. This
    prevents corrective tasks (which themselves land terminal) from
    re-firing the tournament. Once corrective tasks reach terminal, this
    function transitions ``"corrective_required"`` directly to
    ``"accepted"`` without a second tournament.

    v0.29.0 Bug 7: also short-circuits when the phase contains a
    ``quarantined`` task. Quarantined tasks are non-terminal but signal
    a halt-on-infra-failure that must NOT be silently force-accepted.
    The phase is parked at ``review_status="paused"`` instead, and
    :meth:`Orchestrator.resume` clears that state once the quarantined
    work resolves so the tournament re-fires fresh.

    v0.30.0 Bug 3: generalises the same guard to terminal blocked
    tasks whose ``block_reason_class="infrastructure"``. The worker-
    exception path can stamp a task ``blocked`` (terminal) with the
    typed infrastructure class — without this guard the all-terminal
    poll below would force-accept the phase even though the operator
    can still recover via ``autodev requeue --infrastructure``.
    """
    if not orch.cfg.tournaments.phase_review.enabled:
        return

    # v0.11.0 race guard: if any worker for this phase is still in
    # flight, defer firing. The dispatcher will call this again after
    # each worker drains. Without this guard, two workers completing
    # simultaneously can both observe "all terminal" and double-fire.
    if (await orch.plan_manager.phase_in_flight_count(phase_id)) > 0:
        return

    plan = await orch.plan_manager.load()
    if plan is None:
        return
    phase = next((p for p in plan.phases if p.id == phase_id), None)
    if phase is None:
        return

    # v0.29.0 Bug 7: BEFORE every auto-accept site below, refuse to
    # touch a phase that holds a quarantined task. Park at "paused" and
    # exit; the resume path is responsible for clearing this once the
    # quarantined work resolves.
    if _phase_has_quarantined_task(phase):
        await _pause_phase_for_quarantine(orch, phase_id, phase.review_status)
        return
    # v0.30.0 Bug 3: same guard for terminal blocked tasks that carry
    # the typed ``"infrastructure"`` class. The aggregator must NOT
    # auto-accept a phase whose only remaining failures are operator-
    # recoverable transient infrastructure errors — the resume path
    # will requeue + clear once the operator fixes the environment.
    if _phase_has_infrastructure_block(phase):
        await _pause_phase_for_infrastructure(
            orch, phase_id, phase.review_status
        )
        return

    # Critical loop guard: reviewed phases are not re-reviewed.
    if phase.review_status == "accepted":
        return
    if phase.review_status == "skipped":
        return
    if phase.review_status == "paused":
        # v0.29.0 Bug 7: a phase parked by an earlier quarantine. With
        # no quarantined tasks remaining (checked above), the resume
        # path is responsible for explicitly clearing ``paused``;
        # observing ``paused`` here without the resume-clear means
        # we're being polled mid-recovery and should defer.
        return
    if phase.review_status == "corrective_required":
        # Corrective tasks have landed terminal — accept and move on.
        if _all_phase_tasks_terminal(phase):
            try:
                await orch.plan_manager.update_phase_meta(
                    phase_id, review_status="accepted"
                )
                logger.info(
                    "execute_phase.corrective_completed_accepted",
                    phase_id=phase_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.corrective_accept_failed",
                    phase_id=phase_id,
                    err=str(exc),
                )
        return

    # Only fire on full-terminal observation.
    if not _all_phase_tasks_terminal(phase):
        return

    # v0.21.0 B1: capture end_checkpoint_commit at the moment we observe
    # all tasks terminal — BEFORE any concurrent next-phase tasks land
    # commits. Idempotent: re-entry with end_checkpoint already set is a
    # no-op. The captured SHA is read by the phase-review runner as
    # tip_commit so the diff range is phase-isolated.
    await _maybe_record_phase_checkpoint(orch, phase_id)

    # Re-load to pick up the captured checkpoint (used downstream).
    plan = await orch.plan_manager.load()
    if plan is None:
        return
    phase = next((p for p in plan.phases if p.id == phase_id), None)
    if phase is None:
        return

    await _run_phase_review(orch, phase)


async def _run_phase_review(orch: "Orchestrator", phase: Phase) -> None:
    """Run the phase-review tournament and apply its outcome.

    A-winner → ``review_status="accepted"``.
    B/AB-winner → parse direction → append corrective tasks. The tasks land
    at the end of the phase, where ``next_pending_task()`` will pick them
    up before advancing to the next phase.
    Exception → ``review_status="skipped"``, log warning, do not block
    forward progress.
    """
    from orchestrator.corrective_parser import parse_corrective_direction
    from orchestrator.phase_review_runner import (
        _phase_complexity_rollup,
        run_phase_review_tournament,
    )

    # Mark in-progress so a concurrent observer can see we're working.
    try:
        await orch.plan_manager.update_phase_meta(
            phase.id, review_status="in_progress"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.review_status_in_progress_failed",
            phase_id=phase.id,
            err=str(exc),
        )

    baseline_commit = phase.baseline_commit or ""
    # v0.21.0 B1: prefer the captured end-checkpoint SHA (set in
    # ``_maybe_record_phase_checkpoint``) so the diff range is phase-
    # isolated even when cross-phase parallelism is active. Falls back
    # to live HEAD when the checkpoint isn't recorded (e.g. legacy
    # plans, pre-v0.21.0 phases that never executed under the new
    # capture path).
    tip_commit = phase.end_checkpoint_commit or _git_rev_parse_head(orch.cwd) or ""
    spec_md = ""
    spec_path = autodev_root(orch.cwd) / "spec.md"
    if spec_path.exists():
        try:
            spec_md = spec_path.read_text(encoding="utf-8")
        except OSError:
            spec_md = ""

    try:
        outcome = await run_phase_review_tournament(
            orch, phase, baseline_commit, tip_commit, spec_md
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.phase_review_error",
            phase_id=phase.id,
            err=str(exc),
        )
        try:
            await orch.plan_manager.update_phase_meta(
                phase.id, review_status="skipped"
            )
        except Exception as exc2:  # noqa: BLE001
            logger.warning(
                "execute_phase.review_status_skipped_failed",
                phase_id=phase.id,
                err=str(exc2),
            )
        return

    if outcome.accept_phase:
        try:
            await orch.plan_manager.update_phase_meta(
                phase.id, review_status="accepted"
            )
            logger.info(
                "execute_phase.phase_review_accepted",
                phase_id=phase.id,
                winner=outcome.winner,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.review_status_accepted_failed",
                phase_id=phase.id,
                err=str(exc),
            )
        return

    # Non-A winner: parse the direction and inject corrective sub-tasks.
    if not outcome.corrective_direction:
        logger.warning(
            "execute_phase.phase_review_no_direction",
            phase_id=phase.id,
            winner=outcome.winner,
        )
        try:
            await orch.plan_manager.update_phase_meta(
                phase.id, review_status="skipped"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.review_status_skipped_failed",
                phase_id=phase.id,
                err=str(exc),
            )
        return

    # v0.37.0 H2: compute the phase's remaining corrective-task budget
    # BEFORE parsing. The budget is cumulative across all corrective
    # rounds for this phase so two consecutive B/AB-winner tournaments
    # cannot collectively breach the cap.
    # v0.37.0 H5: auto-scale on huge repos via the knob-keyed multiplier.
    # Defensive: orchestrator stubs in unit tests may lack ``_cwd``.
    _cap_base = int(getattr(orch.cfg, "max_corrective_tasks_per_phase", 8))
    _cwd_for_h5 = getattr(orch, "_cwd", None)
    if _cwd_for_h5 is not None:
        from orchestrator.huge_repo_overrides import apply_and_log_huge_repo_value

        _cap_eff = await apply_and_log_huge_repo_value(
            key="max_corrective_tasks_per_phase",
            base_value=float(_cap_base),
            cwd=_cwd_for_h5,
            cfg=orch.cfg,
            ledger_append=orch.plan_manager.ledger_append,
        )
        cap = int(round(_cap_eff))
    else:
        cap = _cap_base
    phase_corrective_count = len(phase.corrective_task_ids or [])
    # v0.38.0 I3: plan-scope ceiling fires alongside the per-phase ceiling.
    # The smaller remaining budget wins; ``cap_scope`` records which
    # ceiling was binding so the ledger op + warning attribution is correct.
    plan_cap = int(
        getattr(orch.cfg, "max_corrective_tasks_per_plan", 24)
    )
    plan_for_total = await orch.plan_manager.load()
    total_plan_corrective = 0
    if plan_for_total is not None:
        total_plan_corrective = sum(
            len(p.corrective_task_ids or []) for p in plan_for_total.phases
        )
    per_phase_remaining = max(0, cap - phase_corrective_count)
    per_plan_remaining = max(0, plan_cap - total_plan_corrective)
    remaining_budget = min(per_phase_remaining, per_plan_remaining)
    cap_scope = "plan" if per_plan_remaining < per_phase_remaining else "phase"
    binding_cap = plan_cap if cap_scope == "plan" else cap
    if remaining_budget == 0:
        logger.info(
            "execute_phase.corrective_cap_reached",
            phase_id=phase.id,
            cap=binding_cap,
            scope=cap_scope,
            site="phase_review",
            winner=outcome.winner,
        )
        try:
            await orch.plan_manager.ledger_append(
                op="corrective_cap_reached",
                payload={
                    "phase_id": phase.id,
                    "cap": binding_cap,
                    "scope": cap_scope,
                    "site": "phase_review",
                    "winner": outcome.winner,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.ledger_append_failed",
                op="corrective_cap_reached",
                err=str(exc),
            )
        try:
            await orch.plan_manager.update_phase_meta(
                phase.id, review_status="capped"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.review_status_capped_failed",
                phase_id=phase.id,
                err=str(exc),
            )
        # v0.38.0 I3 (HK5): the phase-review path also burns a
        # "skip" on the corrective round when the cap fires (the
        # round produces no new corrective tasks). Increment the
        # diagnostic counter so two cap-hits across two reviews
        # surface the stuck-loop warning.
        cap_action = str(
            getattr(orch.cfg, "corrective_cap_action", "soft_block_phase")
        )
        await _bump_skip_corrective_counter(
            orch, phase_id=phase.id, cap_action=cap_action
        )
        return

    rollup = _phase_complexity_rollup(phase)
    corrective_tasks = parse_corrective_direction(
        outcome.corrective_direction,
        phase_id=phase.id,
        base_task_count=len(phase.tasks),
        phase_complexity=rollup,
        max_tasks=remaining_budget,
    )
    if not corrective_tasks:
        logger.warning(
            "execute_phase.phase_review_no_corrective_parsed",
            phase_id=phase.id,
        )
        try:
            await orch.plan_manager.update_phase_meta(
                phase.id, review_status="skipped"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.review_status_skipped_failed",
                phase_id=phase.id,
                err=str(exc),
            )
        return

    try:
        # v0.37.0 H2: pass the cap to plan_manager for defence-in-depth
        # even though we already truncated upstream via the parser.
        # v0.38.0 I3: thread the plan-scope cap too so the defensive
        # layer can fire ``scope="plan"`` on a bypass.
        await orch.plan_manager.append_corrective_tasks(
            phase.id,
            corrective_tasks,
            max_corrective_tasks_per_phase=cap,
            max_corrective_tasks_per_plan=plan_cap,
        )
        logger.info(
            "execute_phase.phase_review_corrective_injected",
            phase_id=phase.id,
            winner=outcome.winner,
            count=len(corrective_tasks),
        )
        # v0.38.0 I3 (HK5): a successful corrective injection resets
        # the skip-loop counter on this phase so the warning only
        # fires on three consecutive skip rounds.
        await _reset_skip_corrective_counter(orch, phase_id=phase.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.append_corrective_failed",
            phase_id=phase.id,
            err=str(exc),
        )


async def _execute_one(
    orch: "Orchestrator",
    task: Task,
    worktree_mgr: "WorktreeManager | None" = None,
) -> Task:
    """Run the developer -> reviewer -> tests loop for one task. Returns the final task.

    v0.11.0: when ``worktree_mgr`` is supplied, the task runs in an
    isolated git worktree (``tournament_dir/tasks/<task_id>``) and the
    final diff is applied to main via ``apply_patch_to_main`` after the
    task completes. The worktree is removed in a ``finally`` clause so
    a worker exit (success or failure) always cleans up.

    When ``worktree_mgr`` is ``None`` the task runs in ``orch.cwd``
    directly — the legacy serial path used by the single-task CLI
    (``execute --task-id``) and by tests that don't initialize git.
    """
    retry_limit = orch.cfg.qa_retry_limit

    # Step 0: short-circuit non-agent-executable tasks (v0.6.1).
    # Tasks with a non-empty ``requires`` list are skipped programmatically —
    # the orchestrator never invokes any adapter for them. Mirrors the
    # ``status in ('complete', 'skipped')`` short-circuit in
    # :func:`run_execute_phase` for tasks that are already terminal, but
    # catches the *first-time* skip path before any FSM transition.
    if task.requires:
        blocked_reason = f"requires={list(task.requires)}"
        skipped_task = await orch.plan_manager.update_task_status(
            task.id,
            "skipped",
            meta={"blocked_reason": blocked_reason},
        )
        logger.info(
            "execute_phase.skip_requires",
            task_id=task.id,
            requires=list(task.requires),
        )
        return skipped_task

    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

    # v0.11.0: per-task worktree isolation. ``worktree`` is the cwd
    # passed into delegate() for every agent invocation in this task —
    # the developer writes there, QA gates run there, and the diff is
    # applied to main after completion. ``None`` keeps the legacy path
    # where everything happens in orch.cwd.
    worktree: Path | None = None
    if worktree_mgr is not None:
        # v0.17.0 S6: optional sparse-checkout. When the config flag is on,
        # narrow the worktree to ``phase.edit_scope or plan.edit_scope``
        # so huge repos don't materialize gigabytes of unrelated files
        # into every per-task worktree. Falls back to a full checkout
        # when (a) the flag is off, (b) no scope is declared, or (c)
        # git is older than 2.25 (pre-flighted in WorktreeManager.create).
        # v0.23.0 C1: sparse-by-default on huge repos. The legacy
        # ``worktree_sparse_checkout_enabled`` flag still works as an
        # explicit operator opt-in for non-huge repos. On huge repos
        # (when ``worktree_huge_repo_mode`` resolves on) sparse becomes
        # the default unless explicitly disabled with ``"off"``.
        _sparse_enabled = bool(orch.cfg.worktree_sparse_checkout_enabled)
        _huge_mode_cfg_for_sparse = getattr(
            orch.cfg, "worktree_huge_repo_mode", "auto"
        )
        _is_huge_for_sparse = bool(
            getattr(getattr(orch, "_repo_capacity", None), "is_huge", False)
        )
        if _huge_mode_cfg_for_sparse == "on" or (
            _huge_mode_cfg_for_sparse == "auto" and _is_huge_for_sparse
        ):
            _sparse_enabled = True
        sparse_paths: list[str] | None = None
        if _sparse_enabled:
            try:
                _plan_for_scope = await orch.plan_manager.load()
            except Exception:  # noqa: BLE001
                _plan_for_scope = None
            if _plan_for_scope is not None:
                _resolved: list[str] = []
                for _ph in _plan_for_scope.phases:
                    if _ph.id == task.phase_id:
                        _resolved = (
                            list(_ph.edit_scope)
                            if _ph.edit_scope is not None
                            else list(_plan_for_scope.edit_scope)
                        )
                        break
                else:
                    _resolved = list(_plan_for_scope.edit_scope)
                if _resolved:
                    sparse_paths = _resolved
            # huge-repo follow-up: when sparse is enabled (huge repo) but
            # NO edit_scope is declared (the common case — architects don't
            # always emit a plan/phase edit_scope), fall back to the task's
            # OWN claimed files (+ extended_scope) as the sparse cone.
            # Without this, ``sparse_paths`` stays None →
            # ``create_per_task`` does a FULL checkout, which on a huge LFS
            # repo materializes the entire tree and produces a ~62 MB
            # checkout/LFS "phantom diff" that trips the diff-size guardrail
            # and blocks the task. The cone must contain the files the
            # developer needs to read/edit; ``create_per_task`` additionally
            # folds in their sibling headers. Empty → leave None (legacy
            # full checkout) so a task that genuinely claims no files is
            # unaffected.
            if not sparse_paths:
                _task_cone = [
                    p
                    for p in (
                        list(getattr(task, "files", []) or [])
                        + list(getattr(task, "extended_scope", []) or [])
                    )
                    if isinstance(p, str) and p.strip()
                ]
                if _task_cone:
                    # Deduplicate while preserving first-seen order.
                    _seen: set[str] = set()
                    sparse_paths = [
                        p
                        for p in _task_cone
                        if not (p in _seen or _seen.add(p))
                    ]
                    logger.info(
                        "execute_phase.sparse_cone_from_task_files",
                        task_id=task.id,
                        paths=sparse_paths,
                    )
        try:
            worktree = await worktree_mgr.create_per_task(
                task.id,
                sparse_paths=sparse_paths,
                include_headers_for_sparse=bool(
                    getattr(orch.cfg, "include_headers_for_sparse", True)
                ),
            )
        except WorktreeError as exc:
            logger.warning(
                "execute_phase.worktree_create_failed",
                task_id=task.id,
                err=str(exc),
            )
            worktree = None
        # v0.34.0 B2: emit the ``sparse_worktree_expanded`` ledger op
        # when the worktree manager folded sibling headers into the
        # sparse set. Best-effort — ledger failures must never block
        # task dispatch (this mirrors the existing ``attempt_started``
        # pattern downstream).
        added_h = getattr(worktree_mgr, "last_sparse_headers_added", 0) or 0
        if added_h:
            try:
                await orch.plan_manager.ledger_append(
                    op="sparse_worktree_expanded",
                    payload={
                        "task_id": task.id,
                        "added_paths": int(added_h),
                        "mode": "sibling_headers",
                    },
                )
            except Exception:  # noqa: BLE001 — telemetry-only
                pass

    orch.guardrails.start_task(task.id)
    try:
        # Retry loop — one iteration = one developer-then-gates cycle.
        last_issues: list[str] = []
        # v0.32.0 Phase 3 (Gap C): per-task attempt counters for the
        # infrastructure-class test diagnoses (collection_failed /
        # runtime_crash / capture_failed). Lives in this function's
        # scope (NOT task.metadata) because ``task`` is reassigned to
        # a fresh model on every ``update_task_status`` call inside
        # the loop, which would clobber attribute mutations. A simple
        # local dict survives the entire ``while True`` body.
        test_diag_attempts: dict[str, int] = {}
        # v0.38.0 I4: per-task cumulative backoff (seconds) consumed
        # by the H3 test-diag exponential backoff. Threaded into
        # ``InfraFailureCircuitBreaker.test_diag_budget_exhausted`` to
        # gate the hard halt; resets implicitly per task because the
        # variable lives in this function's scope (one ``_execute_one``
        # invocation per task).
        cumulative_backoff_s: float = 0.0
        while True:
            try:
                # v0.22.2 B3: emit a pre-flight marker BEFORE the developer
                # dispatch so resume can detect "evidence written but
                # ``coded`` op missing" (a process crash between
                # ``write_evidence`` at line 1771 and
                # ``update_task_status("coded")`` at line 1818). Audit-only
                # — does NOT mutate plan state.
                try:
                    from state.ledger import append_entry as _append_entry
                    from state.lockfile import plan_lock as _plan_lock

                    async with _plan_lock(
                        orch.cwd,
                        timeout_s=getattr(
                            orch.plan_manager, "_lock_timeout_s", 30.0
                        ),
                    ):
                        await _append_entry(
                            orch.cwd,
                            op="attempt_started",
                            payload={
                                "task_id": task.id,
                                "attempt_n": task.retry_count,
                                "started_at": _dt.datetime.now(
                                    _dt.timezone.utc
                                ).isoformat(),
                                "session_id": getattr(
                                    orch.plan_manager, "_session_id", ""
                                ),
                            },
                            session_id=getattr(
                                orch.plan_manager, "_session_id", ""
                            ),
                        )
                except Exception as exc:  # noqa: BLE001 — best-effort marker
                    logger.debug(
                        "execute_phase.attempt_started_emit_failed",
                        task_id=task.id,
                        err=str(exc),
                    )
                developer_env = _developer_envelope(task, extra_issues=last_issues)
                developer_result = await delegate(
                    orch,
                    "developer",
                    developer_env,
                    retry_count=task.retry_count,
                    last_issues=last_issues,
                    task=task,
                    cwd_override=worktree,
                )
            except GuardrailExceededError as exc:
                logger.warning(
                    "execute_phase.guardrail_exceeded",
                    task_id=task.id,
                    reason=str(exc),
                )
                # v0.42.1 F1: single chokepoint (was guard + legacy block).
                # The consolidated meta-builder still stamps the RecoveryHint.
                task = await block_task(
                    orch,
                    task,
                    failure_class=_fcls.GUARDRAIL_EXCEEDED,
                    raw_error=str(exc),
                    meta=_build_guardrail_block_meta(
                        orch=orch, task_id=task.id, exc=exc
                    ),
                )
                return task

            coder_ev = CoderEvidence(
                task_id=task.id,
                diff=developer_result.diff,
                files_changed=[str(p) for p in developer_result.files_changed],
                output_text=developer_result.text,
                duration_s=developer_result.duration_s,
                success=developer_result.success,
                # v0.31.0 (Phase 1.2): preserve raw response symmetrically
                # with ReviewEvidence / TestEvidence.
                raw_response=developer_result.text,
            )
            await write_evidence(orch.cwd, task.id, coder_ev)
            if developer_result.diff:
                await write_patch(orch.cwd, task.id, developer_result.diff)

            if not developer_result.success:
                logger.warning(
                    "execute_phase.developer_failed",
                    task_id=task.id,
                    err=developer_result.error,
                )
                # Tier J (huge-repo): accept an APPROVED-but-turn-exhausted
                # task instead of losing the approved (empty-diff) result to
                # a ``user_decision_required`` soft-block. Strictly gated —
                # fires ONLY when the failure is pure turn-exhaustion AND a
                # reviewer ``APPROVED`` verdict is already on record for an
                # empty diff (see ``_maybe_accept_approved_on_exhaustion``).
                # Returns ``None`` (no-op) on a genuine semantic failure /
                # non-approved / non-empty-diff task, so this never masks a
                # real failure.
                _accepted = await _maybe_accept_approved_on_exhaustion(
                    orch, task, developer_result
                )
                if _accepted is not None:
                    return _accepted
                # v0.39.0 (Cluster C2c): runtime under-decomposition
                # telemetry. Best-effort, purely observational — never
                # changes control flow (falls through to the existing
                # retry/escalate path below unchanged).
                await maybe_emit_under_decomposed_runtime(
                    orch, task, developer_result
                )
                # v0.20.0 C3: dynamic scope expansion on missing-file
                # error. Inspect the adapter output for paths that look
                # like sparse-checkout misses; if any are covered by the
                # task's extended_scope / phase / plan edit_scope, widen
                # the worktree and retry once. This is a one-shot
                # repair — repeat misses fall through to normal retry.
                if (
                    worktree is not None
                    and worktree_mgr is not None
                    and not task.metadata.get("dynamic_scope_expansion_used")
                ):
                    expanded = await _maybe_expand_sparse_for_missing(
                        orch=orch,
                        task=task,
                        worktree=worktree,
                        worktree_mgr=worktree_mgr,
                        adapter_text=(
                            (developer_result.text or "")
                            + "\n"
                            + (developer_result.error or "")
                        ),
                    )
                    if expanded:
                        # Mark + retry once.
                        task.metadata["dynamic_scope_expansion_used"] = True
                        last_issues = [
                            developer_result.error or "missing-file; sparse-expanded"
                        ]
                        continue
                task = await _try_retry_or_escalate(
                    orch,
                    task,
                    retry_limit,
                    reason=_build_adapter_failure_reason(developer_result),
                )
                if task.escalated:
                    return task
                last_issues = [developer_result.error or "adapter failure"]
                continue

            # Gap 5 (containment): reject a developer diff confined ENTIRELY
            # to AutoDev's own ``.autodev/`` directory. AutoDev owns that
            # dir in the target repo (evidence / ledger / tournament / index
            # state); a task agent editing only those files has perceived
            # AutoDev's internals as the work to do, not the target code.
            # That is never legitimate task output, so route it through the
            # same retry/escalate path a QA-gate failure uses rather than
            # letting it flow to the reviewer (where an APPROVED verdict on
            # a no-op-to-the-target diff could carry it to ``complete``).
            # No-op for empty diffs (research tasks) and for any diff that
            # touches at least one path outside ``.autodev/``.
            if _diff_confined_to_autodev(developer_result):
                _autodev_files = extract_files_from_diff(
                    developer_result.diff or "", strict=False
                )
                logger.warning(
                    "execute_phase.containment_violation_autodev_paths",
                    task_id=task.id,
                    files=_autodev_files[:20],
                )
                try:
                    await orch.plan_manager.ledger_append(
                        op="containment_violation_autodev_paths",
                        payload={
                            "task_id": task.id,
                            "files": _autodev_files[:20],
                        },
                    )
                except Exception as exc:  # noqa: BLE001 — best-effort breadcrumb
                    logger.warning(
                        "execute_phase.ledger_append_failed",
                        op="containment_violation_autodev_paths",
                        err=str(exc),
                    )
                _containment_reason = (
                    "containment_violation: developer diff is confined to "
                    "AutoDev's own .autodev/ directory "
                    f"({', '.join(_autodev_files[:5])}) — edit the TARGET "
                    "repository's code, not AutoDev's internal run state"
                )
                task = await _try_retry_or_escalate(
                    orch, task, retry_limit, reason=_containment_reason
                )
                if task.escalated:
                    return task
                last_issues = [_containment_reason]
                continue

            task = await orch.plan_manager.update_task_status(task.id, "coded")

            # Step 3: auto-gates (syntax/lint/build/test_runner/secretscan).
            # v0.13.0: pass developer_result so secretscan can scope to the
            # diff (skip pre-existing repo state).
            gate_failure = await _run_qa_gates(
                orch, task, developer_result=developer_result
            )
            if gate_failure is not None:
                logger.warning(
                    "execute_phase.qa_gate_failed",
                    task_id=task.id,
                    details=gate_failure,
                )
                task = await _try_retry_or_escalate(
                    orch, task, retry_limit, reason=gate_failure
                )
                if task.escalated:
                    return task
                last_issues = [gate_failure]
                continue
            task = await orch.plan_manager.update_task_status(task.id, "auto_gated")

            # Step 4: reviewer.
            #
            # v0.32.0 Phase 2: when ``cfg.tournaments.review_tournament_enabled``,
            # swap the legacy single-shot ``delegate(..., "reviewer", ...)``
            # call for an A/B/AB tournament routed through
            # :func:`orchestrator.review_tournament_runner.run_review_tournament`.
            # Default ``False`` for one cycle (real-world telemetry needed
            # before flipping the default in v0.33.0). All v0.31.0
            # instrumentation is preserved by construction:
            #   * each candidate gets the same chunked envelope (Phase 1.4),
            #   * each judge's verdict parses through ``_parse_review_verdict``
            #     so MALFORMED still classifies (Phase 1.3),
            #   * ``raw_response`` is captured per candidate (Phase 1.2),
            #   * the empty-result dump path is still inside ``delegate``.
            review_env = _review_envelope(task, coder_ev.diff or "")
            review_result_text: str = ""
            # v0.41.0 (A1): hold the reviewer adapter result so the MALFORMED
            # branch below can distinguish "reviewer ran out of turns" (infra)
            # from "developer produced a bad diff" (genuine MALFORMED). Only
            # the non-tournament path populates this directly; the tournament
            # path falls back to ``orch._last_adapter_subtype``.
            review_result: AgentResult | None = None
            review_tournament_outcome: (
                "ReviewTournamentResult | None"
            ) = None
            if getattr(
                orch.cfg.tournaments, "review_tournament_enabled", False
            ):
                from orchestrator.review_tournament_runner import (
                    ReviewTournamentResult,
                    run_review_tournament,
                )

                try:
                    review_tournament_outcome = await run_review_tournament(
                        orch,
                        task,
                        coder_ev,
                        review_env,
                        cwd_override=worktree,
                    )
                except GuardrailExceededError as exc:
                    logger.warning(
                        "execute_phase.guardrail_exceeded",
                        task_id=task.id,
                        reason=str(exc),
                    )
                    # v0.42.1 F1: single chokepoint (was guard + legacy block).
                    # The consolidated meta-builder still stamps the RecoveryHint.
                    task = await block_task(
                        orch,
                        task,
                        failure_class=_fcls.GUARDRAIL_EXCEEDED,
                        raw_error=str(exc),
                        meta=_build_guardrail_block_meta(
                            orch=orch, task_id=task.id, exc=exc
                        ),
                    )
                    return task

                verdict = review_tournament_outcome.winning_verdict
                issues = list(review_tournament_outcome.winning_issues)
                review_result_text = (
                    review_tournament_outcome.evidence.candidates.get(
                        review_tournament_outcome.winning_label,
                        next(
                            iter(
                                review_tournament_outcome.evidence.candidates.values()
                            )
                        ),
                    ).raw_response
                    or ""
                )
                # v0.32.0 Phase 2: when the tournament escalated (max
                # rounds without convergence) route to the existing
                # critic_sounding_board rung — preserves the legacy
                # escalation path.
                if review_tournament_outcome.escalated:
                    logger.warning(
                        "execute_phase.review_tournament_escalated",
                        task_id=task.id,
                        tournament_id=review_tournament_outcome.tournament_id,
                        rounds=review_tournament_outcome.rounds,
                    )
                    task = await _try_retry_or_escalate(
                        orch,
                        task,
                        retry_limit,
                        reason="review_tournament max_rounds",
                    )
                    if task.escalated:
                        return task
                    last_issues = issues or [
                        "review_tournament max_rounds without convergence"
                    ]
                    continue
            else:
                try:
                    review_result = await delegate(
                        orch,
                        "reviewer",
                        review_env,
                        retry_count=task.retry_count,
                        last_issues=last_issues,
                        cwd_override=worktree,
                    )
                except GuardrailExceededError as exc:
                    logger.warning(
                        "execute_phase.guardrail_exceeded",
                        task_id=task.id,
                        reason=str(exc),
                    )
                    # v0.42.1 F1: single chokepoint (was guard + legacy block).
                    # The consolidated meta-builder still stamps the RecoveryHint.
                    task = await block_task(
                        orch,
                        task,
                        failure_class=_fcls.GUARDRAIL_EXCEEDED,
                        raw_error=str(exc),
                        meta=_build_guardrail_block_meta(
                            orch=orch, task_id=task.id, exc=exc
                        ),
                    )
                    return task

                verdict, issues = _parse_review_verdict(review_result.text)
                review_result_text = review_result.text

            review_ev = ReviewEvidence(
                task_id=task.id,
                verdict=cast(
                    "Literal['APPROVED', 'NEEDS_CHANGES', 'REJECTED', 'MALFORMED']",
                    verdict,
                ),
                issues=issues,
                output_text=review_result_text,
                # v0.31.0 (Phase 1.2): always carry the raw model
                # response so post-mortems can answer "what did the
                # reviewer actually say?" without needing the
                # ``.autodev/debug/*-empty.json`` dumps. Same string we
                # already write to ``output_text`` today, but explicitly
                # named so future parser changes that re-derive
                # ``output_text`` (e.g. stripping the verdict line)
                # don't lose the original.
                raw_response=review_result_text,
            )
            await write_evidence(orch.cwd, task.id, review_ev)
            # v0.41.0 (A1): a MALFORMED verdict is ambiguous at the text
            # layer — it covers BOTH "reviewer ran out of turns and emitted
            # an empty/truncated response" (an INFRA failure) and "developer
            # produced a diff so broken the reviewer couldn't form a verdict"
            # (a genuine content/format failure). Disambiguate on the
            # reviewer's adapter ``subtype`` BEFORE routing: turn-exhaustion
            # must NOT be charged to the developer as a bad diff (the Run-3
            # failure mode that looped 4× then blocked a correct diff).
            if verdict == "MALFORMED" and _reviewer_exhausted_turns(
                orch, review_result
            ):
                # INFRA path: retry the *reviewer* with an escalated turn
                # budget (the per-(task, role) budget-escalation tracker in
                # ``delegate`` auto-bumps max_turns on consecutive
                # ``error_max_turns`` for the same pair), then re-parse.
                logger.warning(
                    "execute_phase.reviewer_infra_exhausted",
                    task_id=task.id,
                    subtype=(
                        review_result.subtype
                        if review_result is not None
                        else getattr(orch, "_last_adapter_subtype", None)
                    ),
                    note=(
                        "reviewer exhausted its turn budget; classifying as "
                        "an infra failure (NOT a developer-discard) and "
                        "retrying the reviewer with an escalated budget."
                    ),
                )
                retry_review_result: AgentResult | None = None
                try:
                    retry_review_result = await delegate(
                        orch,
                        "reviewer",
                        review_env,
                        retry_count=task.retry_count,
                        last_issues=last_issues,
                        cwd_override=worktree,
                    )
                except GuardrailExceededError as exc:
                    logger.warning(
                        "execute_phase.guardrail_exceeded",
                        task_id=task.id,
                        reason=str(exc),
                    )
                    # v0.42.1 F1: single chokepoint (was guard + legacy block).
                    task = await block_task(
                        orch,
                        task,
                        failure_class=_fcls.GUARDRAIL_EXCEEDED,
                        raw_error=str(exc),
                        meta=_build_guardrail_block_meta(
                            orch=orch, task_id=task.id, exc=exc
                        ),
                    )
                    return task

                retry_verdict, retry_issues = _parse_review_verdict(
                    retry_review_result.text
                )
                if retry_verdict != "MALFORMED" or not _reviewer_exhausted_turns(
                    orch, retry_review_result
                ):
                    # The escalated retry produced a parseable verdict (or a
                    # genuine non-exhaustion MALFORMED). Re-stamp evidence and
                    # fall through to the normal verdict routing below.
                    verdict = retry_verdict
                    issues = retry_issues
                    review_result = retry_review_result
                    review_result_text = retry_review_result.text
                    review_ev = ReviewEvidence(
                        task_id=task.id,
                        verdict=cast(
                            "Literal['APPROVED', 'NEEDS_CHANGES', "
                            "'REJECTED', 'MALFORMED']",
                            verdict,
                        ),
                        issues=issues,
                        output_text=review_result_text,
                        raw_response=review_result_text,
                    )
                    await write_evidence(orch.cwd, task.id, review_ev)
                else:
                    # SOFT-PASS: the reviewer still ran out of room after the
                    # escalated budget. Accept the developer's diff rather
                    # than discarding correct work for an infra reason. Stamp
                    # a durable, replay-safe APPROVED ReviewEvidence carrying
                    # the soft-pass marker, log the event, and fall through to
                    # the test step (verdict normalised to APPROVED below).
                    logger.warning(
                        "execute_phase.reviewer_infra_softpass",
                        task_id=task.id,
                        note=(
                            "reviewer exhausted its turn budget even after a "
                            "budget escalation; SOFT-PASSING the review and "
                            "accepting the developer diff (infra failure, not "
                            "a bad diff)."
                        ),
                    )
                    verdict = "APPROVED"
                    issues = [
                        "reviewer_infra_softpass: reviewer exhausted turns "
                        "(escalated); developer diff accepted without a "
                        "reviewer verdict"
                    ]
                    review_result_text = retry_review_result.text
                    review_ev = ReviewEvidence(
                        task_id=task.id,
                        verdict="APPROVED",
                        issues=issues,
                        output_text=review_result_text,
                        raw_response=review_result_text,
                    )
                    await write_evidence(orch.cwd, task.id, review_ev)
            # v0.31.0 (Phase 1.3): a genuine MALFORMED (the reviewer did NOT
            # exhaust turns — the response was unparseable for some other
            # reason) is still a machinery/format failure. Route it through
            # the same retry path as NEEDS_CHANGES but tag the reason so the
            # ledger / logs make the distinction visible.
            if verdict == "MALFORMED":
                logger.warning(
                    "execute_phase.review_malformed",
                    task_id=task.id,
                    issues=issues,
                    note=(
                        "parser could not extract a verdict; treating as "
                        "NEEDS_CHANGES with warning. Inspect "
                        ".autodev/debug/*-empty.json for forensics."
                    ),
                )
                task = await _try_retry_or_escalate(
                    orch, task, retry_limit, reason="reviewer MALFORMED"
                )
                if task.escalated:
                    return task
                last_issues = issues or ["reviewer MALFORMED"]
                continue
            if verdict in ("NEEDS_CHANGES", "REJECTED"):
                logger.info(
                    "execute_phase.review_needs_changes",
                    task_id=task.id,
                    verdict=verdict,
                    issues=issues,
                )
                task = await _try_retry_or_escalate(
                    orch, task, retry_limit, reason=f"reviewer {verdict}"
                )
                if task.escalated:
                    return task
                last_issues = issues or [f"reviewer {verdict}"]
                continue
            task = await orch.plan_manager.update_task_status(task.id, "reviewed")

            # Step 5: test_engineer generates and runs tests.
            try:
                test_env = _test_envelope(task, coder_ev.diff or "")
                test_result = await delegate(
                    orch,
                    "test_engineer",
                    test_env,
                    retry_count=task.retry_count,
                    last_issues=last_issues,
                    cwd_override=worktree,
                )
            except GuardrailExceededError as exc:
                logger.warning(
                    "execute_phase.guardrail_exceeded",
                    task_id=task.id,
                    reason=str(exc),
                )
                # v0.42.1 F1: single chokepoint (was guard + legacy block).
                # The consolidated meta-builder still stamps the RecoveryHint.
                task = await block_task(
                    orch,
                    task,
                    failure_class=_fcls.GUARDRAIL_EXCEEDED,
                    raw_error=str(exc),
                    meta=_build_guardrail_block_meta(
                        orch=orch, task_id=task.id, exc=exc
                    ),
                )
                return task

            passed, failed, total = _parse_test_counts(test_result.text)
            # v0.32.0 Phase 3 (Gap C): six-way self-diagnostic so the
            # orchestrator can distinguish "no tests existed" from
            # "runner crashed" from "stdout capture failed". The
            # classifier is a pure data transformation and never raises;
            # the result is persisted on TestEvidence for forensics
            # AND drives the branching below.
            diagnosis = classify_test_result(
                test_result, (passed, failed, total)
            )
            stderr_tail = redact_stderr_tail(
                getattr(test_result, "raw_stderr", "") or "", tail_chars=1000
            )
            test_ev = TestEvidence(
                task_id=task.id,
                passed=passed,
                failed=failed,
                total=total,
                output_text=test_result.text,
                # v0.31.0 (Phase 1.2): preserve raw response symmetrically
                # with ReviewEvidence — same rationale, same field shape.
                raw_response=test_result.text,
                diagnosis=diagnosis,
                runner_stderr_tail=stderr_tail or None,
            )
            await write_evidence(orch.cwd, task.id, test_ev)

            # v0.39.0 (Cluster A2b): runtime auto-soft-pass fallback. Track
            # consecutive ``capture_failed`` on a huge repo and, after >=2
            # in a row, flip ``treat_unrunnable_tests_as_no_tests`` in-memory
            # so the CURRENT task can benefit — the gate below reads the
            # flag live via getattr. No-op on small repos / escape hatch /
            # when already enabled.
            maybe_enable_auto_soft_pass(orch, diagnosis)

            if diagnosis == "no_tests_found" or (
                diagnosis in ("capture_failed", "collection_failed", "runtime_crash")
                and getattr(
                    orch.cfg, "treat_unrunnable_tests_as_no_tests", False
                )
            ):
                # Legitimate: no tests exist for the changed code — OR
                # (``treat_unrunnable_tests_as_no_tests``) the target repo
                # cannot be built/tested in this environment, so an
                # infra-class capture failure is not a code defect.
                # Soft-pass to the next step rather than hard-failing.
                # The real diagnosis is preserved on
                # ``TestEvidence.diagnosis`` (already written above) —
                # downstream consumers (e.g. ``autodev status``) read the
                # evidence JSON, not the in-memory task.
                logger.info(
                    "execute_phase.tests_no_tests_found",
                    task_id=task.id,
                    diagnosis=diagnosis,
                )
                task = await orch.plan_manager.update_task_status(
                    task.id, "tested"
                )
            elif diagnosis == "ok" and failed == 0 and test_result.success:
                # Existing happy path — all tests passed.
                # v0.38.0 I4: feed the auto-reset success counter so a
                # healthy run can clear a prior flaky burst without
                # operator intervention. Defensive ``getattr`` mirrors
                # the delegate-site pattern for test stubs without a
                # breaker; method itself is best-effort + idempotent.
                _breaker_i4 = getattr(orch, "_circuit_breaker", None)
                if _breaker_i4 is not None and hasattr(
                    _breaker_i4, "record_test_success"
                ):
                    _breaker_i4.record_test_success(
                        task.id,
                        _dt.datetime.now(_dt.timezone.utc),
                    )
                task = await orch.plan_manager.update_task_status(
                    task.id, "tested"
                )
            elif diagnosis == "ok":
                # Tests collected and ran, but at least one failed
                # (or runner reported non-success). Existing retry
                # behaviour applies.
                logger.info(
                    "execute_phase.tests_failed",
                    task_id=task.id,
                    failed=failed,
                    total=total,
                )
                task = await _try_retry_or_escalate(
                    orch, task, retry_limit, reason="tests failed"
                )
                if task.escalated:
                    return task
                last_issues = [
                    f"{failed}/{total} tests failed",
                    test_result.text[:500],
                ]
                continue
            elif diagnosis in (
                "collection_failed",
                "runtime_crash",
                "capture_failed",
            ):
                # Infrastructure-class failures: retry once, then
                # hard-fail with the diagnosis preserved in evidence.
                attempts = test_diag_attempts.get(diagnosis, 0) + 1
                test_diag_attempts[diagnosis] = attempts

                # v0.37.0 H3 / v0.38.0 I4: feed the cross-task test-
                # diag breaker BEFORE branching on retry-vs-hard-fail
                # so the count advances on every occurrence (including
                # the first retry attempt). Defensive ``getattr``
                # matches the delegate-site pattern — orchestrator
                # stubs without a breaker silently no-op. The breaker
                # itself ignores diagnoses not in
                # ``cfg.test_diag_breaker_diagnoses`` (default:
                # ``capture_failed`` only), so feeding all three here
                # is safe and keeps the routing simple.
                #
                # I4 (HK7): on threshold cross the breaker no longer
                # hard-halts immediately — it returns the next
                # exponential backoff via
                # ``next_backoff_s_for_test_diag``. The orchestrator
                # sleeps for that delay, accumulates into the per-
                # task ``cumulative_backoff_s``, and only raises the
                # hard halt once the cumulative crosses the
                # configured budget (the operator-observed real-world
                # enterprise-runs flaky pattern that motivated I4
                # often resolves in a single backoff iteration).
                _breaker_h3 = getattr(orch, "_circuit_breaker", None)
                if _breaker_h3 is not None:
                    _breaker_h3.record_test_diagnosis(
                        task.id,
                        diagnosis,
                        _dt.datetime.now(_dt.timezone.utc),
                    )
                    # Adapter-class stream check (unchanged path).
                    _halt_adapter, _reason_adapter = _breaker_h3.should_halt()
                    if _halt_adapter:
                        # Adapter-class trip still hard-halts here for
                        # back-compat with v0.30.0 (auth_failed mixed
                        # with capture_failed bursts).
                        logger.error(
                            "execute_phase.test_diag_breaker_trip",
                            task_id=task.id,
                            diagnosis=diagnosis,
                            count=len(_breaker_h3._test_diag_failures),
                            window_s=_breaker_h3.test_diag_window_s,
                        )
                        raise InfrastructureCircuitOpenError(
                            _reason_adapter
                            or "infrastructure circuit open",
                            halted_task_id=task.id,
                        )
                    # I4: test-diag stream — backoff-then-budget.
                    backoff_s = _breaker_h3.next_backoff_s_for_test_diag()
                    if backoff_s is not None:
                        cumulative_backoff_s += backoff_s
                        tripped, reason = (
                            _breaker_h3.test_diag_budget_exhausted(
                                cumulative_backoff_s
                            )
                        )
                        if tripped:
                            logger.error(
                                "execute_phase.test_diag_budget_exhausted",
                                task_id=task.id,
                                diagnosis=diagnosis,
                                cumulative_s=cumulative_backoff_s,
                                budget_s=(
                                    _breaker_h3.test_diag_backoff_total_budget_s
                                ),
                            )
                            raise InfrastructureCircuitOpenError(
                                reason
                                or "test-diagnosis backoff budget exhausted",
                                halted_task_id=task.id,
                            )
                        logger.warning(
                            "execute_phase.test_diag_backoff",
                            task_id=task.id,
                            diagnosis=diagnosis,
                            backoff_s=backoff_s,
                            cumulative_s=cumulative_backoff_s,
                            budget_s=(
                                _breaker_h3.test_diag_backoff_total_budget_s
                            ),
                        )
                        await asyncio.sleep(backoff_s)
                        # Continue retry — don't raise. The cross-task
                        # breaker above is the systemic-halt path; the
                        # per-task retry/soft-pass decision below gates a
                        # SINGLE task's fate (and, post-v0.41.0, soft-passes
                        # an uncapturable ``capture_failed`` rather than
                        # blocking it).

                # v0.41.0 (P1-F): bounded soft-pass for ``capture_failed``.
                # The cross-task circuit breaker (above) still trips on a
                # SYSTEMICALLY broken runner — many tasks each emitting
                # ``capture_failed`` exhaust the backoff budget and raise
                # ``InfrastructureCircuitOpenError`` (operator-halt). But a
                # SINGLE otherwise-passing task whose test RESULT merely
                # cannot be CAPTURED (empty ``text``/``raw_stderr``,
                # ``total==0`` and — critically — zero captured ``failed``)
                # should NOT hard-fail to ``blocked``. The legacy
                # ``attempts==2`` branch below did exactly that, looping the
                # task reviewed→in_progress→reviewed until its retries were
                # spent (observed on a trivial one-line edit to
                # ``tooling_agent.py``). After
                # ``cfg.capture_failed_soft_pass_after`` consecutive
                # uncapturable attempts on this task, advance to ``tested``
                # (falling through to Step 6 like the happy path) and stamp
                # ``soft_passed=True`` on the evidence.
                #
                # CRITICAL invariants — only soft-pass a GENUINE capture
                # failure:
                #   * ``diagnosis == "capture_failed"`` only. ``collection_
                #     failed`` / ``runtime_crash`` carry signal (collection
                #     errors, timeouts/kills) and keep retry-then-hard-fail.
                #   * ``failed == 0`` — a real RED test parses ``total>0`` →
                #     diagnosis ``ok`` and never reaches this branch, so a
                #     captured failure is NEVER soft-passed; this guard is
                #     belt-and-suspenders.
                soft_pass_after = getattr(
                    orch.cfg, "capture_failed_soft_pass_after", 2
                )
                if (
                    diagnosis == "capture_failed"
                    and failed == 0
                    and attempts >= soft_pass_after
                ):
                    soft_pass_reason = (
                        f"capture_failed persisted across {attempts} "
                        f"attempt(s) with no captured failures "
                        f"(passed={passed} failed={failed} total={total}); "
                        f"test result could not be captured — soft-passing "
                        f"an otherwise-passing task instead of blocking"
                    )
                    logger.warning(
                        "execute_phase.test_capture_soft_pass",
                        task_id=task.id,
                        diagnosis=diagnosis,
                        attempts=attempts,
                        soft_pass_after=soft_pass_after,
                        reason=soft_pass_reason,
                    )
                    # Re-stamp the evidence (atomic overwrite of the same
                    # ``{task_id}-test.json``) so downstream consumers and
                    # forensics see the soft-pass marker alongside the
                    # preserved ``capture_failed`` diagnosis.
                    test_ev_soft = TestEvidence(
                        task_id=task.id,
                        passed=passed,
                        failed=failed,
                        total=total,
                        output_text=test_result.text,
                        raw_response=test_result.text,
                        diagnosis=diagnosis,
                        runner_stderr_tail=stderr_tail or None,
                        soft_passed=True,
                        soft_pass_reason=soft_pass_reason,
                    )
                    await write_evidence(orch.cwd, task.id, test_ev_soft)
                    task = await orch.plan_manager.update_task_status(
                        task.id, "tested"
                    )
                    # Fall through to Step 6 (impl tournament) — do NOT
                    # ``continue``/``return``; the task advances toward
                    # ``complete`` exactly like the ``no_tests_found`` /
                    # passing branches above.

                elif attempts == 1:
                    log_method = (
                        logger.error
                        if diagnosis == "capture_failed"
                        else logger.warning
                    )
                    log_method(
                        "execute_phase.test_diagnosis_retry",
                        task_id=task.id,
                        diagnosis=diagnosis,
                        attempt=attempts,
                    )
                    last_issues = [
                        f"test_engineer {diagnosis} — retrying",
                        test_result.text[:500],
                    ]
                    # Transition back to ``in_progress`` so the next
                    # loop iteration's developer dispatch is a valid
                    # state transition (mirrors what
                    # ``_try_retry_or_escalate`` does on the legacy
                    # tests-failed path). The retry-attempt counter
                    # lives in ``test_diag_attempts`` (function-scope
                    # local) and survives ``task`` reassignments.
                    task = await orch.plan_manager.update_task_status(
                        task.id, "in_progress"
                    )
                    continue
                else:
                    # Second occurrence of a signal-bearing infra failure
                    # (``collection_failed`` / ``runtime_crash``), or a
                    # ``capture_failed`` with captured failures / soft-pass
                    # disabled — hard-fail with the diagnosis preserved.
                    logger.error(
                        "execute_phase.test_diagnosis_hard_fail",
                        task_id=task.id,
                        diagnosis=diagnosis,
                        attempts=attempts,
                    )
                    # v0.32.0 (Phase 5, Gap G): structured recovery hint so
                    # the operator sees the exact diagnosis + the evidence
                    # path containing the captured stderr tail.
                    hint_hardfail = _build_recovery_hint(
                        task_id=task.id,
                        hint_class="missing_test_output",
                        action=(
                            f"Test runner produced an infrastructure-class "
                            f"failure ({diagnosis}) that persisted across "
                            f"{attempts} attempts. Inspect "
                            f".autodev/evidence/{task.id}-test.json (stderr "
                            f"tail captured) to fix the runner, then "
                            f"`autodev requeue --task {task.id}`."
                        ),
                        evidence_files=[
                            f".autodev/evidence/{task.id}-test.json"
                        ],
                        commands=[
                            f"autodev requeue --task {task.id}",
                            "autodev doctor",
                        ],
                    )
                    # v0.42.1 F1: single chokepoint (was guard + legacy block).
                    task = await block_task(
                        orch,
                        task,
                        failure_class=_fcls.TEST_DIAGNOSIS_HARDFAIL,
                        raw_error=f"test_diagnosis: {diagnosis}",
                        failing_role="test_engineer",
                        meta={
                            "blocked_reason": (
                                f"test_diagnosis: {diagnosis} "
                                f"(persisted across {attempts} attempts)"
                            ),
                            "recovery_hint": hint_hardfail,
                        },
                    )
                    return task
            else:
                # ``no_signal`` — soft-block with an explicit reason
                # rather than masquerading as a generic test failure.
                logger.warning(
                    "execute_phase.test_diagnosis_no_signal",
                    task_id=task.id,
                )
                # v0.32.0 (Phase 5, Gap G): structured hint pointing to
                # the test evidence + suggesting the user verify tests
                # are discoverable.
                hint_no_signal = _build_recovery_hint(
                    task_id=task.id,
                    hint_class="missing_test_output",
                    action=(
                        f"The test gate produced no diagnostic signal. "
                        f"Inspect .autodev/evidence/{task.id}-test.json and "
                        f"ensure tests are discoverable; then "
                        f"`autodev requeue --task {task.id}`."
                    ),
                    evidence_files=[f".autodev/evidence/{task.id}-test.json"],
                    commands=[f"autodev requeue --task {task.id}"],
                )
                # v0.42.1 F1: single chokepoint (was guard + legacy block).
                task = await block_task(
                    orch,
                    task,
                    failure_class=_fcls.TEST_DIAGNOSIS_NO_SIGNAL,
                    raw_error="test result inconclusive — no diagnostic signal",
                    failing_role="test_engineer",
                    meta={
                        "blocked_reason": (
                            "test result inconclusive — "
                            "no diagnostic signal"
                        ),
                        "recovery_hint": hint_no_signal,
                    },
                )
                return task

            # Step 6: impl tournament.
            if orch.cfg.tournaments.impl.enabled and not orch.disable_impl_tournament:
                from orchestrator.impl_tournament_runner import (
                    run_impl_tournament,
                    run_multi_branch_impl_tournament,
                )
                from tournament import ImplBundle as _ImplBundle

                _initial_bundle = _ImplBundle(
                    task_id=task.id,
                    task_description=task.description,
                    diff=coder_ev.diff or "",
                    files_changed=coder_ev.files_changed,
                    tests_passed=passed,
                    tests_failed=failed,
                    tests_total=total,
                    test_output_excerpt=test_result.text[:1000],
                )
                # v0.21.0 A2: opt into the multi-branch impl-tournament fan-
                # out when ``cfg.tournaments.impl.num_branches > 1`` OR the
                # operator pinned a non-None ``branches`` list. Keeps the
                # single-branch path byte-identical when neither is set.
                _impl_cfg = orch.cfg.tournaments.impl
                _impl_branch_configs = (
                    list(_impl_cfg.branches) if _impl_cfg.branches else None
                )
                _impl_n_branches = (
                    len(_impl_branch_configs)
                    if _impl_branch_configs is not None
                    else _impl_cfg.num_branches
                )
                try:
                    if _impl_n_branches > 1:
                        try:
                            await run_multi_branch_impl_tournament(
                                orch,
                                task,
                                _initial_bundle,
                                n_branches=_impl_n_branches,
                                branch_configs=_impl_branch_configs,
                            )
                        except TournamentError as _mb_exc:
                            # Survivor floor or other fan-out failure —
                            # fall back to the single-branch path so the
                            # task still gets tournament refinement.
                            logger.warning(
                                "execute_phase.multi_branch_impl_fallback",
                                task_id=task.id,
                                err=str(_mb_exc),
                            )
                            await run_impl_tournament(
                                orch, task, _initial_bundle
                            )
                    else:
                        await run_impl_tournament(orch, task, _initial_bundle)
                except Exception as _exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.impl_tournament_error",
                        task_id=task.id,
                        err=str(_exc),
                    )
            task = await orch.plan_manager.update_task_status(task.id, "tournamented")

            # Step 7: extract and record lessons from agent outputs.
            await _record_lessons(orch, task.id, developer_result.text, "developer")
            # v0.32.0 Phase 2: ``review_result_text`` is the winning
            # candidate's raw response when the review tournament fired,
            # otherwise the legacy single-shot reviewer's output text.
            await _record_lessons(orch, task.id, review_result_text, "reviewer")
            await _record_lessons(orch, task.id, test_result.text, "test_engineer")

            # v0.11.0: when running in a per-task worktree, apply the
            # diff to the main repo BEFORE marking complete. This is
            # the convergence step that makes parallel execution safe:
            # only after a successful apply do dependent tasks see the
            # change. Apply failures route into critic-escalation:
            # critic_sounding_board is invoked in CONFLICT ESCALATION
            # MODE and its RESOLUTION directive determines the next
            # step (rebase-and-retry / abandon-task / rewrite).
            if worktree_mgr is not None and worktree is not None:
                applied = await _apply_with_conflict_escalation(
                    orch, task, worktree, worktree_mgr
                )
                if not applied:
                    return await orch.plan_manager.get_task(task.id) or task

            # Step 8: complete.
            task = await orch.plan_manager.update_task_status(
                task.id,
                "complete",
                meta={"evidence_bundle": f".autodev/evidence/{task.id}-coder.json"},
            )
            # v0.15.0: zero the stuck-state counters on success so the
            # ladder accounting tracks the *current* episode of stuck-ness,
            # not historical retries that successfully recovered.
            try:
                await orch.plan_manager.reset_stuck_state(task.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.reset_stuck_state_failed",
                    task_id=task.id,
                    err=str(exc),
                )
            # v0.35.0 C1 prerequisite: credit every lesson that landed
            # in a prompt for this task with one succeeded_after_count
            # increment. Drain the per-task slice of the correlation
            # map so re-entry doesn't double-credit.
            try:
                await _credit_injected_lessons_for_task(orch, task.id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.credit_lessons_failed",
                    task_id=task.id,
                    err=str(exc),
                )
            logger.info("execute_phase.task_complete", task_id=task.id)
            return task
    finally:
        orch.guardrails.end_task(task.id)
        # v0.11.0: always clean up the per-task worktree, even on
        # exception. ``remove_per_task`` swallows missing-worktree races.
        if worktree_mgr is not None and worktree is not None:
            try:
                await worktree_mgr.remove_per_task(task.id, force=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.worktree_remove_failed",
                    task_id=task.id,
                    err=str(exc),
                )


async def _maybe_expand_sparse_for_missing(
    orch: "Orchestrator",
    task: Task,
    worktree: Path,
    worktree_mgr: WorktreeManager,
    adapter_text: str,
) -> bool:
    """v0.20.0 C3: parse missing-file errors and dynamically widen sparse-checkout.

    Returns True iff at least one path was admitted via
    :meth:`WorktreeManager.expand_sparse_paths`. Returns False when:

    * no missing-file paths are detected in ``adapter_text``;
    * none of the detected paths fall under the task's
      ``extended_scope`` / phase ``edit_scope`` / plan ``edit_scope`` —
      we never widen beyond the architect-declared envelope.
    """
    from orchestrator.dag import is_in_scope
    from orchestrator.worktree import detect_missing_paths

    missing = detect_missing_paths(adapter_text)
    if not missing:
        return False
    # Resolve the broadest legitimate scope: extended_scope ∪ phase
    # edit_scope ∪ plan edit_scope. If a missing path is covered by
    # any of these, admit it.
    scope: list[str] = list(task.extended_scope or [])
    if orch.plan_manager is not None:
        try:
            plan = await orch.plan_manager.load()
        except Exception:  # noqa: BLE001
            plan = None
        if plan is not None:
            scope.extend(plan.edit_scope or [])
            for ph in plan.phases:
                if ph.id == task.phase_id and ph.edit_scope is not None:
                    scope.extend(ph.edit_scope)
                    break
    if not scope:
        # No scope to honor → skip dynamic expansion (safety: never
        # blindly admit arbitrary missing paths).
        return False
    to_admit: list[str] = []
    for path in missing:
        if is_in_scope(path, scope):
            # Admit the parent directory (sparse-checkout takes prefixes;
            # adding the file directly works under cone-mode but adding
            # the parent is more idempotent / future-friendly).
            parent = "/".join(path.split("/")[:-1])
            if parent and parent not in to_admit:
                to_admit.append(parent)
            elif not parent and path not in to_admit:
                to_admit.append(path)
    if not to_admit:
        return False
    try:
        await worktree_mgr.expand_sparse_paths(worktree, to_admit)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.dynamic_scope_expand_failed",
            task_id=task.id,
            err=str(exc),
        )
        return False
    logger.info(
        "execute_phase.dynamic_scope_expanded",
        task_id=task.id,
        admitted=to_admit,
    )
    if orch.plan_manager is not None:
        try:
            await orch.plan_manager.ledger_append(
                op="sparse_checkout_expanded",
                payload={
                    "task_id": task.id,
                    "missing_paths": missing,
                    "admitted_prefixes": to_admit,
                },
            )
        except Exception:  # noqa: BLE001
            pass
    return True


async def _enforce_retry_backoff(
    last_retry_at: str | None,
    min_interval_s: float,
    *,
    now: Callable[[], "datetime"] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> float:
    """v0.25.1 Bug #4: pause until the retry interval has elapsed.

    Returns the number of seconds actually slept (``0.0`` when no wait
    was required). ``now`` and ``sleep`` are injectable so tests can
    drive the helper deterministically.

    The guard is a no-op when:

    * ``last_retry_at`` is ``None`` (first retry of the task);
    * ``min_interval_s <= 0`` (operator opted out);
    * the elapsed time since ``last_retry_at`` already exceeds the
      interval (legitimate slow retry, e.g. resume after a long pause).

    Otherwise the helper sleeps for ``min_interval_s - elapsed`` so the
    next dispatch lands at the earliest permissible instant. This stops
    the resume-loop pathology where a task wedged at ``retry_count=N``
    burns through ``N+1, N+2, …`` within milliseconds (sub-second
    sequences observed in the unity run).
    """
    import asyncio
    from datetime import datetime, timezone

    if last_retry_at is None or min_interval_s <= 0:
        return 0.0
    try:
        last_dt = datetime.fromisoformat(last_retry_at)
    except (ValueError, TypeError):
        # Malformed timestamp on disk — fail open rather than block
        # forever. Subsequent mark_task_retry will overwrite with a
        # well-formed value.
        return 0.0
    now_fn = now if now is not None else lambda: datetime.now(timezone.utc)
    sleep_fn = sleep if sleep is not None else asyncio.sleep
    elapsed = (now_fn() - last_dt).total_seconds()
    if elapsed >= min_interval_s:
        return 0.0
    wait = min_interval_s - elapsed
    await sleep_fn(wait)
    return wait


async def _try_retry_or_escalate(
    orch: "Orchestrator",
    task: Task,
    retry_limit: int,
    reason: str,
) -> Task:
    """Bump retry count or escalate when the limit is reached.

    Returns the updated task. If ``task.escalated`` becomes True on return,
    the caller should stop the loop.

    v0.15.0: in addition to the legacy retry-then-escalate logic, this
    helper now consults the per-task :class:`StuckState` against
    :func:`orchestrator.escalation_ladder.next_step`. When the ladder
    returns ``"continue"`` (the dominant path in normal runs), behavior
    is identical to v0.14.0 — backward compat. Otherwise the helper
    dispatches to :func:`_escalate_stuck_to_critic` and applies the
    resolution:

    * ``"REFINE"`` → critic suggests a small adjustment; on
      ``RESOLUTION: refine`` the task is restarted in_progress (the
      caller's loop picks up the developer with the refined guidance
      injected via ``last_issues`` on the next iteration). The
      lessons-emit path records a ``course_correction`` event.
    * ``"PIVOT"`` → critic suggests a radical redirect; on
      ``RESOLUTION: pivot`` the pivot counter is bumped and the task
      is restarted with the radical guidance.
    * ``"SOFT_BLOCKER"`` → critic confirms a human-required decision;
      the task is marked ``escalated`` + ``blocked`` with the guidance
      surfaced as ``blocked_reason``. Lessons-emit records a
      ``soft_blocker`` event.

    All branches first bump :meth:`PlanManager.increment_discard` so the
    ladder accounting reflects the freshest signal.
    """
    from orchestrator.escalation_ladder import next_step
    from orchestrator.knowledge_lookup import lookup_recent_failures
    from orchestrator.repetition_recovery import choose_recovery_action
    from state.knowledge import TournamentEvent

    # v0.25.1 Bug #4: enforce the configured minimum retry interval
    # BEFORE any state mutation. Stops the resume-loop pathology where
    # a task wedged at retry_count=N burns through N+1, N+2, ... within
    # milliseconds. Idempotent — re-entering with a fresh ``last_retry_at``
    # naturally short-circuits via the elapsed-time check.
    min_interval = getattr(orch.cfg, "qa_retry_min_interval_s", 30.0)
    waited = await _enforce_retry_backoff(task.last_retry_at, min_interval)
    if waited > 0:
        logger.info(
            "execute_phase.retry_backoff_enforced",
            task_id=task.id,
            waited_s=waited,
            min_interval_s=min_interval,
            last_retry_at=task.last_retry_at,
        )

    # v0.15.0: bump the stuck-state discard counter under lock.
    stuck_state = await orch.plan_manager.increment_discard(task.id)

    # v0.32.0 Phase 4.1: query PRM for detected patterns and pass them
    # into the ladder so ``repetition_loop`` / ``ping_pong`` can gate
    # the REFINE→PIVOT (and REFINE/PIVOT→ARCHITECT_CONSULT) overrides.
    # Defensive: any failure reading the trajectory store falls through
    # to the legacy ladder behaviour (knowledge_context=None).
    detected_patterns: list[str] = []
    target_files_observed: list[str] = []
    trajectory_store = getattr(orch, "trajectory_store", None)
    if trajectory_store is not None:
        try:
            patterns = trajectory_store.analyze(task.id)
            detected_patterns = [p.name for p in patterns]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.prm_analyze_failed_in_retry",
                task_id=task.id,
                err=str(exc),
            )
        try:
            recent_events = trajectory_store.events_for(task.id)[-3:]
            seen: set[str] = set()
            for ev in recent_events:
                for f in getattr(ev, "target_files", ()):
                    if f not in seen:
                        seen.add(f)
                        target_files_observed.append(f)
        except Exception:  # noqa: BLE001
            target_files_observed = []

    knowledge_context: dict[str, Any] | None = None
    if detected_patterns:
        knowledge_context = {"detected_patterns": detected_patterns}

    step = next_step(stuck_state, knowledge_context=knowledge_context)

    # v0.32.0 Phase 4.5: emit a forensic breadcrumb when the PRM
    # observed a repetition_loop (regardless of whether the ladder ended
    # up overriding — the breadcrumb captures the *signal* so post-
    # mortems can correlate "we knew we were looping" with "what we
    # did about it").
    repetition_loop_detected = "repetition_loop" in detected_patterns
    if repetition_loop_detected:
        try:
            await orch.plan_manager.ledger_append(
                op="repetition_loop_detected",
                payload={
                    "task_id": task.id,
                    "discard_count": stuck_state.discard_count,
                    "pivot_count": stuck_state.pivot_count,
                    "target_files": target_files_observed,
                    "detected_at_attempt": int(getattr(task, "retry_count", 0)),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.ledger_append_failed",
                op="repetition_loop_detected",
                err=str(exc),
            )

    # v0.32.0 Phase 4.4: pick a recovery action so the ledger captures
    # *why* the retry path is going to dispatch what it dispatches.
    # The action does not currently override the ladder's choice (the
    # ladder is the source of truth for the next dispatch rung); it is
    # an audit-only annotation that lets forensics reconstruct the
    # policy decision. Future work can route ``increase_scope`` /
    # ``re_architect`` / ``do_nothing`` to dedicated paths once the
    # corresponding dispatch sites mature.
    qa_gates_passed = bool(getattr(task, "qa_gates_passed", False))
    chosen_action = choose_recovery_action(
        discard_count=stuck_state.discard_count,
        pivot_count=stuck_state.pivot_count,
        architect_count=stuck_state.architect_count,
        qa_gates_passed=qa_gates_passed,
        repetition_loop_detected=repetition_loop_detected,
    )
    try:
        await orch.plan_manager.ledger_append(
            op="recovery_action_chosen",
            payload={
                "task_id": task.id,
                "action": chosen_action,
                "reason": reason[:200],
                "next_step": step,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.ledger_append_failed",
            op="recovery_action_chosen",
            err=str(exc),
        )

    if step != "continue":
        # Ladder dispatch path. Build a minimal prior_attempts list from
        # the legacy retry_count for forensics.
        prior_attempts = [f"retry_count={task.retry_count}, reason={reason}"]

        # v0.32.0 Phase 4.2: KB lookup before refine — query the
        # knowledge store for the most recent discard / soft_blocker
        # entries on this task signature and append them to the reason
        # string so the critic prompt sees "we already tried X and Y".
        # The lookup is bounded by a 100ms timeout so a slow store
        # cannot stall the retry loop.
        try:
            past_failures = await lookup_recent_failures(orch, task.id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.knowledge_lookup_failed",
                task_id=task.id,
                err=str(exc),
            )
            past_failures = []
        if past_failures:
            kb_block_lines = [
                "PAST_FAILURE_CONTEXT (recent discards/soft-blocks on this task):",
            ]
            for summary in past_failures:
                kb_block_lines.append(f"  - {summary}")
            kb_block = "\n".join(kb_block_lines)
            prior_attempts.append(kb_block)
            # Splice into the failure reason so the existing
            # ``recent_evidence`` flow path picks it up unchanged.
            reason = f"{reason}\n\n{kb_block}"
            # Forensic breadcrumb: we switched the tactic by enriching
            # the critic context with KB lookups.
            try:
                await orch.plan_manager.ledger_append(
                    op="tactic_switch",
                    payload={
                        "task_id": task.id,
                        "prior_tactic": "refine_minimal",
                        "new_tactic": "refine_with_kb",
                        "guidance_source": "kb_lookup",
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.ledger_append_failed",
                    op="tactic_switch",
                    err=str(exc),
                )

        # v0.18.0: WEB_SEARCH ladder step — when enabled by config, fetch
        # top-3 search results and splice them as a ``WEB_CONTEXT:`` block
        # into the next critic prompt. After the search dispatches, fall
        # through to the critic invocation with the spliced context. The
        # search adapter, web_search_invoked ledger op, and search_count
        # accounting all already shipped in v0.17.0; this is the deferred
        # wiring at the executor's dispatch site.
        web_context_block: str = ""
        if step == "WEB_SEARCH" and getattr(
            orch.cfg, "web_search_enabled", False
        ):
            try:
                from adapters.web_search import web_search

                # Build a minimal query from the failure reason + task.
                query = f"{task.title}: {reason}"[:200]
                results = await web_search(query, max_results=3)
                # Format results into the WEB_CONTEXT: block.
                if results:
                    rows = []
                    for r in results:
                        rows.append(f"  - {r.title}: {r.url}")
                        if r.snippet:
                            rows.append(f"    {r.snippet[:200]}")
                    web_context_block = (
                        "WEB_CONTEXT:\n" + "\n".join(rows) + "\n"
                    )
                # Bump search_count under lock + ledger op.
                await orch.plan_manager.increment_search(task.id)
                try:
                    await orch.plan_manager.ledger_append(
                        op="web_search_invoked",
                        payload={
                            "task_id": task.id,
                            "query": query,
                            "results_count": len(results),
                            "search_count_after": stuck_state.search_count + 1,
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.ledger_append_failed",
                        op="web_search_invoked",
                        err=str(exc),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.web_search_failed",
                    task_id=task.id,
                    err=str(exc),
                )

        # v0.26.1 patch G: ARCHITECT_CONSULT rung — re-delegate to
        # architect_b in consult mode. Branches before the regular
        # critic dispatch because the response format + action set is
        # distinct (refine-tasks / infrastructure / continue).
        if step == "ARCHITECT_CONSULT":
            arch_resolution = await _dispatch_architect_consult(
                orch,
                task,
                stuck_state=stuck_state,
                reason=reason,
                prior_attempts=prior_attempts,
                web_context_block=web_context_block,
            )
            if arch_resolution is not None:
                return arch_resolution

        try:
            # v0.37.0 H1: same evidence-body threading as architect-consult.
            resolution = await _escalate_stuck_to_critic(
                orch,
                task,
                stuck_state=stuck_state,
                ladder_step=step,
                recent_evidence=await _build_recent_evidence_block(
                    orch, task, reason, web_context_block
                ),
                prior_attempts=prior_attempts,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.stuck_escalation_failed",
                task_id=task.id,
                step=step,
                err=str(exc),
            )
            # Fall through to legacy path on any escalation failure.
            resolution = None

        if resolution is not None:
            # Branch on the critic's chosen action.
            if resolution.action == "soft-blocker":
                # v0.42.1 F1: route the SOFT_BLOCKER escalation rung through the
                # single chokepoint. A resolver recovery (consult_knowledge /
                # retry_with_changes) re-enables the task; the per-blocker cycle
                # budget caps repeated soft-blocker recoveries and then falls
                # through to the legacy block (``block_task`` commits it).
                await orch.plan_manager.mark_escalated(task.id)
                guidance_text = resolution.guidance or "human decision required"
                # v0.32.0 (Phase 5, Gap G): structured recovery hint so
                # ``autodev status --blocked`` renders the critic's
                # guidance + relevant evidence paths inline.
                soft_block_hint = _build_recovery_hint(
                    task_id=task.id,
                    hint_class="user_decision_required",
                    action=(
                        f"Multiple refinement cycles produced no "
                        f"improvement (discard_count="
                        f"{stuck_state.discard_count}, pivot_count="
                        f"{stuck_state.pivot_count}). Manual review "
                        f"needed: {guidance_text[:200]}"
                    ),
                    commands=[
                        f"autodev requeue --task {task.id}",
                    ],
                )
                updated = await block_task(
                    orch,
                    task,
                    failure_class=_fcls.SOFT_BLOCKER,
                    raw_error=(resolution.guidance or "soft-blocker"),
                    evidence_refs=list(prior_attempts or []),
                    meta={
                        "blocked_reason": (
                            f"soft-blocker: {guidance_text}"
                        ),
                        "recovery_hint": soft_block_hint,
                    },
                )
                if updated.status != "blocked":
                    # Resolver actively recovered the task — return the
                    # re-enabled task and SKIP the soft-blocker handoff ops.
                    return updated
                # Audit-only ledger op + cross-run lesson.
                try:
                    await orch.plan_manager.ledger_append(
                        op="soft_blocker_handoff",
                        payload={
                            "task_id": task.id,
                            "reason": reason,
                            "critic_response_excerpt": guidance_text[:500],
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.ledger_append_failed",
                        op="soft_blocker_handoff",
                        err=str(exc),
                    )
                try:
                    await orch.knowledge.record_tournament_event(
                        TournamentEvent(
                            event_type="soft_blocker",
                            family="execute-phase",
                            hypothesis=(
                                f"task {task.id} required human decision "
                                f"after {stuck_state.discard_count} discards "
                                f"and {stuck_state.pivot_count} pivots"
                            ),
                            evidence=guidance_text[:500],
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.knowledge_record_failed",
                        event="soft_blocker",
                        err=str(exc),
                    )
                return updated

            if resolution.action == "pivot":
                # Bump pivot counter under lock + audit ledger op.
                await orch.plan_manager.increment_pivot(task.id)
                try:
                    await orch.plan_manager.ledger_append(
                        op="stuck_pivot",
                        payload={
                            "task_id": task.id,
                            "reason": reason,
                            "critic_response_excerpt": resolution.guidance[:500],
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.ledger_append_failed",
                        op="stuck_pivot",
                        err=str(exc),
                    )
                try:
                    await orch.knowledge.record_tournament_event(
                        TournamentEvent(
                            event_type="course_correction",
                            family="execute-phase",
                            hypothesis=(
                                f"task {task.id} pivoted at "
                                f"discard_count={stuck_state.discard_count}"
                            ),
                            evidence=resolution.guidance[:500],
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.knowledge_record_failed",
                        event="pivot",
                        err=str(exc),
                    )
                # Restart the task so the executor's outer loop re-picks
                # it with the pivot guidance injected via ``last_issues``.
                if task.status != "in_progress":
                    task = await orch.plan_manager.update_task_status(
                        task.id, "in_progress"
                    )
                return await orch.plan_manager.get_task(task.id) or task

            # ``refine`` (also the defensive default).
            try:
                await orch.plan_manager.ledger_append(
                    op="stuck_refine",
                    payload={
                        "task_id": task.id,
                        "reason": reason,
                        "critic_response_excerpt": resolution.guidance[:500],
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.ledger_append_failed",
                    op="stuck_refine",
                    err=str(exc),
                )
            try:
                await orch.knowledge.record_tournament_event(
                    TournamentEvent(
                        event_type="course_correction",
                        family="execute-phase",
                        hypothesis=(
                            f"task {task.id} refined at "
                            f"discard_count={stuck_state.discard_count}"
                        ),
                        evidence=resolution.guidance[:500],
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.knowledge_record_failed",
                    event="refine",
                    err=str(exc),
                )
            if task.status != "in_progress":
                task = await orch.plan_manager.update_task_status(
                    task.id, "in_progress"
                )
            return await orch.plan_manager.get_task(task.id) or task

    # Legacy ("continue") path: behavior identical to v0.14.0.
    new_count = await orch.plan_manager.mark_task_retry(task.id)
    if new_count >= retry_limit:
        logger.warning(
            "execute_phase.retry_exhausted",
            task_id=task.id,
            retry=new_count,
            reason=reason,
        )
        sb_env = DelegationEnvelope(
            task_id=task.id,
            target_agent="critic_sounding_board",
            action="critique",
            acceptance="Diagnose why this task keeps failing and suggest next steps.",
            context={
                "task_id": task.id,
                "reason": reason,
                "retry_count": new_count,
            },
        )
        sb_result = await delegate(orch, "critic_sounding_board", sb_env)
        await write_evidence(
            orch.cwd,
            task.id,
            CriticEvidence(
                task_id=task.id,
                verdict="NEEDS_REVISION",
                issues=[reason],
                output_text=sb_result.text,
            ),
        )
        # Cross-run lesson: legacy retry-exhaustion escalation.
        try:
            await orch.knowledge.record_tournament_event(
                TournamentEvent(
                    event_type="escalation",
                    family="execute-phase",
                    hypothesis=(
                        f"task {task.id} exceeded retry_limit={retry_limit}"
                    ),
                    evidence=f"reason={reason} retry_count={new_count}",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.knowledge_record_failed",
                event="escalation",
                err=str(exc),
            )
        await orch.plan_manager.mark_escalated(task.id)
        # v0.32.0 (Phase 5, Gap G): structured recovery hint inferred
        # from the ``reason`` text. The classifier picks among
        # thin_review_evidence / missing_test_output / network_transient
        # / user_decision_required so the CLI surface can render a
        # purposeful action message.
        retry_exhausted_hint = _build_recovery_hint_from_reason(
            task_id=task.id, reason=reason
        )
        # v0.42.1 F1: route the retry-exhaustion escalation through the single
        # chokepoint (UNKNOWN so the LLM resolver gets a shot at recovery); a
        # resolver recovery re-enables the task, otherwise ``block_task`` commits
        # the legacy block unchanged.
        updated = await block_task(
            orch,
            task,
            failure_class=_fcls.UNKNOWN,
            raw_error=f"escalated: {reason}",
            meta={
                "blocked_reason": f"escalated: {reason}",
                "recovery_hint": retry_exhausted_hint,
            },
        )
        return updated

    # Retry: transition blocked/etc -> in_progress as appropriate.
    if task.status != "in_progress":
        task = await orch.plan_manager.update_task_status(task.id, "in_progress")
    fresh = await orch.plan_manager.get_task(task.id)
    return fresh or task


async def delegate(
    orch: "Orchestrator",
    role: str,
    envelope: DelegationEnvelope,
    extra_context: str = "",
    retry_count: int = 0,
    last_issues: list[str] | None = None,
    task: Task | None = None,
    cwd_override: Path | None = None,
) -> AgentResult:
    """Build an :class:`AgentInvocation` from the envelope and call the adapter.

    Guardrail hooks are called around the adapter execution:
    - ``pre_invocation`` before the adapter call (may raise GuardrailExceededError)
    - ``post_invocation`` after the adapter call (may raise GuardrailExceededError)
    - ``loop_detector.observe`` after post_invocation

    v0.26.0: InlineAdapter's suspend/resume special-cases (response-file
    shortcut on the resume path, ``write_suspend_state`` on the
    ``DelegationPendingSignal`` exit path) were removed. Every adapter
    is now a subprocess adapter and the dispatch is a straight call.

    v0.8.0: when ``task`` is provided, the per-task complexity resolvers
    override ``max_turns`` and ``timeout_s`` on the constructed invocation.
    The developer call passes ``task`` so its budget scales with the
    architect-tagged complexity bucket; reviewer/test_engineer/critic_t
    calls leave it unset and inherit the spec defaults (which are tuned
    for shorter, single-purpose passes).
    """
    spec = orch.registry.get(role)
    if spec is None:
        raise AutodevError(f"role {role!r} not in registry")
    parts: list[str] = [spec.prompt.strip()]
    parts.append("\n\n---\n")
    parts.append(envelope.render_as_task_message())
    if extra_context:
        parts.append("\n\n")
        parts.append(extra_context)
    # v0.35.0 C1 prerequisite: prefer the IDs-returning variant so the
    # success path can credit the entries that contributed. Fall back
    # to the legacy str-returning ``inject_block`` for test fakes that
    # haven't been updated to expose the sister method.
    inject_with_ids = getattr(orch.knowledge, "inject_block_with_ids", None)
    if inject_with_ids is not None:
        lessons, injected_ids = await inject_with_ids(
            role, task_id=envelope.task_id
        )
    else:
        lessons = await orch.knowledge.inject_block(role, task_id=envelope.task_id)
        injected_ids = []
    if lessons:
        parts.append("\n\n")
        parts.append(lessons)
    # v0.35.0 C1 prerequisite: stash the (task_id, role) -> ids
    # correlation so the success path can credit succeeded_after_count
    # on the entries that actually contributed to this prompt. The map
    # accumulates across roles for the same task so one task that
    # injects in both coder and reviewer prompts credits both. Cleared
    # by the success-recording helper after the task transitions to
    # ``complete``. Defensive ``getattr`` so test fakes for Orchestrator
    # don't have to carry the slot.
    if injected_ids and envelope.task_id:
        correlation = getattr(orch, "_injected_lessons_by_task", None)
        if correlation is not None:
            key = (envelope.task_id, role)
            existing = correlation.get(key, [])
            correlation[key] = existing + [
                i for i in injected_ids if i not in existing
            ]

    # v0.15.0: PRM trajectory pattern detection. Before this dispatch,
    # consult the trajectory store for any patterns observed since the
    # last call. If a CourseCorrection is pending for the task AND has
    # not been emitted yet, splice the markdown block into the prompt
    # and mark it emitted (cap one correction per fingerprint per task).
    trajectory_store = getattr(orch, "trajectory_store", None)
    if trajectory_store is not None and envelope.task_id:
        try:
            patterns = trajectory_store.analyze(envelope.task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.prm_analyze_failed",
                task_id=envelope.task_id,
                err=str(exc),
            )
            patterns = []

        # v0.20.0 A1: optional LLM-augmentation. When ``cfg.prm.strategy``
        # is ``"rules+ml"`` AND the orchestrator carries an
        # ``llm_trajectory_classifier`` attribute, run the classifier and
        # merge its patterns with the rule-based ones. Rules win on dedup;
        # graceful degradation on any error (already implemented inside
        # :class:`LLMTrajectoryClassifier`).
        prm_strategy = getattr(
            getattr(orch.cfg, "prm", None), "strategy", "rules"
        )
        ml_clf = getattr(orch, "llm_trajectory_classifier", None)
        if prm_strategy == "rules+ml" and ml_clf is not None:
            try:
                events_for_clf = trajectory_store.events_for(envelope.task_id)
                ml_patterns = await ml_clf.classify(events_for_clf)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.prm_ml_classify_failed",
                    task_id=envelope.task_id,
                    err=str(exc),
                )
                ml_patterns = []
            if ml_patterns:
                from orchestrator.prm import merge_patterns

                patterns = merge_patterns(patterns, ml_patterns)
        if patterns:
            from orchestrator.prm import CourseCorrection

            # Highest-severity pattern first (analyze() pre-sorts).
            top = patterns[0]
            cc = CourseCorrection.from_pattern(top)
            fingerprint = cc.fingerprint()
            if not trajectory_store.has_emitted(envelope.task_id, fingerprint):
                parts.append("\n\n")
                parts.append(cc.format_for_prompt())
                trajectory_store.mark_emitted(envelope.task_id, fingerprint)
                # Audit-only ledger op + cross-run lesson.
                if hasattr(orch, "plan_manager") and orch.plan_manager is not None:
                    try:
                        await orch.plan_manager.ledger_append(
                            op="course_correction_emitted",
                            payload={
                                "task_id": envelope.task_id,
                                "taxonomy": cc.taxonomy,
                                "pattern": cc.pattern,
                                "suggestion": cc.suggestion[:500],
                            },
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "execute_phase.ledger_append_failed",
                            op="course_correction_emitted",
                            err=str(exc),
                        )
                try:
                    from state.knowledge import TournamentEvent

                    await orch.knowledge.record_tournament_event(
                        TournamentEvent(
                            event_type="course_correction",
                            family="prm",
                            hypothesis=(
                                f"PRM detected {cc.pattern} on task "
                                f"{envelope.task_id}"
                            ),
                            evidence=cc.suggestion[:500],
                            next_action_hint=(
                                f"taxonomy={cc.taxonomy}; review trajectory "
                                "before next dispatch"
                            ),
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.knowledge_record_failed",
                        event="course_correction",
                        err=str(exc),
                    )

    # v0.14.0: inject EDIT SCOPE addendum for the developer role when the
    # plan/phase declares a non-empty scope. Resolution mirrors
    # :func:`orchestrator.dag.validate_edit_scope`: phase-level override
    # (when non-None) wins over plan-level. The developer sees the
    # narrowest applicable boundary as a single line in their system
    # prompt addendum so they can self-police edits before writing.
    if role == "developer" and orch.plan_manager is not None and task is not None:
        try:
            existing_plan_for_scope = await orch.plan_manager.load()
        except Exception:  # noqa: BLE001
            existing_plan_for_scope = None
        if existing_plan_for_scope is not None:
            resolved_scope: list[str] = []
            for ph in existing_plan_for_scope.phases:
                if ph.id == task.phase_id:
                    if ph.edit_scope is not None:
                        resolved_scope = list(ph.edit_scope)
                    else:
                        resolved_scope = list(existing_plan_for_scope.edit_scope)
                    break
            else:
                # task.phase_id not matched (defensive — shouldn't happen
                # in production); fall back to plan-level scope.
                resolved_scope = list(existing_plan_for_scope.edit_scope)
            if resolved_scope:
                parts.append("\n\n")
                parts.append(f"EDIT SCOPE: {', '.join(resolved_scope)}")

    # Resolve per-role effort using the parsed plan complexity. Once the
    # execute phase begins, the architect has run and ``Plan.complexity`` is
    # set (or None on legacy/pre-upgrade plans, which gracefully falls back
    # to the user-global default).
    agent_cfg = orch.cfg.agents.get(role)
    plan_complexity: str | None = None
    if orch.plan_manager is not None:
        try:
            existing_plan = await orch.plan_manager.load()
        except Exception:  # noqa: BLE001
            existing_plan = None
        if existing_plan is not None:
            plan_complexity = existing_plan.complexity
    effort = resolve_role_effort(
        role, agent_cfg, plan_complexity, orch.cfg.user_complexity
    )

    # v0.8.0: per-task complexity overrides for ``max_turns`` and
    # ``timeout_s``. Resolvers return ``None`` when no override applies (or
    # when ``task`` is unset for non-developer roles), and the spec / module
    # constant defaults take over.
    spec_max_turns = spec.max_turns or 1
    if task is not None:
        # v0.13.0: thread the orchestrator's repo-size snapshot through so
        # tasks on Unity-class repos get the doubled per-complexity budget.
        # ``_repo_capacity`` may be None on the orchestrator stub used in
        # some unit tests (back-compat). Fall back to legacy behavior in
        # that case.
        repo_capacity = getattr(orch, "_repo_capacity", None)
        # v0.20.0 D1: thread per-bucket huge-repo multiplier overrides
        # through to the resolver. Default ``None`` → resolver uses
        # baked-in per-bucket curves (simple 3.0×, medium 2.0×, complex 1.5×).
        # ``getattr`` on the cfg attribute is defensive — orchestrator stubs
        # in some unit tests omit ``task_overrides`` entirely.
        _task_overrides_cfg = getattr(orch.cfg, "task_overrides", None)
        huge_mult_overrides = (
            getattr(_task_overrides_cfg, "huge_repo_multipliers", None)
            if _task_overrides_cfg is not None
            else None
        )
        # v0.36.0 E2: thread retry attempt + multiplier/cap into the
        # resolver so retry attempt ≥ 2 actually gets more runway.
        retry_mult = (
            getattr(_task_overrides_cfg, "retry_budget_multiplier", 2.0)
            if _task_overrides_cfg is not None
            else 2.0
        )
        retry_cap = (
            getattr(_task_overrides_cfg, "retry_budget_cap_turns", 200)
            if _task_overrides_cfg is not None
            else 200
        )
        _base_max = resolve_task_max_turns(
            task,
            spec.max_turns,
            capacity=repo_capacity,
            huge_repo_multipliers=huge_mult_overrides,
            retry_attempt=0,
        )
        max_turns = (
            resolve_task_max_turns(
                task,
                spec.max_turns,
                capacity=repo_capacity,
                huge_repo_multipliers=huge_mult_overrides,
                retry_attempt=retry_count,
                retry_budget_multiplier=retry_mult,
                retry_budget_cap_turns=retry_cap,
            )
            or spec.max_turns
            or 1
        )
        # v0.36.0 E2 telemetry: emit retry_budget_scaled when retry
        # actually changed the budget. Best-effort — never block the
        # dispatch.
        if (
            retry_count >= 2
            and _base_max is not None
            and max_turns != _base_max
        ):
            try:
                await orch.plan_manager.ledger_append(
                    op="retry_budget_scaled",
                    payload={
                        "task_id": envelope.task_id,
                        "attempt": retry_count,
                        "base": int(_base_max),
                        "effective": int(max_turns),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.retry_budget_ledger_failed", err=str(exc)
                )
        # v0.36.0 E1 telemetry: if a role-keyed multiplier > 1 applies
        # AND the repo is huge, emit huge_repo_multiplier_applied.
        # Reads role-keyed entries from the same cfg dict the
        # complexity-keyed resolver consults — the dict carries both
        # key shapes for forward-compat with future role-aware
        # resolution.
        if (
            repo_capacity is not None
            and getattr(repo_capacity, "is_huge", False)
            and isinstance(huge_mult_overrides, dict)
            and role in huge_mult_overrides
            and huge_mult_overrides[role] > 1.0
        ):
            try:
                _role_mult = float(huge_mult_overrides[role])
                _role_base = _base_max if _base_max is not None else spec_max_turns
                _role_effective = int(round(_role_base * _role_mult))
                await orch.plan_manager.ledger_append(
                    op="huge_repo_multiplier_applied",
                    payload={
                        "role": role,
                        "base": int(_role_base),
                        "multiplier": _role_mult,
                        "effective": _role_effective,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.huge_repo_ledger_failed", err=str(exc)
                )
        timeout_s = resolve_task_timeout_s(task, _DEFAULT_DEVELOPER_TIMEOUT_S)
        if timeout_s is None:
            timeout_s = _DEFAULT_DEVELOPER_TIMEOUT_S
    else:
        # v0.39.0 (Cluster C1): non-task roles (reviewer, test_engineer,
        # domain_expert, critics, …) previously bypassed huge-repo scaling
        # entirely — ``max_turns`` was pinned to ``spec_max_turns`` no matter
        # how large the repo was, which is why the reviewer needed a manual
        # ``reviewer.max_turns=12`` override to survive Unity-class runs.
        # We now scale these roles by the same role-keyed
        # ``huge_repo_multipliers`` dict the task branch consults, gated on
        # the identical ``_repo_capacity.is_huge`` signal (NOT
        # ``is_huge_repo(cwd)``) so both arms of this ``delegate`` agree.
        # No-op when capacity is None / not huge / role absent / mult ≤ 1.0
        # / cfg lacks ``task_overrides``. Idempotent — always recomputes
        # from ``spec_max_turns``.
        repo_capacity = getattr(orch, "_repo_capacity", None)
        _task_overrides_cfg = getattr(orch.cfg, "task_overrides", None)
        huge_mult_overrides = (
            getattr(_task_overrides_cfg, "huge_repo_multipliers", None)
            if _task_overrides_cfg is not None
            else None
        )
        max_turns = spec_max_turns
        if (
            repo_capacity is not None
            and getattr(repo_capacity, "is_huge", False)
            and isinstance(huge_mult_overrides, dict)
            and role in huge_mult_overrides
            and huge_mult_overrides[role] > 1.0
        ):
            _role_mult = float(huge_mult_overrides[role])
            # Round half *up* (not banker's ``round``): the documented C1
            # outcomes require reviewer 5×2.5→13 and domain_expert 3×1.5→5,
            # i.e. .5 cases round toward more runway. ``int(x + 0.5)`` gives
            # 13 / 8 / 5 where ``int(round(x))`` would give 12 / 8 / 4.
            max_turns = int(spec_max_turns * _role_mult + 0.5)
            # v0.36.0 E1 telemetry (reused): record the role-keyed scaling
            # for forensics. Best-effort, ``plan_manager``-guarded.
            if getattr(orch, "plan_manager", None) is not None:
                try:
                    await orch.plan_manager.ledger_append(
                        op="huge_repo_multiplier_applied",
                        payload={
                            "role": role,
                            "base": int(spec_max_turns),
                            "multiplier": _role_mult,
                            "effective": int(max_turns),
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "execute_phase.huge_repo_nontask_ledger_failed",
                        err=str(exc),
                    )
        timeout_s = None  # adapter applies its own default (600s in claude_code)

    # v0.31.0 (Phase 3): per-(task_id, role) budget escalation on
    # consecutive ``error_max_turns``. The tracker is stateful (owned
    # by the orchestrator); we read its current count to decide what
    # multiplier to apply, then update it after the adapter returns.
    # Hard-fail when the ladder is exhausted (4th consecutive max-turns
    # would otherwise burn another retry slot with no extra budget).
    # The ``getattr`` guard keeps backward compat with orchestrator
    # stubs in older tests that predate the tracker.
    _budget_tracker = getattr(orch, "_budget_escalation_tracker", None)
    _escalation_attempt = 0
    _prior_max_turns = max_turns
    _prior_timeout_s = timeout_s
    if _budget_tracker is not None and envelope.task_id:
        from orchestrator.budget_escalation import (
            BudgetEscalationTracker,
            escalate_budget,
        )

        # Read tunable ceilings off the cfg if present (operator
        # override surface), else fall back to the module defaults.
        from orchestrator.budget_escalation import (
            DEFAULT_MAX_TURNS_CEILING,
        )
        from orchestrator.huge_repo_overrides import resolve_huge_repo_value

        _ceiling_cfg = getattr(orch.cfg, "budget_escalation", None)
        _max_turns_ceiling = (
            getattr(_ceiling_cfg, "max_turns_ceiling", None)
            if _ceiling_cfg is not None
            else None
        )
        _timeout_s_ceiling = (
            getattr(_ceiling_cfg, "timeout_s_ceiling", None)
            if _ceiling_cfg is not None
            else None
        )
        # v0.39.0 (Cluster A3): lift the turns ceiling on huge repos
        # (1.5× → 375 from the 250 default) so a long task can climb the
        # escalation ladder without prematurely hard-failing. The resolver
        # is identity on small repos / escape hatch, so small-repo
        # behaviour is byte-identical. ``timeout_s_ceiling`` stays un-scaled.
        _base_ceiling = (
            _max_turns_ceiling
            if _max_turns_ceiling is not None
            else DEFAULT_MAX_TURNS_CEILING
        )
        _ceiling_eff, _ = resolve_huge_repo_value(
            key="max_turns_ceiling",
            base_value=float(_base_ceiling),
            cwd=orch.cwd,
            cfg=orch.cfg,
        )
        _kwargs: dict[str, int] = {}
        _kwargs["max_turns_ceiling"] = int(round(_ceiling_eff))
        if _timeout_s_ceiling is not None:
            _kwargs["timeout_s_ceiling"] = int(_timeout_s_ceiling)

        if _budget_tracker.is_exhausted(envelope.task_id, role):
            # 4th consecutive ``error_max_turns`` would just burn
            # another retry slot with no extra budget. Hard-fail
            # before dispatching so the caller surfaces a typed
            # diagnostic instead of consuming a retry quota.
            return AgentResult(
                success=False,
                text="",
                duration_s=0.0,
                error=_budget_tracker.exhaustion_diagnostic,
                subtype="error_max_turns_escalation_exhausted",
            )

        _escalation_attempt = _budget_tracker.current_attempt(
            envelope.task_id, role
        )
        if _escalation_attempt > 0:
            _new_max_turns, _new_timeout_s = escalate_budget(
                _prior_max_turns,
                _prior_timeout_s,
                _escalation_attempt,
                **_kwargs,
            )
            # Emit the budget-escalation breadcrumb BEFORE the bumped
            # dispatch so post-mortems can correlate the escalation
            # with the subsequent adapter call. Best-effort — a ledger
            # failure here MUST NOT mask the dispatch. Mirrors the
            # ``adapter_failure`` ledger pattern below.
            _payload = {
                "task_id": envelope.task_id,
                "role": role,
                "prior_max_turns": _prior_max_turns,
                "new_max_turns": _new_max_turns,
                "prior_timeout_s": _prior_timeout_s,
                "new_timeout_s": _new_timeout_s,
                "attempt": _escalation_attempt,
            }
            if getattr(orch, "plan_manager", None) is not None:
                try:
                    await orch.plan_manager.ledger_append(
                        op="budget_escalation",
                        payload=_payload,
                    )
                except Exception as exc:  # noqa: BLE001 — best-effort
                    # Ledger op may not be wired in older schemas; log a
                    # structured-log fallback with the same payload so
                    # the breadcrumb is recoverable from log streams.
                    logger.warning(
                        "orchestrator.budget_escalation",
                        err=str(exc),
                        **_payload,
                    )
            else:
                # No plan_manager (some unit-test orchestrator stubs):
                # log directly.
                logger.warning(
                    "orchestrator.budget_escalation",
                    **_payload,
                )
            max_turns = _new_max_turns
            timeout_s = _new_timeout_s
        # Silence a noqa for the unused alias import on the no-op path.
        _ = BudgetEscalationTracker

    # v0.11.0: per-task worktree isolation — when a worker passes a
    # cwd_override (its worktree path), agent execution happens there
    # rather than in orch.cwd (the main repo). All other accounting
    # (evidence, ledger, guardrails) still keys off orch.cwd.
    effective_cwd = cwd_override if cwd_override is not None else orch.cwd

    # v0.31.0 (Phase 1.4): per-role output-token hint. Reviewers are the
    # most-bitten role for the empty-result failure mode (Hypothesis A);
    # ask for a generous floor so the model has headroom to emit a full
    # verdict + issues list. Other roles inherit ``None`` (CLI default)
    # for now — extending the per-role table is cheap when more roles
    # need explicit budgets.
    _output_token_budget: int | None = None
    if role == "reviewer":
        _output_token_budget = 4_096

    inv = AgentInvocation(
        role=role,
        prompt="\n".join(parts),
        cwd=effective_cwd,
        model=spec.model,
        allowed_tools=list(spec.tools) if spec.tools else None,
        max_turns=max_turns,
        effort=effort,
        timeout_s=timeout_s,
        output_token_budget=_output_token_budget,
    )

    orch.guardrails.pre_invocation(envelope.task_id, inv)
    import time as _time

    _t0 = _time.time()
    try:
        result = await orch.adapter.execute(inv)
    except Exception as _dispatch_exc:  # noqa: BLE001 - observability only; re-raised
        # v0.42.1 F1e (ADR-0047): an UNEXPECTED raw-dispatch crash (the adapter
        # raised instead of returning a failure ``AgentResult``) used to vanish
        # up the stack with no escalation breadcrumb — so the Run-6 invariant
        # "every terminal block is preceded by an escalation op" could be
        # violated on a dispatch crash. Emit a structured log + a best-effort
        # ``blocker_escalated``-shaped ledger breadcrumb, then RE-RAISE unchanged
        # (control flow / propagation / guardrail semantics are untouched —
        # pure log + crumb + re-raise).
        logger.warning(
            "delegate.dispatch_failed",
            role=role,
            task_id=envelope.task_id,
            err=str(_dispatch_exc),
            exc_type=type(_dispatch_exc).__name__,
        )
        try:
            if getattr(orch, "plan_manager", None) is not None:
                await orch.plan_manager.ledger_append(
                    op="blocker_escalated",
                    payload={
                        "task_id": envelope.task_id,
                        "phase_id": None,
                        "failure_class": _fcls.WORKER_EXCEPTION,
                        "failing_role": role,
                        "raw_error_excerpt": str(_dispatch_exc)[:500],
                        "source": "delegate.dispatch_failed",
                    },
                )
        except Exception:  # noqa: BLE001 - crumb must never mask the raise
            pass
        raise
    orch.guardrails.post_invocation(envelope.task_id, result)
    # v0.29.0 Bug 6: stash the most recent adapter ``subtype`` (and
    # ``api_error_status``) on the orchestrator so the
    # GuardrailExceededError block site downstream can classify the
    # block as ``"infrastructure"`` (subtype is auth/transport-class)
    # vs ``"cap"`` (subtype is ``error_max_turns``/``None`` — agent
    # legitimately exhausted budget). Stored as plain attributes;
    # the orchestrator runs one task at a time per worker, and the
    # cross-phase parallel path tracks per-task state via the typed
    # ``in_flight`` map at the call site rather than the orchestrator.
    orch._last_adapter_subtype = result.subtype
    orch._last_adapter_api_error_status = result.api_error_status

    # v0.31.0 (Phase 3): record this dispatch's subtype against the
    # per-(task_id, role) tracker so the NEXT delegate call for the
    # same pair can decide whether to escalate. ``error_max_turns``
    # increments; any other subtype (success or different failure)
    # clears the counter. ``getattr`` keeps backward compat with the
    # older orchestrator stubs used by some unit tests.
    if _budget_tracker is not None and envelope.task_id:
        _budget_tracker.record_result(envelope.task_id, role, result.subtype)

    # v0.30.0 Bug 4: per-adapter-failure audit breadcrumb. Append one
    # ``adapter_failure`` ledger op for every ``success=False`` result
    # (transient OR fatal) so post-mortems can grep the ledger directly
    # for the count + shape of failures preceding a block, instead of
    # combing through ``.autodev/debug/*.txt`` tracebacks. Best-effort:
    # a ledger write failure here MUST NOT mask the underlying adapter
    # failure for the caller — the retry / escalate / block FSM
    # downstream still needs to see ``result`` exactly as the adapter
    # returned it.
    if not result.success and getattr(orch, "plan_manager", None) is not None:
        try:
            await orch.plan_manager.ledger_append(
                op="adapter_failure",
                payload={
                    "task_id": envelope.task_id,
                    "api_error_status": result.api_error_status,
                    "subtype": result.subtype,
                    "error": result.error,
                    "attempt_n": int(retry_count),
                },
            )
        except Exception as exc:  # noqa: BLE001 — forensics only, never fatal
            logger.warning(
                "execute_phase.adapter_failure_ledger_append_failed",
                task_id=envelope.task_id,
                err=str(exc),
            )

    # v0.30.0 Bug 5: feed the result into the cross-task circuit breaker.
    # Order matters — Bug 4's adapter_failure breadcrumb above lands FIRST
    # so the post-mortem ledger still records the failure even when the
    # breaker trip below raises. Success → reset the rolling counter (a
    # healthy adapter call clears any prior infra-flake history). Infra-
    # class failure → record and check; if the rolling-window count
    # crossed the trip threshold, raise
    # :class:`InfrastructureCircuitOpenError` so the existing v0.29.0
    # ``AuthenticationFailedError`` catch sites quarantine the in-flight
    # task and abort the phase loop. ``getattr`` is defensive: some unit-
    # test orchestrator stubs predate Bug 5 and lack the attribute — for
    # those the breaker logic is a silent no-op rather than a crash.
    _breaker = getattr(orch, "_circuit_breaker", None)
    if _breaker is not None:
        if result.success:
            # v0.37.0 H3: adapter success clears only the adapter-class
            # stream — the test-diagnosis stream must accumulate across
            # intervening healthy adapter calls (developer/reviewer) so
            # the "many tasks each producing one capture_failed" pattern
            # is detectable cross-task. Use ``reset_adapter`` if
            # available (post-H3 breaker); fall back to ``reset`` for
            # legacy stubs.
            _reset_method = getattr(
                _breaker, "reset_adapter", None
            ) or _breaker.reset
            _reset_method()
        else:
            from datetime import datetime as _datetime, timezone as _tz

            # ``datetime.now(timezone.utc)`` matches the orchestrator's
            # other UTC-aware time handling (see
            # :func:`_compute_retry_delay_s` which uses the same call).
            # The breaker normalizes naive stamps internally, but giving
            # it an aware stamp here keeps the call site self-documenting.
            _breaker.record_failure(
                envelope.task_id or "<unknown>",
                result.subtype,
                _datetime.now(_tz.utc),
            )
            _halt, _reason = _breaker.should_halt()
            if _halt:
                # v0.38.0 I4 (HK7): pass the in-flight task id
                # explicitly so the typed-halt handler doesn't have
                # to race-walk the plan to find which task this
                # raise belongs to. ``envelope.task_id`` is the
                # canonical identifier at this raise site.
                raise InfrastructureCircuitOpenError(
                    _reason or "infrastructure circuit open",
                    halted_task_id=envelope.task_id,
                )

    if result.success and result.text:
        orch.loop_detector.observe(envelope.task_id, role, result.text)

    # v0.15.0: record this dispatch into the PRM trajectory store so the
    # NEXT delegate call can run pattern detection against an updated
    # event log. Pure observation — no decisions are made here; the
    # injection logic at the top of this function consumes any pending
    # CourseCorrection from prior analyze() runs.
    if trajectory_store is not None and envelope.task_id:
        try:
            from orchestrator.prm import TrajectoryEvent

            trajectory_store.record(
                envelope.task_id,
                TrajectoryEvent(
                    timestamp=_t0,
                    role=role,
                    action=envelope.action or "dispatch",
                    target_files=tuple(envelope.files or []),
                    success=bool(result.success),
                    duration_s=max(0.0, _time.time() - _t0),
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.prm_record_failed",
                task_id=envelope.task_id,
                err=str(exc),
            )

    return result


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------


def _developer_envelope(task: Task, extra_issues: list[str]) -> DelegationEnvelope:
    acceptance = " | ".join(a.description for a in task.acceptance) or None
    context: dict = {"task_title": task.title, "task_description": task.description}
    # v0.8.0: surface the architect-tagged complexity bucket to the developer
    # so it can pace itself (a ``complex`` task gets 40 turns and should not
    # wrap up after 5 — the prompt-level hint reinforces the budget the
    # adapter-level ``max_turns`` enforces). Defaults to ``"medium"`` when
    # the architect didn't tag a bucket — matches the orchestrator's spec
    # fallback shape and avoids surfacing the raw ``None`` value.
    context["complexity"] = task.complexity or "medium"
    if extra_issues:
        context["prior_issues"] = extra_issues
    return DelegationEnvelope(
        task_id=task.id,
        target_agent="developer",
        action="implement",
        files=list(task.files),
        acceptance=acceptance,
        context=context,
    )


# v0.31.0 (Phase 1.4): generated / lock files that carry no review signal
# but routinely dominate diff bytes. Skipped wholesale by the chunked
# envelope so the per-file budget is spent on files a human reviewer
# would actually want to see.
_REVIEW_GENERATED_FILE_GLOBS: tuple[str, ...] = (
    "*.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "uv.lock",
    "poetry.lock",
    "Gemfile.lock",
    "composer.lock",
    "*.min.js",
    "*.min.css",
)
# Soft byte caps — not hard limits, the chunker rounds at file boundaries.
_REVIEW_PER_FILE_FULL_BYTES = 2_048  # files ≤ this size pass through whole.
_REVIEW_PER_FILE_HEAD_BYTES = 1_024  # head slice for oversize files.
_REVIEW_PER_FILE_TAIL_BYTES = 512  # tail slice for oversize files.
_REVIEW_TOTAL_ENVELOPE_BYTES = 32_768  # soft cap on the chunked envelope.
_REVIEW_DIFF_PASSTHROUGH_BYTES = 8_192  # diffs ≤ this size skip chunking.


def _matches_generated_glob(path: str) -> bool:
    """Return True if ``path`` matches any generated/lock file glob."""
    import fnmatch as _fn

    base = path.rsplit("/", 1)[-1]
    for pattern in _REVIEW_GENERATED_FILE_GLOBS:
        if _fn.fnmatch(path, pattern) or _fn.fnmatch(base, pattern):
            return True
    # Also skip any path containing a ``__pycache__/`` segment.
    return "__pycache__/" in path or path.startswith("__pycache__/")


def _split_diff_by_file(diff: str) -> list[tuple[str, str]]:
    """Split a unified diff into ``[(path, file_diff_text), ...]`` chunks.

    The split key is the standard ``diff --git a/<p> b/<p>`` header that
    git emits at the start of each file's section. Lines preceding the
    first header (rare; usually empty in our captured diffs) are
    discarded — they carry no per-file context worth preserving and
    would otherwise be appended to the first file's section misleadingly.
    """
    sections: list[tuple[str, str]] = []
    current_path: str | None = None
    current_lines: list[str] = []
    for line in diff.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_path is not None:
                sections.append((current_path, "".join(current_lines)))
            # ``diff --git a/foo/bar b/foo/bar`` — pull the b-side path
            # which is the post-change name (handles renames sensibly).
            parts = line.strip().split()
            current_path = parts[3][2:] if len(parts) >= 4 else "<unknown>"
            current_lines = [line]
        else:
            if current_path is None:
                # Pre-header preamble; drop.
                continue
            current_lines.append(line)
    if current_path is not None:
        sections.append((current_path, "".join(current_lines)))
    return sections


def _summarise_file_diff(file_diff: str) -> str:
    """Return ``"+N / -M, K hunks"`` summary of a file's diff section."""
    plus = 0
    minus = 0
    hunks = 0
    for line in file_diff.splitlines():
        if line.startswith("@@"):
            hunks += 1
        elif line.startswith("+") and not line.startswith("+++"):
            plus += 1
        elif line.startswith("-") and not line.startswith("---"):
            minus += 1
    return f"+{plus} / -{minus}, {hunks} hunks"


def _build_chunked_review_diff(diff: str) -> str:
    """v0.31.0 (Phase 1.4): build a chunked review envelope from ``diff``.

    Strategy:

    * If ``diff`` ≤ :data:`_REVIEW_DIFF_PASSTHROUGH_BYTES`: return as-is.
    * Otherwise split per-file and:
        - Skip generated / lock files entirely
          (:func:`_matches_generated_glob`).
        - Files ≤ :data:`_REVIEW_PER_FILE_FULL_BYTES`: include whole diff.
        - Files > full-bytes: include the path + a per-file summary
          (``"+N / -M, K hunks"``) plus head + tail byte slices.
    * Soft-cap the assembled envelope at
      :data:`_REVIEW_TOTAL_ENVELOPE_BYTES` and append a
      ``"# REMAINING: <n> files truncated"`` footer when the cap fires.

    Replaces the prior ``diff[:8000]`` hard truncation, which silently
    dropped any per-file context past the 8 KB mark — the reviewer
    couldn't see the files most likely to need scrutiny when those
    files happened to land late in the diff stream.
    """
    if len(diff.encode("utf-8")) <= _REVIEW_DIFF_PASSTHROUGH_BYTES:
        return diff

    sections = _split_diff_by_file(diff)
    if not sections:
        # No ``diff --git`` headers at all (e.g. raw text patch). Fall
        # back to a head-only slice rather than dropping the input.
        return diff[: _REVIEW_PER_FILE_FULL_BYTES * 4]

    parts: list[str] = []
    skipped_generated = 0
    truncated_files = 0
    total_bytes = 0
    files_emitted = 0
    files_remaining = 0

    for idx, (path, file_diff) in enumerate(sections):
        if _matches_generated_glob(path):
            skipped_generated += 1
            continue

        file_bytes = len(file_diff.encode("utf-8"))
        if total_bytes >= _REVIEW_TOTAL_ENVELOPE_BYTES:
            files_remaining = len(sections) - idx
            break

        if file_bytes <= _REVIEW_PER_FILE_FULL_BYTES:
            chunk = file_diff
        else:
            head = file_diff[:_REVIEW_PER_FILE_HEAD_BYTES]
            tail = (
                file_diff[-_REVIEW_PER_FILE_TAIL_BYTES:]
                if file_bytes > _REVIEW_PER_FILE_TAIL_BYTES
                else ""
            )
            summary = _summarise_file_diff(file_diff)
            chunk = (
                f"# {path} ({summary}; "
                f"showing first {_REVIEW_PER_FILE_HEAD_BYTES}B + "
                f"last {_REVIEW_PER_FILE_TAIL_BYTES}B of "
                f"{file_bytes}B total)\n"
                f"{head}\n# ... [truncated middle] ...\n{tail}"
            )
            truncated_files += 1

        parts.append(chunk)
        total_bytes += len(chunk.encode("utf-8"))
        files_emitted += 1

    envelope = "\n".join(parts)
    footer_bits: list[str] = []
    if skipped_generated:
        footer_bits.append(
            f"{skipped_generated} generated/lock file(s) skipped"
        )
    if truncated_files:
        footer_bits.append(
            f"{truncated_files} large file(s) chunked to head+tail"
        )
    if files_remaining:
        footer_bits.append(
            f"{files_remaining} file(s) omitted (envelope cap reached)"
        )
    if footer_bits:
        envelope += "\n\n# DIFF SUMMARY: " + "; ".join(footer_bits)
        envelope += f" — {files_emitted} file(s) included in full review."
    return envelope


def _review_envelope(task: Task, diff: str) -> DelegationEnvelope:
    return DelegationEnvelope(
        task_id=task.id,
        target_agent="reviewer",
        action="review",
        files=list(task.files),
        acceptance=(
            "Respond with VERDICT: APPROVED, VERDICT: NEEDS_CHANGES, or "
            "VERDICT: REJECTED on a line by itself. Follow with "
            "bullet-point issues if not APPROVED."
        ),
        context={
            "task_title": task.title,
            "task_description": task.description,
            # v0.31.0 (Phase 1.4): chunked envelope replaces the prior
            # ``diff[:8000]`` hard truncation. See
            # :func:`_build_chunked_review_diff` for the per-file
            # bytewise budget.
            "diff": _build_chunked_review_diff(diff),
        },
    )


def _test_envelope(task: Task, diff: str) -> DelegationEnvelope:
    return DelegationEnvelope(
        task_id=task.id,
        target_agent="test_engineer",
        action="test",
        files=list(task.files),
        acceptance=(
            "Run tests and return a line of the form 'RESULTS: passed=N "
            "failed=M total=T'. Include failure output if any test failed."
        ),
        context={
            "task_title": task.title,
            "diff": diff[:8000],
        },
    )


# ---------------------------------------------------------------------------
# Output parsers
# ---------------------------------------------------------------------------


def _parse_review_verdict(text: str) -> tuple[str, list[str]]:
    """Parse a reviewer's response into a (verdict, issues) tuple.

    v0.31.0 (Phase 1.3): hardened against two pre-existing failure modes:

    * **Empty / whitespace-only response** — used to return
      ``("NEEDS_CHANGES", ["empty reviewer response"])``, which masked
      the underlying machinery failure (Hypothesis A: max_tokens
      exhausted; Hypothesis C: structured-output schema rejected the
      envelope) as a content signal. Now returns
      ``("MALFORMED", ["empty reviewer response"])`` so the orchestrator
      can route it through the format-specific retry path. The issues
      list keeps the legacy ``"empty reviewer response"`` string for
      backward compatibility with existing log/monitoring greps.
    * **Prose with no verdict keyword** — used to silently default to
      ``APPROVED`` (Hypothesis B), which is the unsafest possible
      default. Now returns ``("MALFORMED", ...)``.

    The verdict is now searched anywhere in the response (not just the
    first non-empty line) and matched against the strict prefix
    ``"VERDICT: <KEYWORD>"`` first; if no such line exists we fall back
    to the legacy "first line containing a verdict keyword" search for
    backward compat with reviewers that pre-date the strict prompt.
    """
    if not text or not text.strip():
        return "MALFORMED", ["empty reviewer response"]

    # Strict ``VERDICT: <KEYWORD>`` line takes precedence — this is the
    # format the new (post-v0.31.0) reviewer system prompt instructs the
    # model to emit. Case-insensitive on the keyword; the prefix match
    # tolerates leading whitespace / list markers (``"- VERDICT: ..."``)
    # that some reviewers occasionally produce.
    import re as _re

    strict_pat = _re.compile(
        r"^\s*[-*]?\s*VERDICT\s*:\s*(APPROVED|NEEDS[_\s]CHANGES|REJECTED)\s*$",
        _re.IGNORECASE | _re.MULTILINE,
    )
    m = strict_pat.search(text)
    verdict: str | None = None
    if m is not None:
        token = m.group(1).upper().replace(" ", "_")
        verdict = token

    # Legacy fallback: scan every line for an unambiguous verdict
    # keyword. Unlike the pre-v0.31.0 parser this scans ALL lines (not
    # just the first non-empty one) so a reviewer that puts prose first
    # and the verdict on line 5 is parsed correctly. If multiple verdict
    # tokens appear, the last one wins — reviewers occasionally restate
    # their initial impression and converge on a final verdict at the
    # end.
    if verdict is None:
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            upper = s.upper()
            if "REJECTED" in upper:
                verdict = "REJECTED"
            elif "NEEDS_CHANGES" in upper or "NEEDS CHANGES" in upper:
                verdict = "NEEDS_CHANGES"
            elif "APPROVED" in upper:
                verdict = "APPROVED"

    # Strict default flipped from APPROVED → MALFORMED. The pre-v0.31.0
    # default was a latent footgun: any reviewer producing prose without
    # a recognised verdict keyword silently approved the change. Better
    # to surface the format failure as a typed signal the orchestrator
    # can handle (Phase 1.3 of the recovery plan).
    if verdict is None:
        verdict = "MALFORMED"

    issues: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("- ") or s.startswith("* "):
            issues.append(s[2:].strip())
    return verdict, issues


def _parse_test_counts(text: str) -> tuple[int, int, int]:
    """Parse ``RESULTS: passed=N failed=M total=T`` from test_engineer output.

    Very forgiving — missing values default to 0. If no RESULTS line is
    present, return (0, 0, 0) and let the orchestrator treat it as failure
    only if ``result.success`` is also False.
    """
    import re

    m = re.search(
        r"passed\s*=\s*(\d+)\s+failed\s*=\s*(\d+)\s+total\s*=\s*(\d+)",
        text,
        re.IGNORECASE,
    )
    if m is None:
        return 0, 0, 0
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


# Turn-exhaustion failure subtypes that are purely infrastructural — the
# agent ran out of turns on (huge-repo) exploration, NOT a semantic defect.
# ``error_max_turns`` is the per-attempt cap; ``error_max_turns_escalation_
# exhausted`` is the synthetic subtype the budget-escalation tracker returns
# once the per-(task, role) escalation ladder is spent. Both mean "more
# runway would have helped", never "the work is wrong".
_TURN_EXHAUSTION_SUBTYPES: frozenset[str] = frozenset(
    {"error_max_turns", "error_max_turns_escalation_exhausted"}
)


def _reviewer_exhausted_turns(
    orch: "Orchestrator",
    review_result: "AgentResult | None",
) -> bool:
    """v0.41.0 (A1): did the REVIEWER itself run out of turns?

    A reviewer that exhausts its turn budget returns an empty / truncated
    response, which :func:`_parse_review_verdict` classifies as
    ``MALFORMED`` — indistinguishable, at the text layer, from a developer
    diff so broken the reviewer couldn't form a verdict. The adapter
    ``subtype`` is the disambiguator: ``error_max_turns`` (or the
    escalation-exhausted synthetic subtype) means "reviewer ran out of
    room" (an INFRA failure), NOT "developer produced a bad diff".

    Prefers the reviewer's own :class:`AgentResult.subtype` when the
    caller captured it (non-tournament path). Falls back to
    ``orch._last_adapter_subtype`` (set by :func:`delegate` after every
    dispatch) so the review-tournament path — which doesn't surface a
    single ``AgentResult`` — is still covered.
    """
    if review_result is not None:
        return review_result.subtype in _TURN_EXHAUSTION_SUBTYPES
    last_subtype = getattr(orch, "_last_adapter_subtype", None)
    return last_subtype in _TURN_EXHAUSTION_SUBTYPES


async def _maybe_accept_approved_on_exhaustion(
    orch: "Orchestrator",
    task: "Task",
    developer_result: "AgentResult",
) -> "Task | None":
    """Tier J: accept an APPROVED-but-turn-exhausted task instead of blocking.

    Returns the completed ``Task`` when the approved artifact is accepted,
    or ``None`` (no-op) when the strict gate does not hold — in which case
    the caller falls through to the unchanged retry/escalate path.

    The gap (observed on a 358k-file repo): a Phase-0 research/confirmation
    task whose correct output is an EMPTY diff already has a reviewer
    ``APPROVED`` verdict on record (``{task_id}-review.json``), but the
    developer keeps hitting ``error_max_turns`` on broad codebase
    exploration. The discard/escalation ladder eventually soft-blocks the
    task as ``user_decision_required`` — *losing* the already-approved
    result. The failure is purely infrastructural turn-exhaustion, not a
    semantic verdict.

    Gate STRICTLY (all must hold):

    1. The developer result's ``subtype`` is a turn-exhaustion subtype
       (:data:`_TURN_EXHAUSTION_SUBTYPES`). A semantic NEEDS_CHANGES /
       REJECTED retry, a parse error, an auth/transport failure, etc. is
       NOT accepted — those must still block.
    2. A reviewer verdict of ``APPROVED`` is on record for this task's
       artifact (``{task_id}-review.json``). Any other verdict (or no
       review evidence at all) is NOT accepted.
    3. The current (turn-exhausted) developer attempt produced no diff —
       ``developer_result.diff`` is empty / whitespace-only. This is the
       reliable signal: a turn-exhausted attempt emits no patch, and an
       empty diff integrates as a no-op so completion is safe without an
       apply step. A non-empty in-hand diff is deliberately NOT auto-
       accepted here (applying an un-reviewed partial diff would be
       unsafe); it falls through to the unchanged path.

    Idempotent: marking the task ``complete`` is the terminal transition,
    so a re-entry (e.g. resume) re-reads the same APPROVED evidence and
    re-completes deterministically; the caller returns immediately on a
    non-None result, so the developer is never re-dispatched. Best-effort
    ledger / stuck-state side effects never mask the completion.
    """
    subtype = developer_result.subtype
    if subtype not in _TURN_EXHAUSTION_SUBTYPES:
        return None

    from state.evidence import read_evidence  # noqa: PLC0415 — break cycle

    try:
        review_ev = await read_evidence(orch.cwd, task.id, "review")
    except Exception as exc:  # noqa: BLE001 — defensive: never block the path
        logger.warning(
            "execute_phase.accept_approved_review_read_failed",
            task_id=task.id,
            err=str(exc),
        )
        return None
    # Only an APPROVED verdict sticks. NEEDS_CHANGES / REJECTED / MALFORMED
    # (or no review evidence) → no-op, fall through to retry/escalate.
    if review_ev is None or getattr(review_ev, "verdict", None) != "APPROVED":
        return None

    # The current (turn-exhausted) attempt must carry no diff — an empty
    # diff integrates as a no-op, so completion is safe without an apply
    # step. A turn-exhausted attempt reliably emits no patch; a non-empty
    # in-hand diff is out of scope (applying an un-reviewed partial diff
    # would be unsafe) and falls through to the unchanged path.
    in_hand_diff = developer_result.diff
    if in_hand_diff is not None and in_hand_diff.strip():
        return None

    logger.warning(
        "execute_phase.accepted_approved_on_exhaustion",
        task_id=task.id,
        subtype=subtype,
        verdict="APPROVED",
        diff_empty=True,
    )
    # Audit-only ledger breadcrumb. Best-effort — a ledger failure here
    # MUST NOT mask the completion the operator needs.
    if getattr(orch, "plan_manager", None) is not None:
        try:
            await orch.plan_manager.ledger_append(
                op="accepted_approved_on_exhaustion",
                payload={
                    "task_id": task.id,
                    "verdict": "APPROVED",
                    "subtype": subtype or "unknown",
                    "diff_empty": True,
                },
            )
        except Exception as exc:  # noqa: BLE001 — best-effort breadcrumb
            logger.warning(
                "execute_phase.ledger_append_failed",
                op="accepted_approved_on_exhaustion",
                err=str(exc),
            )

    # Mark complete. An empty diff integrates as a no-op, so there is
    # nothing to apply to main — the reviewer already certified the
    # empty diff as structurally correct. The task is ``in_progress`` at
    # this point (set at dispatch and reset on every retry), and the FSM
    # forbids a direct ``in_progress -> complete`` edge, so walk the
    # canonical happy-path pipeline states the approved artifact would
    # have traversed had it not been pre-empted by turn-exhaustion. Each
    # edge is legal per ``task_state.TASK_TRANSITIONS``; the final
    # ``tournamented -> complete`` carries the evidence bundle.
    completed = task
    for _status in ("coded", "auto_gated", "reviewed", "tested", "tournamented"):
        if completed.status == _status:
            continue
        completed = await orch.plan_manager.update_task_status(task.id, _status)
    completed = await orch.plan_manager.update_task_status(
        task.id,
        "complete",
        meta={"evidence_bundle": f".autodev/evidence/{task.id}-review.json"},
    )
    # Zero the stuck-state counters on success (mirrors the happy-path
    # completion tail). Best-effort — never mask the completion.
    try:
        await orch.plan_manager.reset_stuck_state(task.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "execute_phase.reset_stuck_state_failed",
            task_id=task.id,
            err=str(exc),
        )
    return completed


def _build_adapter_failure_reason(
    developer_result: AgentResult | None,
) -> str:
    """v0.26.1 patch D: surface ``developer_result.error`` + ``subtype`` in
    the escalation reason so the repetition_loop / stuck-recovery ladder
    sees semantic variation across genuinely-different failures.

    Prior to this patch every coder-adapter failure produced the
    identical string ``"coder adapter failure"`` regardless of the
    underlying cause (``error_max_turns`` vs ``error_max_tokens`` vs
    parser failure vs subprocess crash), so the repetition_loop course-
    correction misfired by matching on the cosmetic symptom.

    The returned reason starts with the legacy literal so any
    case-insensitive matcher still recognises adapter-class failures,
    appends the typed subtype, and the first 200 chars of the adapter's
    own error message.
    """
    base = "coder adapter failure"
    if developer_result is None:
        return f"{base} (unknown)"
    subtype = developer_result.subtype or "unknown"
    raw_error = developer_result.error or "adapter failure"
    truncated = raw_error[:200]
    return f"{base} ({subtype}): {truncated}"


# Gap 5 (containment): AutoDev owns the ``.autodev/`` directory in the
# target repo (evidence, ledger, tournaments, index DB, debug dumps). A
# task agent must never perceive AutoDev's own run-mechanics as the work
# to do. The observed derailment (corrective task ``0.c2`` on a 358k-file
# run) had a developer edit ``.autodev/evidence/0-drift-verifier.json``
# instead of the target repo's code, and that ``.autodev/``-only diff was
# accepted as legitimate task work. A diff confined ENTIRELY to
# ``.autodev/`` is the reliable signal for this class of derailment: real
# task work always touches at least one path outside AutoDev's own dir.
_AUTODEV_PATH_PREFIX = f"{AUTODEV_DIR}/"


def _path_is_autodev_owned(path: str) -> bool:
    """True iff *path* (repo-relative) lives under AutoDev's ``.autodev/``.

    Normalizes a leading ``./`` and matches both the directory itself
    (``.autodev``) and anything beneath it (``.autodev/...``). Conservative:
    a path that merely *starts with* the literal string but is a sibling
    (e.g. ``.autodev-notes/x``) is NOT matched.
    """
    p = path.strip()
    if p.startswith("./"):
        p = p[2:]
    return p == AUTODEV_DIR or p.startswith(_AUTODEV_PATH_PREFIX)


def _diff_confined_to_autodev(
    developer_result: AgentResult | None,
) -> bool:
    """Gap 5: True iff a NON-EMPTY developer diff touches ONLY ``.autodev/``.

    Returns ``False`` for a missing result, an empty / whitespace-only
    diff, or any diff that touches at least one path outside AutoDev's own
    directory. Only when the diff parses to a non-empty file set AND every
    one of those files is AutoDev-owned does this return ``True`` — the
    signal that the agent edited AutoDev's internals instead of the target
    repo.

    Empty-diff cases return ``False`` so legitimate research tasks (which
    correctly produce no diff and are gated separately on an APPROVED
    verdict) are never affected by this guard.
    """
    if developer_result is None or not developer_result.diff:
        return False
    try:
        files = extract_files_from_diff(developer_result.diff, strict=False)
    except Exception:  # noqa: BLE001 — defensive: never block on a parse quirk
        return False
    if not files:
        return False
    return all(_path_is_autodev_owned(f) for f in files)


def _files_changed_for_secretscan(
    developer_result: AgentResult | None,
) -> list[Path]:
    """Extract repo-relative paths from a developer ``AgentResult`` for the
    v0.13.0 diff-scoped secretscan.

    v0.26.1 patch C — contract simplified to always return a list:

    * ``[]`` when the result is missing or has no diff text.
    * ``list[Path]`` of repo-relative paths in first-seen, deduped order
      when a parseable diff is present.

    Callers do not need to special-case the empty case; passing
    ``paths=[]`` through to ``run_secretscan`` / ``run_hallucination_guard``
    /``run_mutation_test`` / ``run_code_size`` produces a clean "scan
    nothing, pass the gate" outcome.

    v0.27.0 (audit §6) — caller of last resort: when a non-empty diff
    body has no parseable ``+++ b/`` headers (garbage payload, truncated
    stream, etc.) this helper now raises :class:`errors.DiffParseError`
    so :func:`_run_qa_gates` can fail-closed against tasks declared as
    ``produces_diff=True`` rather than silently scan zero files.
    """
    if developer_result is None or not developer_result.diff:
        return []
    return [
        Path(p)
        for p in extract_files_from_diff(developer_result.diff, strict=True)
    ]


def _surface_warning(task: "Task", gate_name: str, result: GateResult) -> None:
    """Record a warn/info-severity gate result on the task without halting.

    v0.22.0: warn-severity GateResults (passed=True, severity="warn") and
    info-severity findings (passed=False, severity="info") are surfaced
    via the task's ``metadata["qa_warnings"]`` list so downstream
    consumers (evidence ledger, status command, knowledge seeding) can
    pick them up without a separate channel. The list is appended in
    gate-evaluation order; each entry is a small dict with the gate
    name, severity, details snippet, and structured metrics carrier.
    """
    if task.metadata is None:  # pragma: no cover — Task default is dict
        task.metadata = {}
    warnings = task.metadata.setdefault("qa_warnings", [])
    warnings.append(
        {
            "gate": gate_name,
            "severity": result.severity,
            "details": result.details,
            "metrics": result.metrics,
        }
    )


def _run_secretscan_with_cfg(
    cwd: Path, secretscan_paths: list[Path] | None, cfg: object
) -> Awaitable[GateResult]:
    """v0.23.0 C2: bridge that only forwards new C2 kwargs when set.

    Existing tests stub :func:`run_secretscan` with the v0.19.0 signature
    (no C2 kwargs). When the operator hasn't opted into the new fields,
    we omit them from the call so those mocks keep working. Only when
    the cfg explicitly carries any new C2 setting do we thread them
    through (and any test that exercises the C2 surface will mock
    accordingly).
    """
    extra: dict[str, object] = {}
    ignore = getattr(cfg, "secretscan_ignore_paths", None)
    if ignore:
        extra["ignore_paths"] = ignore
    eth = getattr(cfg, "secretscan_entropy_threshold", None)
    if eth is not None:
        extra["entropy_threshold_override"] = eth
    mlen = getattr(cfg, "secretscan_min_entropy_length", None)
    if mlen is not None:
        extra["min_entropy_length"] = mlen
    return run_secretscan(
        cwd,
        paths=secretscan_paths,
        per_extension_thresholds=cfg.secretscan_per_extension_thresholds,
        baseline_enabled=cfg.secretscan_baseline_enabled,
        **extra,
    )


# v0.34.0 B1: language-profile lookup for the hallucination allowlist.
# We use the cached ``.autodev/language_profile.json`` (written by
# ``runtime.language_profile``) so the QA-gate hot path does not pay
# the cost of a fresh repo scan on every dispatch. When the cache is
# absent the profile is recomputed once and persisted by that module.
def _hallucination_allowlist_for(cwd: Path) -> frozenset[str]:
    from qa.hallucination_guard import HALLUCINATION_ALLOWLISTS
    from runtime.language_profile import (
        compute_language_profile,
        get_dominant_language,
    )

    try:
        profile = compute_language_profile(cwd)
    except Exception:  # noqa: BLE001 — telemetry must never block the gate
        return frozenset()
    dominant = get_dominant_language(profile)
    return HALLUCINATION_ALLOWLISTS.get(dominant, frozenset())


# v0.34.0 B1: sparse-mode heuristic. The hallucination guard cannot see
# the full include / import chain in a sparse worktree, so the gate
# downgrades unresolved-symbol findings to warnings instead of blocking.
# True when sparse-checkout is opted into AND the repo is in huge-repo
# mode — the same conditions ``create_per_task`` uses to choose the
# sparse code path.
def _hallucination_sparse_mode_for(orch: "Orchestrator", cfg: object) -> bool:
    if not bool(getattr(cfg, "hallucination_guard_sparse_downgrade", True)):
        return False
    sparse_opted_in = bool(
        getattr(orch.cfg, "worktree_sparse_checkout_enabled", False)
    )
    huge_mode_cfg = getattr(orch.cfg, "worktree_huge_repo_mode", "auto")
    is_huge = bool(
        getattr(getattr(orch, "_repo_capacity", None), "is_huge", False)
    )
    if huge_mode_cfg == "on":
        huge_active = True
    elif huge_mode_cfg == "off":
        huge_active = False
    else:
        huge_active = is_huge
    return sparse_opted_in and huge_active


async def _run_qa_gates(
    orch: "Orchestrator",
    task: "Task",
    developer_result: AgentResult | None = None,
) -> str | None:
    """Run enabled QA gates. Returns the first failure detail string, or None if all pass.

    v0.13.0: ``developer_result`` is the optional output of the most recent
    developer delegation. When supplied, the secretscan gate is invoked
    with ``paths=`` extracted from its diff so only the executor's just-
    introduced changes are scanned. None preserves legacy whole-tree walk.

    v0.22.0: respects :class:`GateResult` severity. ``passed=False`` with
    the default ``severity="block"`` halts as before (byte-identical to
    v0.21.0). ``passed=True, severity="warn"`` and ``passed=False,
    severity="info"`` are surfaced via :func:`_surface_warning` and the
    gate dispatch continues. Existing gates that don't set ``severity``
    inherit the "block" default — pre-v0.22.0 behavior is preserved.
    """
    from errors import DiffParseError
    from plugins.registry import QAContext

    cfg = orch.cfg.qa_gates
    cwd = orch.cwd
    language = detect_language(cwd)

    # v0.27.0 (audit §6): fail-closed when a diff-producing task ships a
    # malformed diff body. Investigation tasks (``produces_diff=False``)
    # legitimately have no diff — treat their unparseable diff as the
    # legacy ``paths=[]`` no-op so the secretscan / hallucination_guard
    # gates skip cleanly. For everyone else, the gate-set returns a
    # blocking failure detail string before any gate runs.
    try:
        secretscan_paths = _files_changed_for_secretscan(developer_result)
    except DiffParseError as exc:
        if getattr(task, "produces_diff", True) is False:
            secretscan_paths = []
        else:
            return (
                "secretscan: developer diff unparseable, refusing to silently "
                f"skip diff-scoped QA gates ({exc})"
            )
    # v0.16.0: hallucination-guard reuses the diff-scope path list so the
    # AST walk only visits files the executor just touched. ``cfg.hallucination_guard``
    # is a top-level toggle (default True) — see :class:`AutodevConfig`.
    hallucination_guard_enabled = bool(
        getattr(orch.cfg, "hallucination_guard", True)
    )

    # v0.22.1 A2: auto-skip secretscan on huge repos to avoid the
    # false-positive avalanche observed on Unity (27K-50K FPs from asset
    # GUIDs). Operators can force-enable via
    # ``cfg.secretscan_force_run_on_huge_repo``.
    repo_capacity = getattr(orch, "_repo_capacity", None)
    secretscan_huge_skip = bool(
        repo_capacity is not None
        and getattr(repo_capacity, "is_huge", False)
        and getattr(cfg, "secretscan_auto_skip_huge_repo", True)
        and not getattr(cfg, "secretscan_force_run_on_huge_repo", False)
    )
    secretscan_enabled = cfg.secretscan and not secretscan_huge_skip
    if secretscan_huge_skip and cfg.secretscan:
        # Surface the skip so operators see it in the orchestrator log
        # without having to dig into config. Mirrors the pattern of
        # :func:`_surface_warning` used elsewhere for soft gate events.
        import logging as _logging

        _logging.getLogger(__name__).warning(
            "qa.secretscan.auto_skipped_huge_repo file_count=%s total_bytes=%s; "
            "set cfg.qa_gates.secretscan_force_run_on_huge_repo=True to override",
            getattr(repo_capacity, "file_count", "?"),
            getattr(repo_capacity, "total_bytes", "?"),
        )

    # ADR-0046: the reproduce + debug-tag gates ride on the diagnosis-phase
    # toggle (the natural switch). ``getattr`` keeps legacy configs (which
    # predate ``cfg.diagnosis``) safe — they degrade to disabled.
    diagnosis_gates_enabled = bool(
        getattr(getattr(orch.cfg, "diagnosis", None), "enabled", False)
    )

    # v0.22.0: gates are ``(name, enabled, callable)`` triples so the
    # warn-surface helper can attribute findings to a gate.
    gates: list[tuple[str, bool, Callable[[], Awaitable[GateResult]]]] = [
        ("syntax_check", cfg.syntax_check, lambda: run_syntax_check(cwd, language)),
        ("lint", cfg.lint, lambda: run_lint(cwd, language, paths=secretscan_paths, timeout_s=cfg.lint_timeout_s)),
        ("build_check", cfg.build_check, lambda: run_build_check(cwd, language)),
        ("test_runner", cfg.test_runner, lambda: run_tests(cwd, paths=secretscan_paths, timeout_s=cfg.test_timeout_s)),
        (
            "secretscan",
            secretscan_enabled,
            lambda: _run_secretscan_with_cfg(
                cwd,
                secretscan_paths,
                cfg,
            ),
        ),
        (
            "hallucination_guard",
            hallucination_guard_enabled,
            lambda: run_hallucination_guard(
                cwd,
                paths=secretscan_paths,
                extra_skip_dirs=getattr(
                    cfg, "hallucination_guard_skip_dirs", None
                ),
                allowlist=_hallucination_allowlist_for(cwd),
                sparse_mode=_hallucination_sparse_mode_for(orch, cfg),
                task_id=task.id,
                cfg=cfg,
            ),
        ),
        (
            "mutation_test",
            cfg.mutation_test_enabled,
            lambda: run_mutation_test(
                cwd,
                paths=secretscan_paths,
                kill_rate_threshold=cfg.mutation_test_threshold,
            ),
        ),
        (
            "code_size",
            getattr(cfg, "code_size", False),
            lambda: run_code_size(
                cwd,
                paths=secretscan_paths,
                thresholds=getattr(cfg, "code_size_thresholds", None),
                baseline_enabled=getattr(
                    cfg, "code_size_baseline_enabled", False
                ),
            ),
        ),
        # ADR-0046 QA gates — gated on the diagnosis phase toggle (the natural
        # switch). ``reproduce_gate`` is POST-fix: it BLOCKS only when a
        # persisted diagnosis reproduction loop still fails, else soft-passes
        # (no loop / fidelity none|live / unavailable). ``debug_tag`` is cheap
        # and BLOCKS on leftover ``[DEBUG-...]`` markers. Both take the same
        # diff-scoped ``secretscan_paths`` and return GateResult, so the
        # severity-dispatch loop below handles them with no extra code.
        (
            "reproduce_gate",
            diagnosis_gates_enabled,
            lambda: run_reproduce_gate(cwd, secretscan_paths),
        ),
        (
            "debug_tag",
            diagnosis_gates_enabled,
            lambda: run_debug_tag_gate(cwd, secretscan_paths),
        ),
    ]

    for name, enabled, gate_fn in gates:
        if not enabled:
            continue
        result: GateResult = await gate_fn()
        # v0.22.0 severity dispatch:
        #   * passed=False AND severity=="block" (legacy default) → halt.
        #   * passed=True AND severity=="warn" → surface as warning, continue.
        #   * passed=False AND severity=="info" → surface as info, continue.
        #   * other combos (passed=True silent, etc.) → no-op.
        severity = getattr(result, "severity", "block")
        if not result.passed and severity == "block":
            return result.details or "QA gate failed"
        if (result.passed and severity == "warn") or (
            not result.passed and severity == "info"
        ):
            _surface_warning(task, name, result)
            logger.info(
                "execute_phase.qa_gate_warning",
                task_id=task.id,
                gate=name,
                severity=severity,
                details=(result.details or "")[:200],
            )

    # Run plugin QA gates after all built-in gates pass.
    if hasattr(orch, "plugin_registry") and orch.plugin_registry is not None:
        ctx = QAContext(cwd=cwd, task_id=task.id)
        for plugin in orch.plugin_registry.qa_gates.values():
            try:
                plugin_result = await plugin.run(ctx)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "execute_phase.plugin_gate_error",
                    task_id=task.id,
                    plugin=plugin.name,
                    error=str(exc),
                )
                continue
            severity = getattr(plugin_result, "severity", "block")
            if not plugin_result.passed and severity == "block":
                return plugin_result.details or f"plugin gate '{plugin.name}' failed"
            if (plugin_result.passed and severity == "warn") or (
                not plugin_result.passed and severity == "info"
            ):
                _surface_warning(task, f"plugin:{plugin.name}", plugin_result)

    return None


async def _record_lessons(
    orch: "Orchestrator",
    task_id: str,
    output_text: str,
    role: str,
) -> None:
    """Scan ``output_text`` for ``LESSON:`` prefixed lines and record each.

    Extraction is lightweight: only lines that start with ``LESSON:``
    (case-insensitive, after stripping whitespace) are recorded.  Each lesson
    is recorded with confidence 0.7 and the agent's role as ``role_source``.

    If ``orch.knowledge`` is None or recording raises, a WARNING is logged and
    execution continues — knowledge errors must never block task completion.
    """
    if not output_text:
        return
    lessons: list[str] = []
    for line in output_text.splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("LESSON:"):
            lesson_text = stripped[len("LESSON:"):].strip()
            if lesson_text:
                lessons.append(lesson_text)
    if not lessons:
        return
    for lesson_text in lessons:
        try:
            await orch.knowledge.record(lesson_text, role, confidence=0.7)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "execute_phase.knowledge_record_failed",
                task_id=task_id,
                role=role,
                err=str(exc),
            )


__all__ = [
    "ConflictResolution",
    "StuckResolution",
    "TaskEscalatedError",
    "delegate",
    "run_execute_phase",
]
