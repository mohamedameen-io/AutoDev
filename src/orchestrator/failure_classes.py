"""Canonical failure-class taxonomy for the Universal Blocker Resolver (ADR-0047).

Before v0.42.0 the orchestrator expressed terminal failures as ad-hoc string
literals scattered across ``execute_phase.py`` (``"dag_invalid"``,
``"test_diagnosis"``, ``"edit_scope_violation"`` …) and embedded them in
``blocked_reason`` metadata. The blocker resolver needs a single, named
vocabulary so it can (a) route a known failure to a deterministic fast-path
action without an LLM call, and (b) recognise an *unrecognised* class and hand
it to the LLM resolver. This module is that vocabulary.

The taxonomy is intentionally coarse — one entry per terminal *site class* from
the recovery audit, plus the phase-degrade classes and a catch-all
:data:`UNKNOWN` for novel/unseen failures (the case the resolver exists to
handle). ``failure_class`` strings on :class:`state.schemas.BlockerContext` are
expected (but not required) to be one of :data:`ALL_FAILURE_CLASSES`; an
arbitrary string is treated as :data:`UNKNOWN` by :func:`classify`.
"""

from __future__ import annotations

from typing import Literal

# --- Terminal failure sites in execute_phase.py (the recovery audit) ----------
# DAG / scope — structural plan errors (resolver typically re-plans or falls
# through to the legacy block; these are not task-local).
DAG_INVALID = "dag_invalid"
CROSS_PHASE_DAG_INVALID = "cross_phase_dag_invalid"
EDIT_SCOPE_VIOLATION = "edit_scope_violation"

# Merge-conflict escalation.
CONFLICT_3WAY_FAILED = "conflict_3way_failed"
CONFLICT_ABANDON = "conflict_abandon"
CONFLICT_REWRITE_CAP_EXCEEDED = "conflict_rewrite_cap_exceeded"

# Guardrail exhaustion (decision-cost / turn budget overruns).
GUARDRAIL_EXCEEDED = "guardrail_exceeded"

# Oversized input (context-window bloat). RECOVERY-CONTRACT §7 Step 8 / the A4
# root cause: a role (esp. ``critic_t``) fed an oversized prompt burns turns
# reading tools/context and hits ``error_max_turns``. Today ``budget_escalation``
# then grants it MORE turns — the WRONG direction. This class routes such a
# failure to a BOUND_INPUT recovery (truncate / decompose / re-dispatch with
# reduced scope) and explicitly does NOT escalate turns. It is task-local
# (re-dispatch the same task with a smaller prompt), so it is NOT structural.
OVERSIZED_INPUT = "oversized_input"

# Test-diagnosis terminal signals.
TEST_DIAGNOSIS_HARDFAIL = "test_diagnosis_hardfail"
TEST_DIAGNOSIS_NO_SIGNAL = "test_diagnosis_no_signal"

# Worker crash (developer/test adapter raised at the code layer).
WORKER_EXCEPTION = "worker_exception"

# --- Retry-loop terminal sites (Step 3: real classes for _try_retry_or_escalate)
# These are the in-loop QA/review/test failures the escalation ladder threads to
# its terminal rung. They are retry-mappable (the resolver can retry / consult /
# rescope), so they are NOT in STRUCTURAL_FAILURE_CLASSES.
# Auto-gate (syntax/lint/build/test_runner/secretscan) failure.
QA_GATE_FAILED = "qa_gate_failed"
# Review tournament hit max_rounds without convergence.
REVIEW_ESCALATED = "review_escalated"
# Reviewer verdict was unparseable (not a turn-budget exhaustion).
REVIEW_MALFORMED = "review_malformed"
# Reviewer returned NEEDS_CHANGES / REJECTED.
REVIEW_REJECTED = "review_rejected"
# Tests collected and ran but at least one failed (diagnosis == "ok").
TESTS_FAILED = "tests_failed"

# Infra circuit breaker (auth/rate-limit/server failures across tasks).
INFRA_CIRCUIT_OPEN = "infra_circuit_open"

# Soft-blocker handoff rung (escalation ladder terminal).
SOFT_BLOCKER = "soft_blocker"

# Worktree apply failure (patch could not be applied to the working tree).
WORKTREE_APPLY_FAILED = "worktree_apply_failed"

# Worktree diff-check failure (could not determine whether the worktree holds
# unapplied changes — e.g. the worktree was removed, or git raised). Blocking
# here is safe-fail: we cannot know whether approved changes exist, so we must
# not silently complete the task (the exact silent-loss class A4 prevents).
WORKTREE_DIFF_CHECK_FAILED = "worktree_diff_check_failed"

# --- Phase-degrade classes (intake/diagnosis/framing convert silent degrade) --
PHASE_DEGRADED = "phase_degraded"

# --- Catch-all: a failure the deterministic ladder does not recognise. This is
# the class the LLM resolver exists to handle. ---------------------------------
UNKNOWN = "unknown"

FailureClass = Literal[
    "dag_invalid",
    "cross_phase_dag_invalid",
    "edit_scope_violation",
    "conflict_3way_failed",
    "conflict_abandon",
    "conflict_rewrite_cap_exceeded",
    "guardrail_exceeded",
    "oversized_input",
    "test_diagnosis_hardfail",
    "test_diagnosis_no_signal",
    "worker_exception",
    "qa_gate_failed",
    "review_escalated",
    "review_malformed",
    "review_rejected",
    "tests_failed",
    "infra_circuit_open",
    "soft_blocker",
    "worktree_apply_failed",
    "worktree_diff_check_failed",
    "phase_degraded",
    "unknown",
]

ALL_FAILURE_CLASSES: tuple[str, ...] = (
    DAG_INVALID,
    CROSS_PHASE_DAG_INVALID,
    EDIT_SCOPE_VIOLATION,
    CONFLICT_3WAY_FAILED,
    CONFLICT_ABANDON,
    CONFLICT_REWRITE_CAP_EXCEEDED,
    GUARDRAIL_EXCEEDED,
    OVERSIZED_INPUT,
    TEST_DIAGNOSIS_HARDFAIL,
    TEST_DIAGNOSIS_NO_SIGNAL,
    WORKER_EXCEPTION,
    QA_GATE_FAILED,
    REVIEW_ESCALATED,
    REVIEW_MALFORMED,
    REVIEW_REJECTED,
    TESTS_FAILED,
    INFRA_CIRCUIT_OPEN,
    SOFT_BLOCKER,
    WORKTREE_APPLY_FAILED,
    WORKTREE_DIFF_CHECK_FAILED,
    PHASE_DEGRADED,
    UNKNOWN,
)

# Structural plan errors: the resolver should NOT attempt task-local recovery
# (these need architect re-planning or are intentionally phase-wide). Wiring
# still routes them through ``resolve_blocker`` for observability, but the
# default policy is to re-plan or fall through to the legacy block.
STRUCTURAL_FAILURE_CLASSES: frozenset[str] = frozenset(
    {DAG_INVALID, CROSS_PHASE_DAG_INVALID, EDIT_SCOPE_VIOLATION, INFRA_CIRCUIT_OPEN}
)

# WS3: the three merge-conflict escalation classes that terminate the conflict
# cascade at ``block_task``. The validated-patch conflict-recovery hook
# (``execute_phase._maybe_recover_validated_patch_on_conflict_exhaustion``) fires
# on EXACTLY these three — a task discarded over a *mechanical* merge collision
# despite an already-validated (genuine-APPROVED + converged tournament winner)
# result — and no others. A broader gate would recover over unrelated failure
# classes; a narrower one would leave a discard class on the table.
CONFLICT_EXHAUSTION_FAILURE_CLASSES: frozenset[str] = frozenset(
    {CONFLICT_3WAY_FAILED, CONFLICT_ABANDON, CONFLICT_REWRITE_CAP_EXCEEDED}
)


def classify(raw: str | None) -> str:
    """Normalise an arbitrary failure-class string to a known class.

    Returns the string unchanged if it is in :data:`ALL_FAILURE_CLASSES`,
    otherwise :data:`UNKNOWN` (the novel-failure path the resolver handles).
    """
    if raw is not None and raw in ALL_FAILURE_CLASSES:
        return raw
    return UNKNOWN


def is_known(raw: str | None) -> bool:
    """True if ``raw`` names a known failure class (not the catch-all)."""
    return raw is not None and raw in ALL_FAILURE_CLASSES and raw != UNKNOWN


def classify_max_turns_failure(prompt_len: int, threshold: int) -> str:
    """Classify an ``error_max_turns`` failure by the size of its input prompt.

    RECOVERY-CONTRACT §7 Step 8 / the A4 root cause. A role that exhausts its
    turn budget did so for one of two reasons:

    * the prompt was a normal size and the task legitimately needs more runway
      (``GUARDRAIL_EXCEEDED`` — the existing budget-escalation ladder is the
      right remedy: grant more turns); or
    * the prompt was *oversized* — the agent burned its turns reading
      tools/context to digest the bloat and never got to the real work
      (``OVERSIZED_INPUT`` — the remedy is to BOUND the input, NOT to grant more
      turns; more turns just burn more budget on the same bloat).

    ``prompt_len`` is the character length of the dispatched prompt (the real
    size source — :attr:`adapters.types.AgentInvocation.prompt`). ``threshold``
    is the inclusive char cutoff (``cfg.budget_escalation.oversized_input_char_threshold``).
    The boundary is inclusive: ``prompt_len >= threshold`` is oversized.
    """
    if prompt_len >= threshold:
        return OVERSIZED_INPUT
    return GUARDRAIL_EXCEEDED


__all__ = [
    "FailureClass",
    "ALL_FAILURE_CLASSES",
    "STRUCTURAL_FAILURE_CLASSES",
    "CONFLICT_EXHAUSTION_FAILURE_CLASSES",
    "classify",
    "classify_max_turns_failure",
    "is_known",
    # Constants
    "DAG_INVALID",
    "CROSS_PHASE_DAG_INVALID",
    "EDIT_SCOPE_VIOLATION",
    "CONFLICT_3WAY_FAILED",
    "CONFLICT_ABANDON",
    "CONFLICT_REWRITE_CAP_EXCEEDED",
    "GUARDRAIL_EXCEEDED",
    "OVERSIZED_INPUT",
    "TEST_DIAGNOSIS_HARDFAIL",
    "TEST_DIAGNOSIS_NO_SIGNAL",
    "WORKER_EXCEPTION",
    "QA_GATE_FAILED",
    "REVIEW_ESCALATED",
    "REVIEW_MALFORMED",
    "REVIEW_REJECTED",
    "TESTS_FAILED",
    "INFRA_CIRCUIT_OPEN",
    "SOFT_BLOCKER",
    "WORKTREE_APPLY_FAILED",
    "WORKTREE_DIFF_CHECK_FAILED",
    "PHASE_DEGRADED",
    "UNKNOWN",
]
