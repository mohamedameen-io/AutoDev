"""Tests for :mod:`orchestrator.plan_tournament_runner`.

v0.7.0 (Issue 5C) — Complexity-aware judge ensemble.

When the architect emits ``COMPLEXITY: complex`` and
``cfg.complex_plan_num_judges_override`` is set, the plan tournament
escalates ``num_judges`` from the default to the override value. Medium /
simple plans (or an unset override) keep the default num_judges.

Tests intercept :class:`~tournament.core.Tournament` construction in the
plan_tournament_runner module so we can read the resolved
:class:`~tournament.core.TournamentConfig` without actually running the
tournament — we want to assert *what was constructed*, not *what was run*.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import plan_tournament_runner as ptr

from stub_adapter import StubAdapter, ok


# ---------------------------------------------------------------------------
# Helpers — minimal plan markdown, including the COMPLEXITY: directive.
# ---------------------------------------------------------------------------


def _plan_md(complexity: str | None) -> str:
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


# A spec hash valid for ``int(spec_hash, 16)`` (used to seed the RNG).
_SPEC_HASH = "0123456789abcdef"


# ---------------------------------------------------------------------------
# Tournament-construction interceptor.
#
# Replaces :class:`Tournament` in ``plan_tournament_runner`` with a fake whose
# ``__init__`` captures the resolved ``TournamentConfig`` and whose ``run``
# returns the initial markdown unchanged. This lets us assert on the
# constructed cfg.num_judges without depending on the upstream tournament
# execution path (no subprocess, no judge calls, no artifacts).
# ---------------------------------------------------------------------------


class _CapturingTournament:
    """Stand-in for :class:`Tournament` that records its constructor args."""

    captured_cfg: Any = None
    captured_artifact_dir: Path | None = None

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
        # Store on the class so the test can read it after run_plan_tournament
        # returns (instances are constructed inside the runner and discarded).
        type(self).captured_cfg = cfg
        type(self).captured_artifact_dir = artifact_dir
        self._initial_md = ""

    async def run(self, *, task_prompt: str, initial: str) -> tuple[str, list]:
        self._initial_md = initial
        return initial, []


@pytest.fixture
def capture_tournament(monkeypatch: pytest.MonkeyPatch) -> type[_CapturingTournament]:
    """Replace ``plan_tournament_runner.Tournament`` with the capturing fake."""
    # Reset class-level state between tests.
    _CapturingTournament.captured_cfg = None
    _CapturingTournament.captured_artifact_dir = None
    monkeypatch.setattr(ptr, "Tournament", _CapturingTournament)
    return _CapturingTournament


def _make_orch(
    cwd: Path,
    adapter: StubAdapter,
    *,
    num_judges: int = 5,
    complex_plan_num_judges_override: int | None = 7,
) -> Orchestrator:
    """Build an Orchestrator with controlled plan-tournament knobs.

    The judge model defaults to ``sonnet`` (avoiding the auto-disable path),
    and ``auto_disable_for_models`` is cleared as a belt-and-suspenders.
    """
    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.plan.num_judges = num_judges
    cfg.tournaments.plan.convergence_k = 1
    cfg.tournaments.plan.max_rounds = 3
    cfg.tournaments.plan.complex_plan_num_judges_override = (
        complex_plan_num_judges_override
    )
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-judge-override",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complex_plan_uses_override_judges(
    tmp_path: Path,
    capture_tournament: type[_CapturingTournament],
) -> None:
    """``complexity=complex`` + ``override=7`` → ``TournamentConfig.num_judges == 7``."""
    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(
        tmp_path, adapter, num_judges=5, complex_plan_num_judges_override=7
    )
    md = _plan_md("complex")

    await ptr.run_plan_tournament(orch, md, "spec text", spec_hash=_SPEC_HASH)

    cfg = capture_tournament.captured_cfg
    assert cfg is not None, "Tournament was never constructed"
    assert cfg.num_judges == 7


@pytest.mark.asyncio
async def test_medium_plan_uses_default_judges(
    tmp_path: Path,
    capture_tournament: type[_CapturingTournament],
) -> None:
    """``complexity=medium`` does not trigger the override; default 5 stands."""
    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(
        tmp_path, adapter, num_judges=5, complex_plan_num_judges_override=7
    )
    md = _plan_md("medium")

    await ptr.run_plan_tournament(orch, md, "spec text", spec_hash=_SPEC_HASH)

    cfg = capture_tournament.captured_cfg
    assert cfg is not None
    assert cfg.num_judges == 5


@pytest.mark.asyncio
async def test_simple_plan_uses_default_judges(
    tmp_path: Path,
    capture_tournament: type[_CapturingTournament],
) -> None:
    """``complexity=simple`` does not trigger the override either."""
    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(
        tmp_path, adapter, num_judges=5, complex_plan_num_judges_override=7
    )
    md = _plan_md("simple")

    await ptr.run_plan_tournament(orch, md, "spec text", spec_hash=_SPEC_HASH)

    cfg = capture_tournament.captured_cfg
    assert cfg is not None
    assert cfg.num_judges == 5


@pytest.mark.asyncio
async def test_override_none_keeps_default(
    tmp_path: Path,
    capture_tournament: type[_CapturingTournament],
) -> None:
    """``override=None`` (feature off) keeps default num_judges even on complex plans."""
    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(
        tmp_path, adapter, num_judges=5, complex_plan_num_judges_override=None
    )
    md = _plan_md("complex")

    await ptr.run_plan_tournament(orch, md, "spec text", spec_hash=_SPEC_HASH)

    cfg = capture_tournament.captured_cfg
    assert cfg is not None
    assert cfg.num_judges == 5


@pytest.mark.asyncio
async def test_complex_plan_override_explicit_3_uses_3(
    tmp_path: Path,
    capture_tournament: type[_CapturingTournament],
) -> None:
    """A non-default override (3) is honored when complexity is complex.

    Sanity check that the override is used as-is (no upper-bound clamp,
    no minimum of cfg.num_judges) — the user controls the value directly.
    """
    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(
        tmp_path, adapter, num_judges=5, complex_plan_num_judges_override=3
    )
    md = _plan_md("complex")

    await ptr.run_plan_tournament(orch, md, "spec text", spec_hash=_SPEC_HASH)

    cfg = capture_tournament.captured_cfg
    assert cfg is not None
    assert cfg.num_judges == 3


@pytest.mark.asyncio
async def test_no_complexity_directive_keeps_default(
    tmp_path: Path,
    capture_tournament: type[_CapturingTournament],
) -> None:
    """Markdown without a ``COMPLEXITY:`` line behaves as 'not complex'."""
    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(
        tmp_path, adapter, num_judges=5, complex_plan_num_judges_override=7
    )
    md = _plan_md(None)

    await ptr.run_plan_tournament(orch, md, "spec text", spec_hash=_SPEC_HASH)

    cfg = capture_tournament.captured_cfg
    assert cfg is not None
    assert cfg.num_judges == 5


# ---------------------------------------------------------------------------
# v0.10.0 — resolve_parallelism wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_runner_resolves_parallelism_via_runtime_helper(
    tmp_path: Path,
    capture_tournament: type[_CapturingTournament],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plan-runner asks ``runtime.resource_probe.resolve_parallelism``
    for the value it stuffs into ``TournamentConfig.max_parallel_subprocesses``.

    Test strategy: monkeypatch ``resolve_parallelism`` in the runner's
    namespace (not the runtime module) to return a sentinel value (``42``).
    Assert the captured ``TournamentConfig`` carries that exact value —
    proves the runner is calling the resolver, not falling back to the
    legacy literal pass-through.
    """
    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(tmp_path, adapter)
    # Default config: max_parallel_subprocesses is None (v0.10.0 default).
    assert orch.cfg.tournaments.max_parallel_subprocesses is None
    md = _plan_md("medium")

    captured_kwargs: dict = {}

    def fake_resolve(
        configured: int | None,
        capacity: object,
        role_mix: str,
        num_judges: int,
    ) -> int:
        captured_kwargs.update(
            configured=configured,
            role_mix=role_mix,
            num_judges=num_judges,
        )
        return 42

    monkeypatch.setattr(ptr, "resolve_parallelism", fake_resolve)

    await ptr.run_plan_tournament(orch, md, "spec text", spec_hash=_SPEC_HASH)

    cfg = capture_tournament.captured_cfg
    assert cfg is not None
    assert cfg.max_parallel_subprocesses == 42, (
        f"Expected the runner to wire resolve_parallelism's return value "
        f"into TournamentConfig.max_parallel_subprocesses; got "
        f"{cfg.max_parallel_subprocesses}"
    )
    assert captured_kwargs["role_mix"] == "plan"
    assert captured_kwargs["num_judges"] == 5  # default cfg.num_judges
    assert captured_kwargs["configured"] is None  # auto path


@pytest.mark.asyncio
async def test_plan_runner_passes_configured_int_through_resolver(
    tmp_path: Path,
    capture_tournament: type[_CapturingTournament],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the operator pins ``max_parallel_subprocesses=8``, the runner
    forwards that value into ``resolve_parallelism`` (which then returns
    ``8`` per the explicit-int passthrough rule)."""
    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(tmp_path, adapter)
    orch.cfg.tournaments.max_parallel_subprocesses = 8

    captured_kwargs: dict = {}

    def fake_resolve(
        configured: int | None,
        capacity: object,
        role_mix: str,
        num_judges: int,
    ) -> int:
        captured_kwargs["configured"] = configured
        return configured if configured is not None else 1

    monkeypatch.setattr(ptr, "resolve_parallelism", fake_resolve)

    await ptr.run_plan_tournament(
        orch, _plan_md("medium"), "spec text", spec_hash=_SPEC_HASH
    )

    cfg = capture_tournament.captured_cfg
    assert cfg is not None
    assert cfg.max_parallel_subprocesses == 8
    assert captured_kwargs["configured"] == 8


# ---------------------------------------------------------------------------
# v0.12.0 — _plan_tournament_id branch_index namespacing
# ---------------------------------------------------------------------------


def test_plan_tournament_id_without_branch_uses_legacy_namespace() -> None:
    """``_plan_tournament_id(spec_hash)`` with no branch index returns the
    legacy single-branch id ``f"plan-{spec_hash[:8]}"`` (backward-compat
    with v0.11.x and earlier)."""
    assert ptr._plan_tournament_id(_SPEC_HASH) == "plan-01234567"


def test_plan_tournament_id_with_branch_index_uses_multi_namespace() -> None:
    """``_plan_tournament_id(spec_hash, branch_index=N)`` returns the
    branch-namespaced id ``f"plan-{spec_hash[:8]}-branch{N}"`` for use
    with v0.12.0 multi-branch tournaments."""
    assert ptr._plan_tournament_id(_SPEC_HASH, branch_index=0) == "plan-01234567-branch0"
    assert ptr._plan_tournament_id(_SPEC_HASH, branch_index=1) == "plan-01234567-branch1"
    assert ptr._plan_tournament_id(_SPEC_HASH, branch_index=2) == "plan-01234567-branch2"


def test_plan_tournament_id_branch_index_none_explicit_equals_legacy() -> None:
    """Explicit ``branch_index=None`` matches the no-arg call (back-compat
    surface for callers that always pass the kwarg)."""
    assert ptr._plan_tournament_id(_SPEC_HASH, branch_index=None) == ptr._plan_tournament_id(
        _SPEC_HASH
    )
