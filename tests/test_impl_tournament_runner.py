"""Tests for :mod:`orchestrator.impl_tournament_runner`.

Covers:
  - ``_resolve_tournament_model`` from registry vs config vs neither
  - ``run_impl_tournament`` full-flow with StubAdapter (evidence + ledger)
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.impl_tournament_runner import (
    _resolve_tournament_model,
    run_impl_tournament,
)
from state.schemas import Phase, Plan, Task
from tournament import ImplBundle

from stub_adapter import StubAdapter


# ── Helpers ────────────────────────────────────────────────────────────


def _git_init(path: Path) -> None:
    """Initialize a minimal git repo at *path* with one commit."""
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), check=True, capture_output=True,
    )
    (path / "README.md").write_text("init")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=str(path), check=True, capture_output=True,
    )


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


INITIAL_BUNDLE = ImplBundle(
    task_id="1.1",
    task_description="Add foo()",
    diff="+def foo(): pass",
    files_changed=["foo.py"],
    tests_passed=3,
    tests_failed=0,
    tests_total=3,
    test_output_excerpt="3 passed",
)


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-runner-test",
        spec_hash="h",
        phases=[
            Phase(
                id="1",
                title="Work",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Add foo",
                        description="Implement foo()",
                    )
                ],
            )
        ],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _orch_with_judge(
    cwd: Path,
    adapter: StubAdapter,
    *,
    judge_model: str | None = "sonnet",
    auto_disable: list[str] | None = None,
    config_model: str | None = None,
    clear_registry_model: bool = False,
) -> Orchestrator:
    """Build an Orchestrator configured for tournament tests.

    If *clear_registry_model* is True the judge AgentSpec in the registry
    will have its model set to ``None`` (simulating a registry entry
    without an explicit model override).
    """
    cfg = default_config()
    cfg.tournaments.impl.enabled = True
    cfg.tournaments.impl.num_judges = 1
    cfg.tournaments.impl.convergence_k = 1
    cfg.tournaments.impl.max_rounds = 1
    cfg.tournaments.plan.enabled = False
    if auto_disable is not None:
        cfg.tournaments.auto_disable_for_models = auto_disable

    if judge_model is not None:
        cfg.agents["judge"].model = judge_model
    if config_model is not None:
        cfg.agents["judge"].model = config_model

    registry = build_registry(cfg)

    if clear_registry_model:
        # Wipe model from the registry-level spec so only config fallback fires.
        registry["judge"] = registry["judge"].model_copy(update={"model": None})

    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-runner-test",
    )


# ── _resolve_tournament_model tests ────────────────────────────────────


def test_resolve_tournament_model_from_registry(tmp_path: Path) -> None:
    """When the registry's judge spec has a model, that model is returned."""
    adapter = StubAdapter({})
    orch = _orch_with_judge(tmp_path, adapter, judge_model="gpt-4o-mini")
    result = _resolve_tournament_model(orch)
    assert result == "gpt-4o-mini"


def test_resolve_tournament_model_from_config(tmp_path: Path) -> None:
    """When registry model is None, falls back to config agent model."""
    adapter = StubAdapter({})
    orch = _orch_with_judge(
        tmp_path,
        adapter,
        judge_model="from-config-model",
        clear_registry_model=True,
    )
    # With clear_registry_model=True, registry["judge"].model is None.
    # The function falls through to cfg.agents["judge"].model.
    result = _resolve_tournament_model(orch)
    assert result == "from-config-model"


def test_resolve_tournament_model_none(tmp_path: Path) -> None:
    """When neither registry nor config has a model, returns None."""
    adapter = StubAdapter({})
    orch = _orch_with_judge(
        tmp_path,
        adapter,
        judge_model=None,
        clear_registry_model=True,
    )
    # default_config() sets judge model to "sonnet" via resolve_model();
    # explicitly clear it so neither registry nor config provides a model.
    orch.cfg.agents["judge"].model = None
    result = _resolve_tournament_model(orch)
    assert result is None


# ── Full-flow run_impl_tournament tests ────────────────────────────────


def _tournament_adapter() -> StubAdapter:
    """Build a StubAdapter with handlers for all tournament roles.

    The judge always returns RANKING: 1, 2, 3 which means the first
    slot wins. Because randomize_for_judge shuffles presentation order,
    the exact winner depends on RNG — but the tournament will complete
    and produce evidence regardless.
    """

    def _handler(inv):
        role = inv.role
        if role == "developer":
            return AgentResult(
                success=True,
                text="implemented variant",
                diff="diff --git a/foo.py b/foo.py\n+def foo(): return 42",
                files_changed=[Path("foo.py")],
                duration_s=0.1,
            )
        if role == "test_engineer":
            return AgentResult(
                success=True,
                text="ran tests\nRESULTS: passed=3 failed=0 total=3",
                duration_s=0.1,
            )
        if role == "critic_t":
            return AgentResult(
                success=True,
                text="Critic: looks fine",
                duration_s=0.01,
            )
        if role == "architect_b":
            return AgentResult(
                success=True,
                text="- minor fix",
                duration_s=0.01,
            )
        if role == "synthesizer":
            return AgentResult(
                success=True,
                text="- synthesize both",
                duration_s=0.01,
            )
        if role == "judge":
            return AgentResult(
                success=True,
                text="Good work.\n\nRANKING: 1, 2, 3",
                duration_s=0.01,
            )
        # Default for any other role.
        return AgentResult(success=True, text=f"[stub:{role}]", duration_s=0.01)

    return StubAdapter(
        {
            "developer": _handler,
            "test_engineer": _handler,
            "critic_t": _handler,
            "architect_b": _handler,
            "synthesizer": _handler,
            "judge": _handler,
        }
    )


@pytest.mark.asyncio
async def test_run_impl_tournament_full_flow(tmp_path: Path) -> None:
    """Full tournament flow: StubAdapter handles all roles, evidence is written."""
    _git_init(tmp_path)
    adapter = _tournament_adapter()
    orch = _orch_with_judge(
        tmp_path,
        adapter,
        judge_model="sonnet",
        auto_disable=[],
    )
    await orch.plan_manager.init_plan(_mk_plan())
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    result = await run_impl_tournament(orch, task, INITIAL_BUNDLE)

    # Result is an ImplBundle (possibly different from initial).
    assert isinstance(result, ImplBundle)
    assert result.task_id == "1.1"

    # TournamentEvidence was written.
    ev_path = tmp_path / ".autodev" / "evidence" / "1.1-tournament.json"
    assert ev_path.exists()

    import json

    ev_data = json.loads(ev_path.read_text())
    assert ev_data["phase"] == "impl"
    assert ev_data["task_id"] == "1.1"
    assert ev_data["passes"] >= 1
    assert ev_data["winner"] in ("A", "B", "AB")

    # At least the critic_t, architect_b, synthesizer, judge roles were called.
    assert adapter.count("critic_t") >= 1
    assert adapter.count("architect_b") >= 1
    assert adapter.count("judge") >= 1


@pytest.mark.asyncio
async def test_run_impl_tournament_writes_ledger_breadcrumb(tmp_path: Path) -> None:
    """After tournament completes, an impl_tournament_complete ledger entry exists."""
    _git_init(tmp_path)
    adapter = _tournament_adapter()
    orch = _orch_with_judge(
        tmp_path,
        adapter,
        judge_model="sonnet",
        auto_disable=[],
    )
    await orch.plan_manager.init_plan(_mk_plan())
    task = await orch.plan_manager.get_task("1.1")
    assert task is not None

    await run_impl_tournament(orch, task, INITIAL_BUNDLE)

    ledger = await orch.plan_manager.read_ledger()
    impl_entries = [e for e in ledger if e.op == "impl_tournament_complete"]
    assert len(impl_entries) >= 1

    entry = impl_entries[-1]
    assert entry.payload["task_id"] == "1.1"
    assert "tournament_id" in entry.payload
    assert "passes" in entry.payload
    assert "winner_last" in entry.payload
