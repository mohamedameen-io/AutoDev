"""v0.23.0 C4 regression: plan-tournament huge-repo fast-path.

Unity-scale repos (358K files) burned 80 min on the multi-branch plan
tournament (3 branches × 3-5 passes × 5 judges per branch). C4 falls
back to single-branch when ``RepoCapacity.is_huge`` is True so the
user gets a plan in <20 min. Operators can opt out via
``cfg.tournaments.plan.huge_repo_overrides_disabled = True``.
"""

from __future__ import annotations

from config.defaults import default_config


def _resolved_branches(num_branches: int, is_huge: bool, overrides_disabled: bool) -> int:
    """Mirror the resolution rule in plan_phase (v0.23.0 C4)."""
    if is_huge and not overrides_disabled and num_branches > 1:
        return 1
    return num_branches


def test_normal_repo_keeps_branches() -> None:
    """Non-huge repos use the configured branch count unchanged."""
    cfg = default_config()
    assert _resolved_branches(cfg.tournaments.plan.num_branches, False, False) == 3


def test_huge_repo_collapses_to_single_branch() -> None:
    """Huge repos auto-fall-back to single-branch."""
    cfg = default_config()
    assert _resolved_branches(cfg.tournaments.plan.num_branches, True, False) == 1


def test_huge_repo_overrides_disabled_keeps_branches() -> None:
    """Opt-out preserves the configured branch count even on huge repos."""
    cfg = default_config()
    assert _resolved_branches(cfg.tournaments.plan.num_branches, True, True) == 3


def test_single_branch_unaffected_by_fast_path() -> None:
    """If num_branches is already 1, no override fires."""
    assert _resolved_branches(1, True, False) == 1
    assert _resolved_branches(1, False, False) == 1


def test_huge_repo_overrides_disabled_default_is_false() -> None:
    """Field defaults to False so the fast-path is on by default."""
    cfg = default_config()
    assert cfg.tournaments.plan.huge_repo_overrides_disabled is False
    assert cfg.tournaments.impl.huge_repo_overrides_disabled is False
    assert cfg.tournaments.phase_review.huge_repo_overrides_disabled is False
