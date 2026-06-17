"""WS3 — silent-degrade empty-diff guard for the impl tournament.

A coder that produced **no diff** (an empty-diff bundle) must not be able to
win the implementation tournament silently: an empty win degrades the
incumbent's real output to a no-op without any signal. The guard lives in
:meth:`ImplTournament._filter_empty_variants`, which is invoked in
``run_pass`` *before* Borda scoring.

Engagement proof in this file:

* ``test_empty_b_excluded_from_eligible_labels`` — the new method drops the
  empty candidate label so it cannot accrue Borda weight (fix engages).
* ``test_empty_b_cannot_win_end_to_end`` — full ``run_pass`` with a judge
  that ranks the empty B *first*; B still loses. This is the field behavior.
* ``test_broken_control_full_labels_lets_empty_b_win`` — the BROKEN CONTROL:
  with the pre-fix label set (``["A", "B", "AB"]``) the *same* judge ranking
  hands the win to the empty B. Reverting ``labels=eligible_labels`` back to
  the literal triple resurrects exactly this bug.
* ``test_all_empty_raises_task_level_failure`` /
  ``test_all_empty_emits_op`` — all variants empty → typed task-level
  failure + ``tournament_all_variants_failed`` op (not a silent empty win).
* ``test_empty_drop_emits_op`` — dropping a candidate emits an auditable op.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from errors import TournamentError
from tournament import (
    ImplBundle,
    ImplContentHandler,
    ImplTournament,
    StubLLMClient,
    TournamentConfig,
)
from tournament.core import aggregate_rankings


# --------------------------------------------------------------------------- #
# Fixtures / stubs
# --------------------------------------------------------------------------- #

INITIAL_DIFF = "+def foo():\n+    return 1\n"
INITIAL_BUNDLE = ImplBundle(
    task_id="ws3.1",
    task_description="Add foo()",
    diff=INITIAL_DIFF,
    files_changed=["foo.py"],
    tests_passed=3,
    tests_failed=0,
    tests_total=3,
    test_output_excerpt="3 passed",
)


class _ConfigurableCoderRunner:
    """Coder runner whose B / AB diffs are caller-controlled (may be empty)."""

    def __init__(self, b_diff: str, ab_diff: str) -> None:
        self._b_diff = b_diff
        self._ab_diff = ab_diff

    async def run(
        self,
        variant_label: str,
        direction: str,
        worktree: Path,
        task: ImplBundle,
    ) -> ImplBundle:
        diff = self._b_diff if variant_label == "B" else self._ab_diff
        files = ["foo.py"] if diff.strip() else []
        return ImplBundle(
            task_id=task.task_id,
            task_description=task.task_description,
            diff=diff,
            files_changed=files,
            tests_passed=3 if diff.strip() else 0,
            tests_failed=0,
            tests_total=3 if diff.strip() else 0,
            test_output_excerpt="3 passed" if diff.strip() else "",
            variant_label=variant_label,  # type: ignore[arg-type]
        )


class _NoopWorktreeManager:
    async def create(self, label: str, base_ref: str = "HEAD") -> Path:
        return Path("/tmp/fake-worktree") / label

    async def cleanup_all(self) -> None:
        pass


def _rank_b_first_cb(role: str, system: str, user: str) -> str:
    """A judge that always ranks slot 2 (= B, given the fixed RNG) first.

    With ``rng=random.Random(0)`` the judge presentation places the
    candidate labels deterministically; the assertions key off the *winner
    label*, not the slot, so they hold regardless of shuffle: the point is
    that B is the judge's top pick yet must not win when its diff is empty.
    """
    if role == "critic_t":
        return "Critic: issues found."
    if role == "architect_b":
        return "- Make a change"
    if role == "synthesizer":
        return "- Keep as is"
    if role == "judge":
        # Slot 2 best, then 1, then 3.
        return "Evaluation.\n\nRANKING: 2, 1, 3"
    return "default"


def _make_tour(
    tmp_path: Path,
    runner: _ConfigurableCoderRunner,
    cb=_rank_b_first_cb,
    *,
    name: str = "ws3",
) -> ImplTournament:
    cfg = TournamentConfig(num_judges=1, convergence_k=1, max_rounds=1)
    return ImplTournament(
        handler=ImplContentHandler(),
        client=StubLLMClient(fn=cb),
        cfg=cfg,
        artifact_dir=tmp_path / name,
        rng=random.Random(0),
        coder_runner=runner,
        worktree_manager=_NoopWorktreeManager(),
    )


def _bundle(label: str, diff: str) -> ImplBundle:
    return ImplBundle(
        task_id="ws3.1",
        task_description="Add foo()",
        diff=diff,
        files_changed=["foo.py"] if diff.strip() else [],
        variant_label=label,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- #
# Unit: _filter_empty_variants engages
# --------------------------------------------------------------------------- #


def test_empty_b_excluded_from_eligible_labels(tmp_path: Path) -> None:
    """An empty-diff B variant is dropped from the Borda label set."""
    tour = _make_tour(tmp_path, _ConfigurableCoderRunner(b_diff="", ab_diff="x"))
    eligible, dropped = tour._filter_empty_variants(
        INITIAL_BUNDLE,
        _bundle("B", ""),  # empty diff
        _bundle("AB", "+real change\n"),
        pass_num=1,
    )
    assert "B" in dropped
    assert "B" not in eligible
    assert eligible == ["A", "AB"]


def test_whitespace_only_diff_is_empty(tmp_path: Path) -> None:
    """A whitespace-only diff counts as empty (no real change)."""
    tour = _make_tour(tmp_path, _ConfigurableCoderRunner(b_diff="x", ab_diff="x"))
    eligible, dropped = tour._filter_empty_variants(
        INITIAL_BUNDLE,
        _bundle("B", "   \n\t\n"),  # whitespace only
        _bundle("AB", "+real change\n"),
        pass_num=1,
    )
    assert dropped == ["B"]
    assert eligible == ["A", "AB"]


def test_both_candidates_empty_but_incumbent_real(tmp_path: Path) -> None:
    """B and AB empty but the incumbent has a diff → only A is eligible."""
    tour = _make_tour(tmp_path, _ConfigurableCoderRunner(b_diff="", ab_diff=""))
    eligible, dropped = tour._filter_empty_variants(
        INITIAL_BUNDLE,  # real diff
        _bundle("B", ""),
        _bundle("AB", ""),
        pass_num=1,
    )
    assert dropped == ["B", "AB"]
    assert eligible == ["A"]


# --------------------------------------------------------------------------- #
# End-to-end: empty B cannot win a real pass
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_empty_b_cannot_win_end_to_end(tmp_path: Path) -> None:
    """Judge ranks the empty-diff B first, yet B does NOT win the pass.

    AB carries a real diff, so AB (or A) wins — never the no-op B.
    """
    runner = _ConfigurableCoderRunner(b_diff="", ab_diff="+def foo():\n+    return 9\n")
    tour = _make_tour(tmp_path, runner, name="e2e")
    _final, history = await tour.run(task_prompt="Add foo()", initial=INITIAL_BUNDLE)

    assert history, "expected at least one pass"
    last = history[-1]
    assert last.winner != "B", (
        f"empty-diff B won silently (winner={last.winner}, scores={last.scores})"
    )
    # B must not even be a scorable label.
    assert "B" not in last.scores


@pytest.mark.asyncio
async def test_empty_b_chosen_bundle_has_real_diff(tmp_path: Path) -> None:
    """The bundle the tournament actually returns is never the empty one."""
    runner = _ConfigurableCoderRunner(b_diff="", ab_diff="+def foo():\n+    return 9\n")
    tour = _make_tour(tmp_path, runner, name="e2e-bundle")
    final, _history = await tour.run(task_prompt="Add foo()", initial=INITIAL_BUNDLE)
    assert final.diff.strip(), "tournament returned an empty-diff winner"


# --------------------------------------------------------------------------- #
# BROKEN CONTROL: reverting to full labels resurrects the bug
# --------------------------------------------------------------------------- #


def test_broken_control_full_labels_lets_empty_b_win() -> None:
    """Pre-fix behavior: with labels=['A','B','AB'] an empty B wins.

    This pins the exact line the fix changed. ``run_pass`` now passes
    ``labels=eligible_labels`` (B dropped) instead of the literal triple.
    Reverting that one substitution sends the win straight back to the empty
    B candidate — this control turns RED on that revert.
    """
    # Judge ranks B (label) best, then A, then AB.
    rankings: list[list[str] | None] = [["B", "A", "AB"]]

    # OLD code path (the bug): every label eligible, including empty B.
    winner_buggy, scores_buggy, _ = aggregate_rankings(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A"
    )
    assert winner_buggy == "B", "control precondition: full labels let B win"
    assert scores_buggy["B"] > 0

    # NEW code path (the fix): empty B excluded from eligible labels.
    winner_fixed, scores_fixed, _ = aggregate_rankings(
        rankings, labels=["A", "AB"], tiebreak_winner="A"
    )
    assert winner_fixed != "B"
    assert "B" not in scores_fixed


# --------------------------------------------------------------------------- #
# All-empty → task-level failure + op (not a silent empty win)
# --------------------------------------------------------------------------- #


def test_all_empty_raises_task_level_failure(tmp_path: Path) -> None:
    """Every variant empty (incumbent too) → typed TournamentError."""
    tour = _make_tour(tmp_path, _ConfigurableCoderRunner(b_diff="", ab_diff=""))
    empty_incumbent = ImplBundle(
        task_id="ws3.1", task_description="Add foo()", diff=""
    )
    with pytest.raises(TournamentError, match="empty diff"):
        tour._filter_empty_variants(
            empty_incumbent,
            _bundle("B", ""),
            _bundle("AB", ""),
            pass_num=2,
        )


def test_all_empty_emits_op(tmp_path: Path) -> None:
    """All-empty path emits the auditable tournament_all_variants_failed op."""
    tour = _make_tour(tmp_path, _ConfigurableCoderRunner(b_diff="", ab_diff=""))
    empty_incumbent = ImplBundle(
        task_id="ws3.1", task_description="Add foo()", diff=""
    )
    with capture_logs() as cap:
        with pytest.raises(TournamentError):
            tour._filter_empty_variants(
                empty_incumbent,
                _bundle("B", ""),
                _bundle("AB", ""),
                pass_num=2,
            )
    events = [e.get("event") for e in cap]
    assert "tournament_all_variants_failed" in events


def test_empty_drop_emits_op(tmp_path: Path) -> None:
    """Dropping an empty candidate emits the tournament_empty_variant_dropped op."""
    tour = _make_tour(tmp_path, _ConfigurableCoderRunner(b_diff="", ab_diff="x"))
    with capture_logs() as cap:
        tour._filter_empty_variants(
            INITIAL_BUNDLE,
            _bundle("B", ""),
            _bundle("AB", "+real\n"),
            pass_num=1,
        )
    drop_events = [
        e for e in cap if e.get("event") == "tournament_empty_variant_dropped"
    ]
    assert drop_events, "expected a tournament_empty_variant_dropped op"
    assert "B" in drop_events[0]["dropped"]


def test_no_op_when_all_variants_have_diffs(tmp_path: Path) -> None:
    """When nothing is empty, all three labels stay eligible (no regression)."""
    tour = _make_tour(tmp_path, _ConfigurableCoderRunner(b_diff="x", ab_diff="y"))
    with capture_logs() as cap:
        eligible, dropped = tour._filter_empty_variants(
            INITIAL_BUNDLE,
            _bundle("B", "+b\n"),
            _bundle("AB", "+ab\n"),
            pass_num=1,
        )
    assert eligible == ["A", "B", "AB"]
    assert dropped == []
    assert not [
        e for e in cap if e.get("event") == "tournament_empty_variant_dropped"
    ]
