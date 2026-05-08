"""v0.14.0 ``BranchConfig`` + ``TournamentPhaseConfig.branches`` tests.

Covers:

* :class:`BranchConfig` defaults (no overrides, lane=local-tweak,
  risk=medium, family=None).
* lane Literal validation (rejects bogus lanes).
* risk Literal validation (rejects bogus risk levels).
* model_overrides keys/values must be non-empty strings.
* :class:`TournamentPhaseConfig.branches` validation:
    - mutually exclusive with ``num_branches > 1``;
    - clamped to [1, 5];
    - non-empty list when set.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config.schema import BranchConfig, TournamentPhaseConfig


# ---------------------------------------------------------------------------
# BranchConfig defaults + validation
# ---------------------------------------------------------------------------


def test_branch_config_defaults() -> None:
    """An empty BranchConfig() validates with safe defaults."""
    bc = BranchConfig()
    assert bc.model_overrides == {}
    assert bc.lane == "local-tweak"
    assert bc.risk == "medium"
    assert bc.family is None


def test_branch_config_accepts_all_lane_values() -> None:
    """Every documented lane Literal is accepted."""
    for lane in (
        "distant-scout",
        "local-tweak",
        "architectural",
        "constraint-removal",
        "incumbent-confirmation",
    ):
        bc = BranchConfig(lane=lane)  # type: ignore[arg-type]
        assert bc.lane == lane


def test_branch_config_rejects_invalid_lane() -> None:
    """A lane outside the Literal raises ValidationError."""
    with pytest.raises(ValidationError):
        BranchConfig(lane="not-a-lane")  # type: ignore[arg-type]


def test_branch_config_rejects_invalid_risk() -> None:
    """A risk outside {low, medium, high} raises ValidationError."""
    with pytest.raises(ValidationError):
        BranchConfig(risk="extreme")  # type: ignore[arg-type]


def test_branch_config_model_overrides_rejects_empty_string() -> None:
    """An empty string in model_overrides values raises — likely a typo."""
    with pytest.raises(ValidationError):
        BranchConfig(model_overrides={"developer": ""})


def test_branch_config_model_overrides_accepts_role_to_model_map() -> None:
    """A real role -> model map round-trips."""
    bc = BranchConfig(
        model_overrides={
            "developer": "claude-sonnet-4-5",
            "judge": "claude-haiku-4-5",
        },
        lane="distant-scout",
        risk="high",
        family="exploration-cohort-1",
    )
    assert bc.model_overrides == {
        "developer": "claude-sonnet-4-5",
        "judge": "claude-haiku-4-5",
    }
    assert bc.lane == "distant-scout"
    assert bc.risk == "high"
    assert bc.family == "exploration-cohort-1"


def test_branch_config_extra_fields_forbidden() -> None:
    """``extra='forbid'`` config rejects unknown fields."""
    with pytest.raises(ValidationError):
        BranchConfig(unknown_field=123)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# TournamentPhaseConfig.branches integration
# ---------------------------------------------------------------------------


def _make_tpc(
    *,
    branches: list[BranchConfig] | None = None,
    num_branches: int = 1,
) -> TournamentPhaseConfig:
    return TournamentPhaseConfig(
        enabled=True,
        num_judges=3,
        convergence_k=1,
        max_rounds=2,
        num_branches=num_branches,
        branches=branches,
    )


def test_tournament_phase_config_branches_defaults_to_none() -> None:
    """branches defaults to None — legacy v0.12.0 homogeneous behavior."""
    tpc = _make_tpc()
    assert tpc.branches is None


def test_tournament_phase_config_branches_accepts_list() -> None:
    """A populated branches list flows through and is preserved."""
    bs = [
        BranchConfig(lane="distant-scout"),
        BranchConfig(lane="local-tweak"),
        BranchConfig(lane="architectural"),
    ]
    tpc = _make_tpc(branches=bs, num_branches=1)
    assert tpc.branches is not None
    assert len(tpc.branches) == 3
    assert tpc.branches[0].lane == "distant-scout"


def test_tournament_phase_config_branches_mutually_exclusive_with_num_branches() -> None:
    """Setting BOTH ``num_branches > 1`` AND a non-None ``branches`` list
    raises — the two are mutually exclusive paths to multi-branch."""
    bs = [BranchConfig(), BranchConfig()]
    with pytest.raises(ValidationError):
        _make_tpc(branches=bs, num_branches=3)


def test_tournament_phase_config_branches_with_num_branches_1_is_ok() -> None:
    """num_branches=1 (default) + branches=list is fine: branches list
    drives fan-out, num_branches stays at the legacy default."""
    bs = [BranchConfig(), BranchConfig()]
    tpc = _make_tpc(branches=bs, num_branches=1)
    assert tpc.branches is not None
    assert len(tpc.branches) == 2


def test_tournament_phase_config_branches_clamps_to_5() -> None:
    """A branches list with > 5 entries is rejected."""
    bs = [BranchConfig() for _ in range(6)]
    with pytest.raises(ValidationError):
        _make_tpc(branches=bs)


def test_tournament_phase_config_branches_empty_list_rejected() -> None:
    """An empty branches list is rejected (use None to disable, not [])."""
    with pytest.raises(ValidationError):
        _make_tpc(branches=[])
