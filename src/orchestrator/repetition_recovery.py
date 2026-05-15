"""v0.32.0 Phase 4.4: repetition-loop recovery action taxonomy.

The orchestrator-critic detects ``repetition_loop`` patterns (see
:mod:`orchestrator.prm`) and the v0.32.0 escalation ladder forces
``PIVOT`` when the pattern fires (see :func:`orchestrator.escalation_ladder.next_step`).
But knowing "we're stuck" is only half the story — the harder question
is *what to do about it*.

This module answers that question with a small, deterministic policy
function that maps the per-task counters + PRM signal onto a
:class:`RecoveryAction` label. The orchestrator's
``_try_retry_or_escalate`` site reads the chosen action and routes to
the matching dispatch path:

==================  ==============================================
RecoveryAction      Caller dispatch
==================  ==============================================
switch_tactic       Existing refine path with KB-augmented context.
                    Cheap; sharper guidance via past-failure lookup.
increase_scope      Existing architect-consult path with "split task"
                    hint. Used when the same files repeatedly fail
                    in isolation — the architect should split the
                    surface area into smaller pieces.
decrease_scope      Existing refine path with "narrow scope" hint.
                    Used when the agent over-reaches.
re_architect        Architect_b consult — full structural rethink.
                    Used when discard_count is high and no pivots
                    have happened (we never tried a redirect).
kb_lookup           Inject past-failure summaries into the next
                    critic prompt. Currently a sub-step of
                    ``switch_tactic`` — kept distinct so a future
                    caller can choose lookup-only without a refine.
ask_human           Soft-block with :class:`RecoveryHint`. Used
                    when we have exhausted autonomous escalation
                    rungs (architect already weighed in OR very high
                    discard count).
do_nothing          Accept the current candidate — autoreason
                    "do nothing wins" when QA gates pass and the
                    only signal is a repetition loop on a candidate
                    that is already correct.
==================  ==============================================

The thresholds are intentionally conservative — under-escalation is
preferred to over-escalation because every architect/human invocation
has a real wall-clock cost.
"""

from __future__ import annotations

from typing import Literal


RecoveryAction = Literal[
    "switch_tactic",
    "increase_scope",
    "decrease_scope",
    "re_architect",
    "kb_lookup",
    "ask_human",
    "do_nothing",
]


# Threshold constants — kept module-private so tests + future calibration
# can override via monkeypatch without touching call sites.
_LOW_DISCARD_CEILING: int = 2
_INCREASE_SCOPE_DISCARD: int = 3
_RE_ARCHITECT_DISCARD: int = 4
_ASK_HUMAN_DISCARD: int = 5


def choose_recovery_action(
    discard_count: int,
    pivot_count: int,
    architect_count: int,
    qa_gates_passed: bool,
    repetition_loop_detected: bool,
) -> RecoveryAction:
    """Pick the recovery action for a stuck task.

    The decision tree (in priority order):

    1. ``qa_gates_passed`` AND ``repetition_loop_detected`` AND
       ``discard_count >= 2`` → ``"do_nothing"`` (autoreason convergence
       — the candidate is correct; the loop is the agent failing to
       *believe* it is correct).
    2. ``architect_count >= 1`` OR ``discard_count >= 5`` → ``"ask_human"``
       (autonomous escalation budget exhausted).
    3. ``discard_count >= 4`` AND ``pivot_count == 0`` → ``"re_architect"``
       (we never tried a redirect; architect should rethink).
    4. ``discard_count == 3`` AND ``pivot_count == 0`` →
       ``"increase_scope"`` (architect splits task into smaller pieces).
    5. ``discard_count <= 2`` AND ``repetition_loop_detected`` →
       ``"switch_tactic"`` (cheap; sharper guidance via KB lookup).
    6. Else → ``"switch_tactic"`` (default safe action — bias toward
       the cheapest non-trivial intervention).

    The argument order matches the orchestrator's existing per-task
    state shape — ``StuckState.discard_count`` /
    ``StuckState.pivot_count`` / ``StuckState.architect_count`` plus a
    boolean from the most recent QA gate run plus a boolean from
    :func:`orchestrator.prm.detect_repetition_loop`.
    """
    # Rule 1: do_nothing — autoreason convergence on a correct candidate.
    if (
        qa_gates_passed
        and repetition_loop_detected
        and discard_count >= _LOW_DISCARD_CEILING
    ):
        return "do_nothing"

    # Rule 2: ask_human — escalation budget exhausted.
    if architect_count >= 1 or discard_count >= _ASK_HUMAN_DISCARD:
        return "ask_human"

    # Rule 3: re_architect — high discards with no prior pivot.
    if discard_count >= _RE_ARCHITECT_DISCARD and pivot_count == 0:
        return "re_architect"

    # Rule 4: increase_scope — architect splits the task.
    if discard_count == _INCREASE_SCOPE_DISCARD and pivot_count == 0:
        return "increase_scope"

    # Rule 5: switch_tactic on detected repetition with low discards.
    if discard_count <= _LOW_DISCARD_CEILING and repetition_loop_detected:
        return "switch_tactic"

    # Rule 6: default safe action.
    return "switch_tactic"


__all__ = [
    "RecoveryAction",
    "choose_recovery_action",
]
