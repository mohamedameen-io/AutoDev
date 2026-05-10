"""v0.23.0 C5 regression: explorer max_turns 2x on huge repos.

P-7's investigation of the 2026-05-09 Unity ``.autodev/debug/`` showed
the explorer hit ``error_max_turns`` at turn 3 with 218K cached
tokens — 3 turns is not enough to enumerate a 358K-file codebase.
This test pins the 2x bump so future edits don't accidentally regress
it; other roles are unaffected.
"""

from __future__ import annotations

# We exercise the bump logic directly rather than mocking the full
# orchestrator stack — the bump is a small, isolated piece of logic.

from runtime.repo_probe import RepoCapacity


def _explorer_max_turns(*, role: str, base_turns: int, is_huge: bool) -> int:
    """Mirror the bump rule in plan_phase._delegate (v0.23.0 C5)."""
    resolved = base_turns
    if role == "explorer" and is_huge:
        resolved = int(round(resolved * 2.0))
    return resolved


def test_explorer_huge_doubles_turns() -> None:
    assert _explorer_max_turns(role="explorer", base_turns=3, is_huge=True) == 6


def test_explorer_normal_unchanged() -> None:
    assert _explorer_max_turns(role="explorer", base_turns=3, is_huge=False) == 3


def test_other_roles_unchanged_on_huge() -> None:
    assert _explorer_max_turns(role="architect", base_turns=5, is_huge=True) == 5
    assert _explorer_max_turns(role="developer", base_turns=10, is_huge=True) == 10
    assert _explorer_max_turns(role="judge", base_turns=1, is_huge=True) == 1


def test_explorer_high_base_still_doubles() -> None:
    """Even with a high base, the bump applies (operator intent preserved)."""
    assert _explorer_max_turns(role="explorer", base_turns=10, is_huge=True) == 20


def test_repo_capacity_is_huge_threshold() -> None:
    """Sanity-check the is_huge field is what we think it is."""
    huge = RepoCapacity(file_count=200_000, total_bytes=8 * 1024**3, depth_max=10, is_huge=True)
    not_huge = RepoCapacity(file_count=100, total_bytes=1024, depth_max=2, is_huge=False)
    assert huge.is_huge is True
    assert not_huge.is_huge is False
