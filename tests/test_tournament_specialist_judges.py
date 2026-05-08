"""v0.18.0 C3: tests for specialist judge roles + per-role weighting."""

from __future__ import annotations

from pathlib import Path

import pytest

from tournament.core import Tournament, TournamentConfig
from tournament import PlanContentHandler
from tournament.voting import BordaAggregator


def test_tournament_config_default_judge_roles_none() -> None:
    cfg = TournamentConfig()
    assert cfg.judge_roles is None
    assert cfg.judge_role_weights is None


def test_tournament_config_accepts_specialist_roles() -> None:
    cfg = TournamentConfig(
        judge_roles=["critic", "reviewer", "test_engineer", "domain_expert", "explorer"],
        judge_role_weights={"test_engineer": 2.0},
    )
    assert cfg.judge_roles == [
        "critic", "reviewer", "test_engineer", "domain_expert", "explorer"
    ]
    assert cfg.judge_role_weights == {"test_engineer": 2.0}


def test_borda_aggregator_weights_default_to_one() -> None:
    """When weights=None, BordaAggregator output is byte-identical to no-weights."""
    borda = BordaAggregator()
    rankings = [["A", "B", "AB"], ["B", "A", "AB"], ["AB", "A", "B"]]
    no_weights = borda.aggregate(rankings, labels=["A", "B", "AB"], tiebreak_winner="A")
    explicit_ones = borda.aggregate(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A",
        weights=[1.0, 1.0, 1.0],
    )
    assert no_weights == explicit_ones


def test_borda_aggregator_weight_doubles_judge_contribution() -> None:
    """A 2.0 weight doubles a judge's Borda contribution."""
    borda = BordaAggregator()
    rankings = [["A", "B", "AB"], ["B", "AB", "A"]]
    # Judge 0 (weight 2.0): A=6, B=4, AB=2.
    # Judge 1 (weight 1.0): B=3, AB=2, A=1.
    # Totals: A=7, B=7, AB=4.
    winner, scores, _ = borda.aggregate(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A",
        weights=[2.0, 1.0],
    )
    assert scores["A"] == 7
    assert scores["B"] == 7
    assert scores["AB"] == 4
    # A wins by tiebreak.
    assert winner == "A"


def test_borda_aggregator_weights_break_ties() -> None:
    """Heavy weights can flip the Borda winner."""
    borda = BordaAggregator()
    rankings = [["A", "B", "AB"], ["B", "A", "AB"]]
    # Equal weights: A=4, B=4, AB=2 → A wins by tiebreak.
    # Heavy on judge 1 (3.0): A=3+6=9, B=2+9=11, AB=1+3=4 → B wins.
    winner, _scores, _ = borda.aggregate(
        rankings, labels=["A", "B", "AB"], tiebreak_winner="A",
        weights=[1.0, 3.0],
    )
    assert winner == "B"


def test_tournament_default_no_role_weights_no_change(tmp_path: Path) -> None:
    """Tournament with default config produces same Borda outcomes as v0.17.0."""

    class _NoopClient:
        async def call(self, **_kw):
            return ""

    t = Tournament(
        handler=PlanContentHandler(),
        client=_NoopClient(),
        cfg=TournamentConfig(),
        artifact_dir=tmp_path,
    )
    assert t.voting_strategy is not None
    assert t.cfg.judge_roles is None
    assert t.cfg.judge_role_weights is None


def test_tournament_with_specialist_judge_roles(tmp_path: Path) -> None:
    """Tournament accepts specialist judge_roles + weights."""

    class _NoopClient:
        async def call(self, **_kw):
            return ""

    cfg = TournamentConfig(
        num_judges=5,
        judge_roles=[
            "critic", "reviewer", "test_engineer", "domain_expert", "explorer",
        ],
        judge_role_weights={"test_engineer": 2.0},
    )
    t = Tournament(
        handler=PlanContentHandler(),
        client=_NoopClient(),
        cfg=cfg,
        artifact_dir=tmp_path,
    )
    assert t.cfg.judge_roles is not None
    assert len(t.cfg.judge_roles) == 5
    assert t.cfg.judge_role_weights == {"test_engineer": 2.0}


@pytest.mark.asyncio
async def test_run_judges_dispatches_per_role(tmp_path: Path) -> None:
    """When judge_roles is set, each judge call dispatches with its role."""
    captured_roles: list[str] = []

    class _CapturingClient:
        last_pid: int | None = None

        async def call(self, *, system: str, user: str, role: str, model: str | None = None):
            captured_roles.append(role)
            return "RANKING: 1, 2, 3"

    cfg = TournamentConfig(
        num_judges=3,
        judge_roles=["critic", "reviewer", "test_engineer"],
    )
    t = Tournament(
        handler=PlanContentHandler(),
        client=_CapturingClient(),
        cfg=cfg,
        artifact_dir=tmp_path,
    )

    await t._run_judges(
        task_prompt="prompt",
        v_a="A",
        v_b="B",
        v_ab="AB",
        model=None,
        pass_num=1,
    )

    assert captured_roles == ["critic", "reviewer", "test_engineer"]
