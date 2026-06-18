"""Gate S4: repo_probe scale signals are wired into intake (WS-SCALE-01).

repo_probe computes ``avg_file_size_bytes`` / ``depth_max`` / ``largest_dir`` but
historically NOTHING outside ``repo_probe.py`` read them — "scale-aware" was
vacuous. This suite pins the production-side wiring: the intake phase reads the
:class:`~runtime.repo_probe.RepoCapacity` snapshot (via ``orch.repo_capacity``)
and surfaces a populated ``scale_context`` dict on its output that downstream
framing consumes.

The coordinated contract (with the framing agent): the field is ``scale_context``
with at least ``{'is_large', 'depth_max', 'avg_file_size_bytes'}``. ``is_large``
is True iff ``depth_max > 8`` OR ``avg_file_size_bytes > 50_000`` (gate S4).

Engagement guarantees (a gate that passes on the found-nothing case is the bug):
- On a LARGE repo (depth_max>8 OR avg>50k) → ``is_large`` is True and the dict
  values mirror the probe (read from repo_probe, not constants).
- On a SMALL/shallow repo → ``is_large`` is False.
- BROKEN-CONTROL: a probe that reports large but is NOT read by intake leaves
  ``is_large`` False — proves the signal is actually consumed, not synthesized.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.intake_phase import IntakeOutcome, run_intake_phase
from runtime.repo_probe import RepoCapacity
from state.evidence import read_evidence
from state.schemas import IntakeEvidence
from stub_adapter import StubAdapter


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #


def _bootstrap_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True)
    (tmp_path / "main.py").write_text("def main():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(tmp_path), check=True)


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-scale-context",
    )


def _patch_capacity(
    monkeypatch: pytest.MonkeyPatch, orch: Orchestrator, cap: RepoCapacity
) -> None:
    """Force ``orch.repo_capacity`` to return *cap* (no real filesystem probe)."""
    # repo_capacity caches on first access; pre-seed the private slot so the
    # property returns our fake snapshot without touching the disk.
    orch._repo_capacity = cap


_WELL_FORMED = (
    "# Bug: crash on refresh in src/foo.py\n\n"
    "The bar() widget crashes on refresh; it must not crash.\n"
    "Expected: clean refresh. Acceptance: regression test passes.\n"
    "We cannot break backward compatibility.\n"
)


def _large_by_depth() -> RepoCapacity:
    # depth_max > 8 → is_large, even though bytes are tiny.
    return RepoCapacity(
        file_count=120,
        total_bytes=12_000,
        avg_file_size_bytes=100,
        largest_dir="src",
        largest_dir_file_count=80,
        depth_max=12,
        is_huge=False,
    )


def _large_by_avg_bytes() -> RepoCapacity:
    # avg_file_size_bytes > 50_000 → is_large, even though shallow.
    return RepoCapacity(
        file_count=300,
        total_bytes=300 * 90_000,
        avg_file_size_bytes=90_000,
        largest_dir="assets",
        largest_dir_file_count=200,
        depth_max=3,
        is_huge=False,
    )


def _small_shallow() -> RepoCapacity:
    return RepoCapacity(
        file_count=40,
        total_bytes=40_000,
        avg_file_size_bytes=1_000,
        largest_dir="src",
        largest_dir_file_count=30,
        depth_max=2,
        is_huge=False,
    )


# --------------------------------------------------------------------------- #
# scale_context shape contract
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_outcome_carries_scale_context_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The intake outcome ALWAYS carries a scale_context dict (even passthrough)."""
    _bootstrap_repo(tmp_path)
    orch = _make_orch(tmp_path, StubAdapter({}))
    _patch_capacity(monkeypatch, orch, _small_shallow())

    outcome = await run_intake_phase(orch, _WELL_FORMED)

    assert isinstance(outcome, IntakeOutcome)
    sc = outcome.scale_context
    assert isinstance(sc, dict)
    # Coordinated minimum contract with the framing agent.
    for key in ("is_large", "depth_max", "avg_file_size_bytes"):
        assert key in sc, f"scale_context missing required key {key!r}"


# --------------------------------------------------------------------------- #
# LARGE repo → is_large True (signal READ from repo_probe, not a constant)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_large_repo_by_depth_sets_is_large_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap_repo(tmp_path)
    orch = _make_orch(tmp_path, StubAdapter({}))
    cap = _large_by_depth()
    _patch_capacity(monkeypatch, orch, cap)

    outcome = await run_intake_phase(orch, _WELL_FORMED)

    sc = outcome.scale_context
    assert sc["is_large"] is True
    # ENGAGEMENT: the surfaced values are READ from the probe, not synthesized.
    assert sc["depth_max"] == cap.depth_max == 12
    assert sc["avg_file_size_bytes"] == cap.avg_file_size_bytes == 100


@pytest.mark.asyncio
async def test_large_repo_by_avg_bytes_sets_is_large_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap_repo(tmp_path)
    orch = _make_orch(tmp_path, StubAdapter({}))
    cap = _large_by_avg_bytes()
    _patch_capacity(monkeypatch, orch, cap)

    outcome = await run_intake_phase(orch, _WELL_FORMED)

    sc = outcome.scale_context
    assert sc["is_large"] is True
    assert sc["avg_file_size_bytes"] == cap.avg_file_size_bytes == 90_000
    assert sc["depth_max"] == cap.depth_max == 3


# --------------------------------------------------------------------------- #
# SMALL/shallow repo → is_large False
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_small_repo_sets_is_large_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap_repo(tmp_path)
    orch = _make_orch(tmp_path, StubAdapter({}))
    cap = _small_shallow()
    _patch_capacity(monkeypatch, orch, cap)

    outcome = await run_intake_phase(orch, _WELL_FORMED)

    sc = outcome.scale_context
    assert sc["is_large"] is False
    assert sc["depth_max"] == 2
    assert sc["avg_file_size_bytes"] == 1_000


# --------------------------------------------------------------------------- #
# scale_context is surfaced from the SIGNAL, not a constant (anti-vacuity)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_is_large_tracks_the_probe_not_a_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """is_large flips with the probe — a constant would fail one of these.

    Distinct repos (sub-dirs) so the small run is a FRESH probe, not a resume off
    the big run's persisted evidence (which would correctly carry is_large=True).
    """
    big_dir = tmp_path / "big"
    small_dir = tmp_path / "small"
    big_dir.mkdir()
    small_dir.mkdir()
    _bootstrap_repo(big_dir)
    _bootstrap_repo(small_dir)

    orch_big = _make_orch(big_dir, StubAdapter({}))
    _patch_capacity(monkeypatch, orch_big, _large_by_depth())
    big = await run_intake_phase(orch_big, _WELL_FORMED)

    orch_small = _make_orch(small_dir, StubAdapter({}))
    _patch_capacity(monkeypatch, orch_small, _small_shallow())
    small = await run_intake_phase(orch_small, _WELL_FORMED)

    assert big.scale_context["is_large"] is True
    assert small.scale_context["is_large"] is False


# --------------------------------------------------------------------------- #
# BROKEN-CONTROL: probe reports large but intake does NOT read it → False
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_broken_control_unread_probe_stays_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If intake builds scale_context WITHOUT reading repo_probe, is_large is False.

    This simulates the pre-wiring (vacuous) world: a large probe exists on the
    orchestrator, but the scale-context source is severed from the real probe and
    fed an empty/default capacity instead. ``is_large`` is derived from what was
    actually consumed, so a starved source yields False — proving the GREEN tests
    above pass because the LARGE signal is genuinely READ, not synthesized.
    """
    from orchestrator import intake_phase as ip

    _bootstrap_repo(tmp_path)
    orch = _make_orch(tmp_path, StubAdapter({}))
    # The orchestrator's REAL probe says large...
    _patch_capacity(monkeypatch, orch, _large_by_depth())

    # ...but the control severs the read: the scale_context source ignores
    # ``orch.repo_capacity`` and builds from an empty/default capacity. We call
    # the REAL ``_build_scale_context`` (not the patched name) on an empty
    # snapshot, so this exercises the genuine derivation logic, not a stub.
    empty_cap = RepoCapacity(
        file_count=0, total_bytes=0, depth_max=0, is_huge=False
    )
    monkeypatch.setattr(
        ip, "_scale_context_for", lambda _orch: ip._build_scale_context(empty_cap)
    )

    outcome = await run_intake_phase(orch, _WELL_FORMED)
    assert outcome.scale_context["is_large"] is False
    # Sanity: the real builder DOES report large for the real (large) probe — so
    # the False above is the severance, not a builder that can never say True.
    assert ip._build_scale_context(_large_by_depth())["is_large"] is True


# --------------------------------------------------------------------------- #
# durability: scale_context persists on IntakeEvidence + survives resume
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_scale_context_persisted_and_survives_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bootstrap_repo(tmp_path)
    orch = _make_orch(tmp_path, StubAdapter({}))
    _patch_capacity(monkeypatch, orch, _large_by_depth())

    first = await run_intake_phase(orch, _WELL_FORMED)
    assert first.scale_context["is_large"] is True

    ev = await read_evidence(tmp_path, "plan-intake", "intake")
    assert isinstance(ev, IntakeEvidence)
    assert ev.scale_context["is_large"] is True
    assert ev.scale_context["depth_max"] == 12

    # Resume path re-reads evidence (0 dispatches) and must reconstruct the dict.
    orch2 = _make_orch(tmp_path, StubAdapter({}))
    # Deliberately give the resume orch a SMALL probe; the persisted (large)
    # scale_context must win, proving resume reads evidence not a fresh probe.
    _patch_capacity(monkeypatch, orch2, _small_shallow())
    resumed = await run_intake_phase(orch2, _WELL_FORMED)
    assert resumed.scale_context["is_large"] is True
    assert resumed.scale_context["depth_max"] == 12
