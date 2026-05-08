"""Tests for v0.13.0 lazy repo probe on the :class:`Orchestrator`.

The orchestrator probes repo size once on the first call to ``plan()``
or ``execute()`` and caches the result on ``self._repo_capacity`` so
subsequent calls reuse the snapshot. Tests assert:

* The probe is *not* called during ``__init__`` (lazy contract).
* The first ``plan()``/``execute()`` call triggers a probe.
* Subsequent calls reuse the cached capacity (no re-probe).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from runtime.repo_probe import RepoCapacity
from state.schemas import Plan

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _make_orch(cwd: Path) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.phase_review.enabled = False
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    registry = build_registry(cfg)
    adapter = StubAdapter({"explorer": ok("ok")})
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-repo-probe",
    )


def test_orchestrator_init_does_not_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``__init__`` does not probe — repo capacity is None until first call."""
    from runtime import repo_probe

    calls: list[Path] = []

    def fake_probe(cwd: Path) -> RepoCapacity:
        calls.append(cwd)
        return RepoCapacity(
            file_count=100, total_bytes=1000, depth_max=2, is_huge=False
        )

    monkeypatch.setattr(repo_probe, "probe_repo", fake_probe)
    # Also patch the orchestrator's import-site reference (in case it's
    # bound at import time rather than per-call).
    import orchestrator as orch_mod

    if hasattr(orch_mod, "probe_repo"):
        monkeypatch.setattr(orch_mod, "probe_repo", fake_probe)

    orch = _make_orch(tmp_path)
    assert orch._repo_capacity is None
    assert calls == []


@pytest.mark.asyncio
async def test_orchestrator_probes_repo_lazily_on_first_plan_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First ``plan()`` call triggers a probe and caches the result."""
    from runtime import repo_probe

    calls: list[Path] = []
    fake_cap = RepoCapacity(
        file_count=12_345, total_bytes=99_999, depth_max=3, is_huge=False
    )

    def fake_probe(cwd: Path) -> RepoCapacity:
        calls.append(cwd)
        return fake_cap

    monkeypatch.setattr(repo_probe, "probe_repo", fake_probe)
    import orchestrator as orch_mod

    if hasattr(orch_mod, "probe_repo"):
        monkeypatch.setattr(orch_mod, "probe_repo", fake_probe)

    orch = _make_orch(tmp_path)
    assert orch._repo_capacity is None

    # Patch run_plan_phase so plan() returns immediately without doing
    # real planning work.
    async def fake_run_plan_phase(
        orch_arg: Orchestrator, intent: str
    ) -> Plan:
        # Trigger the probe via the orchestrator's accessor before returning.
        _ = orch_arg.repo_capacity
        return Plan(
            plan_id="p-test",
            spec_hash="0123456789abcdef",
            phases=[],
            created_at=_iso(),
            updated_at=_iso(),
            complexity="medium",
        )

    from orchestrator import plan_phase as pp

    monkeypatch.setattr(pp, "run_plan_phase", fake_run_plan_phase)

    plan = await orch.plan("test intent")
    assert plan.plan_id == "p-test"
    # The probe ran exactly once.
    assert len(calls) == 1
    # The capacity is cached on the orchestrator.
    assert orch._repo_capacity is fake_cap


@pytest.mark.asyncio
async def test_orchestrator_caches_repo_capacity_across_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multiple ``plan()`` / ``execute()`` calls share one probe result."""
    from runtime import repo_probe

    calls: list[Path] = []
    fake_cap = RepoCapacity(
        file_count=12_345, total_bytes=99_999, depth_max=3, is_huge=False
    )

    def fake_probe(cwd: Path) -> RepoCapacity:
        calls.append(cwd)
        return fake_cap

    monkeypatch.setattr(repo_probe, "probe_repo", fake_probe)
    import orchestrator as orch_mod

    if hasattr(orch_mod, "probe_repo"):
        monkeypatch.setattr(orch_mod, "probe_repo", fake_probe)

    async def fake_run_plan_phase(
        orch_arg: Orchestrator, intent: str
    ) -> Plan:
        _ = orch_arg.repo_capacity
        return Plan(
            plan_id="p-test",
            spec_hash="0123456789abcdef",
            phases=[],
            created_at=_iso(),
            updated_at=_iso(),
            complexity="medium",
        )

    async def fake_run_execute_phase(
        orch_arg: Orchestrator, task_id: str | None
    ) -> list[Any]:
        _ = orch_arg.repo_capacity
        return []

    from orchestrator import execute_phase as ep
    from orchestrator import plan_phase as pp

    monkeypatch.setattr(pp, "run_plan_phase", fake_run_plan_phase)
    monkeypatch.setattr(ep, "run_execute_phase", fake_run_execute_phase)

    orch = _make_orch(tmp_path)
    await orch.plan("intent 1")
    await orch.plan("intent 2")
    await orch.execute(task_id=None)

    assert len(calls) == 1
    assert orch._repo_capacity is fake_cap
