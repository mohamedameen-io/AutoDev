"""v0.12.0 multi-branch resume tests.

Validates :func:`walk_multi_branch_resume` and
:func:`latest_incumbent_md_across_branches` against on-disk artifact
layouts. Each branch is its own complete TournamentArtifactStore
lifecycle, so the helper just enumerates the ``branch-N/`` subdirs and
delegates per-branch resume to the existing ``read_resume_state``.

Scenarios covered:

- Empty parent dir → empty list / None.
- Parent dir without any branch- subdirs → empty list / None.
- Branch 0 fully complete, branch 1 mid-pass, branch 2 not started.
- Salvage walker: scattered incumbents across 3 branches → picks highest
  pass number; tie → lowest branch_index wins.
"""

from __future__ import annotations

import json
from pathlib import Path

from tournament.state import (
    TournamentArtifactStore,
    latest_incumbent_md_across_branches,
    walk_multi_branch_resume,
)


def _setup_branch_dir(parent: Path, branch_idx: int) -> TournamentArtifactStore:
    """Create ``parent/branch-{idx}/`` and return a store rooted there."""
    branch_dir = parent / f"branch-{branch_idx}"
    branch_dir.mkdir(parents=True, exist_ok=True)
    return TournamentArtifactStore(branch_dir)


def _write_pass_result(store: TournamentArtifactStore, pass_num: int, winner: str) -> None:
    """Helper: write a minimal valid pass_NN/result.json for testing."""
    pdir = store.pass_dir(pass_num)
    result = {
        "pass_num": pass_num,
        "winner": winner,
        "scores": {"A": 0, "B": 0, "AB": 0},
        "valid_judges": 0,
        "elapsed_s": 0.0,
        "judge_details": [],
        "incumbent_hash_before": "x",
        "incumbent_hash_after": "y",
        "meta": {},
    }
    (pdir / "result.json").write_text(json.dumps(result), encoding="utf-8")


# ---------------------------------------------------------------------------
# walk_multi_branch_resume basic enumeration
# ---------------------------------------------------------------------------


def test_walk_empty_parent_returns_empty_list(tmp_path: Path) -> None:
    """Missing parent dir → empty list."""
    result = walk_multi_branch_resume(tmp_path / "missing")
    assert result == []


def test_walk_parent_with_no_branches_returns_empty_list(tmp_path: Path) -> None:
    """A directory with no ``branch-N/`` subdirs → empty list."""
    parent = tmp_path / "multi"
    parent.mkdir()
    # Add an unrelated subdir to verify it's filtered out.
    (parent / "meta-merge").mkdir()
    result = walk_multi_branch_resume(parent)
    assert result == []


def test_walk_returns_per_branch_resume_state(tmp_path: Path) -> None:
    """3 branches with different progress: 0 fully done, 1 mid-pass, 2 empty."""
    parent = tmp_path / "multi"
    parent.mkdir()

    # Branch 0: fully complete (final_output.md present).
    s0 = _setup_branch_dir(parent, 0)
    s0.write_initial("# Plan: branch-0-initial\n")
    s0.write_final("# Plan: branch-0-final\n", history=[])

    # Branch 1: mid-pass (initial_a.md present, no result.json yet).
    s1 = _setup_branch_dir(parent, 1)
    s1.write_initial("# Plan: branch-1-initial\n")
    s1.write_version_a(pass_num=1, version_a_md="# Plan: branch-1-pass1\n")

    # Branch 2: empty (dir created but no artifacts).
    _setup_branch_dir(parent, 2)

    result = walk_multi_branch_resume(parent)
    assert len(result) == 3
    indices = [pair[0] for pair in result]
    assert indices == [0, 1, 2]

    # Branch 0 → completed
    rs0 = result[0][1]
    assert rs0 is not None
    assert rs0.completed is True
    assert "branch-0-final" in (rs0.final_md or "")

    # Branch 1 → not completed; partial state present
    rs1 = result[1][1]
    assert rs1 is not None
    assert rs1.completed is False
    # version_a.md present but no result.json → partial
    if rs1.partial is not None:
        assert rs1.partial.version_a_md is not None

    # Branch 2 → no resumable state
    rs2 = result[2][1]
    assert rs2 is None


def test_walk_ignores_non_branch_subdirs(tmp_path: Path) -> None:
    """``meta-merge/`` and other non-``branch-N/`` subdirs are ignored."""
    parent = tmp_path / "multi"
    parent.mkdir()
    _setup_branch_dir(parent, 0)
    (parent / "meta-merge").mkdir()
    (parent / "step-0").mkdir()  # spurious
    (parent / "branch-bad").mkdir()  # malformed name (non-int suffix)

    result = walk_multi_branch_resume(parent)
    indices = [pair[0] for pair in result]
    assert indices == [0]


# ---------------------------------------------------------------------------
# latest_incumbent_md_across_branches: salvage walker
# ---------------------------------------------------------------------------


def test_latest_incumbent_returns_none_for_missing_dir(tmp_path: Path) -> None:
    """Missing parent dir → None."""
    assert latest_incumbent_md_across_branches(tmp_path / "missing") is None


def test_latest_incumbent_returns_none_when_no_branches_have_incumbents(
    tmp_path: Path,
) -> None:
    """Branches exist but none have ``incumbent_after_*.md`` → None.

    The helper deliberately does NOT fall back to ``initial_a.md`` — the
    caller wants refinement, not the unrefined draft.
    """
    parent = tmp_path / "multi"
    parent.mkdir()
    s0 = _setup_branch_dir(parent, 0)
    s0.write_initial("# Plan: just-initial\n")  # no incumbent_after files
    assert latest_incumbent_md_across_branches(parent) is None


def test_latest_incumbent_picks_highest_pass_across_branches(tmp_path: Path) -> None:
    """3 branches with scattered incumbents → picks highest pass num.

    Branch 0: incumbent_after_02.md
    Branch 1: incumbent_after_05.md (winner)
    Branch 2: incumbent_after_03.md
    """
    parent = tmp_path / "multi"
    parent.mkdir()
    s0 = _setup_branch_dir(parent, 0)
    s0.write_incumbent_after(pass_num=2, a_md="# branch-0 pass 2\n")
    s1 = _setup_branch_dir(parent, 1)
    s1.write_incumbent_after(pass_num=5, a_md="# branch-1 pass 5\n")
    s2 = _setup_branch_dir(parent, 2)
    s2.write_incumbent_after(pass_num=3, a_md="# branch-2 pass 3\n")

    result = latest_incumbent_md_across_branches(parent)
    assert result is not None
    md, branch_idx, pass_num = result
    assert pass_num == 5
    assert branch_idx == 1
    assert md == "# branch-1 pass 5\n"


def test_latest_incumbent_tiebreak_lowest_branch_index(tmp_path: Path) -> None:
    """Two branches have the same top pass number → lowest branch_index wins."""
    parent = tmp_path / "multi"
    parent.mkdir()
    s0 = _setup_branch_dir(parent, 0)
    s0.write_incumbent_after(pass_num=4, a_md="# branch-0 pass 4\n")
    s1 = _setup_branch_dir(parent, 1)
    s1.write_incumbent_after(pass_num=4, a_md="# branch-1 pass 4\n")
    s2 = _setup_branch_dir(parent, 2)
    s2.write_incumbent_after(pass_num=4, a_md="# branch-2 pass 4\n")

    result = latest_incumbent_md_across_branches(parent)
    assert result is not None
    md, branch_idx, pass_num = result
    assert pass_num == 4
    assert branch_idx == 0
    assert md == "# branch-0 pass 4\n"


def test_latest_incumbent_skips_branch_with_no_incumbents(tmp_path: Path) -> None:
    """A branch with only initial_a.md (no incumbent_after_*) is skipped."""
    parent = tmp_path / "multi"
    parent.mkdir()
    s0 = _setup_branch_dir(parent, 0)
    s0.write_initial("# init only\n")  # no incumbent_after
    s1 = _setup_branch_dir(parent, 1)
    s1.write_incumbent_after(pass_num=2, a_md="# branch-1 pass 2\n")

    result = latest_incumbent_md_across_branches(parent)
    assert result is not None
    _, branch_idx, pass_num = result
    assert branch_idx == 1
    assert pass_num == 2


# ---------------------------------------------------------------------------
# Resume walker handles deleted branch dirs gracefully
# ---------------------------------------------------------------------------


def test_walk_handles_deleted_branch_dir(tmp_path: Path) -> None:
    """If a branch dir exists in the index but was deleted between runs,
    the walker simply omits it (returns the survivors). No crash."""
    parent = tmp_path / "multi"
    parent.mkdir()
    s0 = _setup_branch_dir(parent, 0)
    s0.write_initial("# 0\n")
    s2 = _setup_branch_dir(parent, 2)
    s2.write_initial("# 2\n")
    # Note: branch-1 is missing entirely (not just empty, doesn't exist).

    result = walk_multi_branch_resume(parent)
    indices = [pair[0] for pair in result]
    # Only branches 0 and 2 surface; branch 1 is naturally absent.
    assert indices == [0, 2]


# ---------------------------------------------------------------------------
# v0.12.0 commit 12 — focused edge tests for latest_incumbent_md_across_branches
# ---------------------------------------------------------------------------


def test_latest_incumbent_signature_returns_three_tuple(tmp_path: Path) -> None:
    """Helper returns ``(md, branch_index, pass_num)`` shape (or None).

    Sanity check the signature contract — callers in plan_phase rely
    on the 3-tuple unpacking.
    """
    parent = tmp_path / "multi"
    parent.mkdir()
    s = _setup_branch_dir(parent, 0)
    s.write_incumbent_after(pass_num=1, a_md="# A\n")
    result = latest_incumbent_md_across_branches(parent)
    assert result is not None
    md, idx, pass_num = result
    assert isinstance(md, str)
    assert isinstance(idx, int)
    assert isinstance(pass_num, int)


def test_latest_incumbent_skips_pass_num_zero(tmp_path: Path) -> None:
    """``incumbent_after_00.md`` shouldn't normally exist (pass 1 is the
    first pass), but if it does, the regex/scan still picks higher passes
    over it."""
    parent = tmp_path / "multi"
    parent.mkdir()
    s0 = _setup_branch_dir(parent, 0)
    # Synthesize a pass-0 file (degenerate but valid filename).
    (s0.artifact_dir / "incumbent_after_00.md").write_text(
        "# pass 0 oddity\n", encoding="utf-8"
    )
    s0.write_incumbent_after(pass_num=2, a_md="# pass 2\n")
    result = latest_incumbent_md_across_branches(parent)
    assert result is not None
    _, _, pass_num = result
    assert pass_num == 2


def test_latest_incumbent_high_pass_numbers(tmp_path: Path) -> None:
    """Pass numbers like 100+ work correctly (no fixed-width assumptions)."""
    parent = tmp_path / "multi"
    parent.mkdir()
    s0 = _setup_branch_dir(parent, 0)
    # write_incumbent_after uses ``f"{pass_num:02d}"`` formatter; for
    # large numbers the file name becomes incumbent_after_100.md.
    s0.write_incumbent_after(pass_num=100, a_md="# big\n")
    result = latest_incumbent_md_across_branches(parent)
    assert result is not None
    _, _, pass_num = result
    assert pass_num == 100


def test_latest_incumbent_corrupt_file_skipped_gracefully(tmp_path: Path) -> None:
    """An unreadable ``incumbent_after_NN.md`` is skipped (not a crash).

    Validates the OSError catch in
    :func:`latest_incumbent_md_across_branches` — important because
    crashes in salvage paths must NEVER mask the underlying tournament
    error from the operator.
    """
    parent = tmp_path / "multi"
    parent.mkdir()
    s0 = _setup_branch_dir(parent, 0)
    # Create a directory at the path of the file so reading fails as
    # IsADirectoryError (subclass of OSError).
    (s0.artifact_dir / "incumbent_after_05.md").mkdir()

    s1 = _setup_branch_dir(parent, 1)
    s1.write_incumbent_after(pass_num=2, a_md="# fallback\n")

    result = latest_incumbent_md_across_branches(parent)
    # The corrupt branch-0 entry was skipped; branch-1's pass-2 won.
    assert result is not None
    md, idx, pass_num = result
    assert idx == 1
    assert pass_num == 2
    assert md == "# fallback\n"
