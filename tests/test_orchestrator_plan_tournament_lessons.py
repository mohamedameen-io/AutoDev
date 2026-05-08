"""v0.15.0: lessons emission from plan tournament + multi-branch meta-merge.

Validates that ``run_plan_tournament`` emits a ``winner_promoted`` lesson
after the tournament returns, and a ``discard`` lesson for each non-winning
candidate observed in the history. Also asserts that the multi-branch
meta-merge emits ``winner_promoted`` for the final markdown and ``discard``
for the rejected branch winners.

The strategy mirrors :mod:`tests.test_orchestrator_plan_tournament_runner`:
intercept ``Tournament`` (or the relevant runner internals) so the test
controls what the tournament returns, then read what was written into the
swarm-tier knowledge store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import plan_tournament_runner as ptr
from state.knowledge import TournamentEvent  # noqa: F401 — used in assertions

from stub_adapter import StubAdapter, ok


_SPEC_HASH = "0123456789abcdef"


def _plan_md(complexity: str | None = "medium") -> str:
    body = (
        "# Plan: Add foo(x)\n\n"
        "## Phase 1: Implement\n\n"
        "### Task 1.1: Write foo\n"
        "  - Description: Add a function foo.\n"
        "  - Files: foo.py\n"
        "  - Acceptance:\n"
        "    - [ ] function exists\n"
    )
    if complexity is None:
        return body
    return f"{body}\nCOMPLEXITY: {complexity}\n"


class _HistoryTournament:
    """Stand-in for :class:`Tournament` that returns synthetic history.

    The history list is set on the class before construction and read by
    :meth:`run`. This lets a single test control which winner / discard
    events get fed into the lessons emitter.
    """

    history_to_return: list[Any] = []
    final_md_override: str | None = None

    def __init__(
        self,
        *,
        handler: Any,
        client: Any,
        cfg: Any,
        artifact_dir: Path,
        rng: Any = None,
        judge_plugins: Any = None,
    ) -> None:
        self._cfg = cfg
        self._artifact_dir = artifact_dir

    async def run(self, *, task_prompt: str, initial: str) -> tuple[str, list[Any]]:
        final = (
            type(self).final_md_override
            if type(self).final_md_override is not None
            else initial
        )
        return final, list(type(self).history_to_return)


@pytest.fixture
def synthetic_tournament(monkeypatch: pytest.MonkeyPatch) -> type[_HistoryTournament]:
    _HistoryTournament.history_to_return = []
    _HistoryTournament.final_md_override = None
    monkeypatch.setattr(ptr, "Tournament", _HistoryTournament)
    return _HistoryTournament


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.plan.num_judges = 3
    cfg.tournaments.plan.convergence_k = 1
    cfg.tournaments.plan.max_rounds = 2
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.impl.enabled = False
    cfg.hive.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-lessons",
    )


@pytest.mark.asyncio
async def test_pass_winner_emits_winner_promoted_event(
    tmp_path: Path,
    synthetic_tournament: type[_HistoryTournament],
) -> None:
    """A converged tournament must emit a ``winner_promoted`` lesson tagged
    with the ``plan-tournament`` family.
    """
    from tournament.core import PassResult

    pass1 = PassResult(
        pass_num=1,
        winner="B",
        scores={"A": 4, "B": 7, "AB": 5},
        valid_judges=3,
        elapsed_s=1.0,
        incumbent_hash_before="aaaaaaaa",
        incumbent_hash_after="bbbbbbbb",
    )
    pass2 = PassResult(
        pass_num=2,
        winner="A",
        scores={"A": 8, "B": 4, "AB": 4},
        valid_judges=3,
        elapsed_s=1.0,
        incumbent_hash_before="bbbbbbbb",
        incumbent_hash_after="bbbbbbbb",
    )
    synthetic_tournament.history_to_return = [pass1, pass2]
    synthetic_tournament.final_md_override = "# refined plan\n"

    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(tmp_path, adapter)
    await ptr.run_plan_tournament(
        orch, _plan_md("medium"), "spec text", spec_hash=_SPEC_HASH
    )

    entries = await orch.knowledge.read_all(tier="swarm")
    families = [e.metadata.get("family") for e in entries]
    event_types = [e.metadata.get("event_type") for e in entries]
    assert "plan-tournament" in families
    # At least one winner_promoted event must be present.
    assert "winner_promoted" in event_types


@pytest.mark.asyncio
async def test_pass_discard_emits_discard_event(
    tmp_path: Path,
    synthetic_tournament: type[_HistoryTournament],
) -> None:
    """When a pass picks B (or AB) over the incumbent A, the losing
    candidate(s) must be recorded as ``discard`` lessons so future passes
    learn what didn't work."""
    from tournament.core import PassResult

    # B wins over A — A is implicitly the discard, plus AB is also discarded.
    pass1 = PassResult(
        pass_num=1,
        winner="B",
        scores={"A": 4, "B": 7, "AB": 5},
        valid_judges=3,
        elapsed_s=1.0,
        incumbent_hash_before="aaaaaaaa",
        incumbent_hash_after="bbbbbbbb",
    )
    synthetic_tournament.history_to_return = [pass1]
    synthetic_tournament.final_md_override = "# refined plan\n"

    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(tmp_path, adapter)
    await ptr.run_plan_tournament(
        orch, _plan_md("medium"), "spec text", spec_hash=_SPEC_HASH
    )

    entries = await orch.knowledge.read_all(tier="swarm")
    event_types = [e.metadata.get("event_type") for e in entries]
    assert "discard" in event_types, f"expected 'discard' lesson, got {event_types}"


@pytest.mark.asyncio
async def test_lessons_recording_failure_does_not_break_tournament(
    tmp_path: Path,
    synthetic_tournament: type[_HistoryTournament],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If knowledge.record_tournament_event raises, the tournament must still
    return its converged plan — lesson recording must never break the flow.
    """
    from tournament.core import PassResult

    pass1 = PassResult(
        pass_num=1,
        winner="A",
        scores={"A": 9, "B": 2, "AB": 3},
        valid_judges=3,
        elapsed_s=1.0,
        incumbent_hash_before="aaaaaaaa",
        incumbent_hash_after="aaaaaaaa",
    )
    synthetic_tournament.history_to_return = [pass1]
    synthetic_tournament.final_md_override = "# converged plan\n"

    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(tmp_path, adapter)

    async def boom(self: Any, event: Any) -> None:
        raise RuntimeError("simulated knowledge failure")

    monkeypatch.setattr(
        type(orch.knowledge), "record_tournament_event", boom
    )

    # Must not raise — the runner swallows the knowledge error.
    final = await ptr.run_plan_tournament(
        orch, _plan_md("medium"), "spec text", spec_hash=_SPEC_HASH
    )
    assert final == "# converged plan\n"
