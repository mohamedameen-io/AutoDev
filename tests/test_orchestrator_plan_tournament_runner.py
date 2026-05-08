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


# ---------------------------------------------------------------------------
# v0.12.0 — run_plan_tournament branch_index/branch_seed parameters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_plan_tournament_legacy_dir_when_no_branch_index(
    tmp_path: Path,
    capture_tournament: type[_CapturingTournament],
) -> None:
    """No ``branch_index`` → legacy ``tournaments/plan-{hash}/`` dir."""
    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(tmp_path, adapter)
    md = _plan_md("medium")

    await ptr.run_plan_tournament(orch, md, "spec text", spec_hash=_SPEC_HASH)

    captured = capture_tournament.captured_artifact_dir
    assert captured is not None
    # Legacy single-branch path under tournaments/plan-{hash}/
    parts = captured.parts
    assert "tournaments" in parts
    assert any(p.startswith("plan-") for p in parts)
    assert not any(p.startswith("multi-") for p in parts)


@pytest.mark.asyncio
async def test_run_plan_tournament_multi_dir_when_branch_index_set(
    tmp_path: Path,
    capture_tournament: type[_CapturingTournament],
) -> None:
    """``branch_index=2`` → ``tournaments/multi-{hash}/branch-2/`` dir."""
    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(tmp_path, adapter)
    md = _plan_md("medium")

    await ptr.run_plan_tournament(
        orch,
        md,
        "spec text",
        spec_hash=_SPEC_HASH,
        branch_index=2,
        branch_seed=42,
    )

    captured = capture_tournament.captured_artifact_dir
    assert captured is not None
    parts = captured.parts
    assert "tournaments" in parts
    assert f"multi-{_SPEC_HASH[:8]}" in parts
    assert "branch-2" in parts


@pytest.mark.asyncio
async def test_run_plan_tournament_branch_seed_diverges_rng(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different ``branch_seed`` values → different RNG seeds.

    Captures the ``rng`` argument passed to the Tournament constructor,
    asserts that two distinct ``branch_seed`` values produce RNG instances
    whose first-draw output differs.
    """
    captured_rngs: list[Any] = []

    class _CaptureRng:
        def __init__(self, **kwargs: Any) -> None:
            captured_rngs.append(kwargs.get("rng"))

        async def run(self, *, task_prompt: str, initial: str) -> tuple[str, list]:
            return initial, []

    monkeypatch.setattr(ptr, "Tournament", _CaptureRng)
    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(tmp_path, adapter)
    md = _plan_md("medium")

    await ptr.run_plan_tournament(
        orch, md, "spec", spec_hash=_SPEC_HASH, branch_index=0, branch_seed=100
    )
    await ptr.run_plan_tournament(
        orch, md, "spec", spec_hash=_SPEC_HASH, branch_index=1, branch_seed=200
    )

    assert len(captured_rngs) == 2
    rng_a, rng_b = captured_rngs
    assert rng_a is not None and rng_b is not None
    # Distinct seeds → distinct first draws.
    a_draw = rng_a.random()
    b_draw = rng_b.random()
    assert a_draw != b_draw, (
        "Distinct branch_seed values should yield divergent RNG streams; "
        f"both seeds drew {a_draw}"
    )


@pytest.mark.asyncio
async def test_run_plan_tournament_branch_seed_none_uses_spec_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``branch_seed=None`` preserves v0.11.x behavior: seed from spec_hash.

    Two runs with the same ``spec_hash`` and no ``branch_seed`` produce
    RNG instances whose first draws match (deterministic legacy seeding).
    """
    captured_rngs: list[Any] = []

    class _CaptureRng:
        def __init__(self, **kwargs: Any) -> None:
            captured_rngs.append(kwargs.get("rng"))

        async def run(self, *, task_prompt: str, initial: str) -> tuple[str, list]:
            return initial, []

    monkeypatch.setattr(ptr, "Tournament", _CaptureRng)
    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(tmp_path, adapter)
    md = _plan_md("medium")

    await ptr.run_plan_tournament(orch, md, "spec", spec_hash=_SPEC_HASH)
    await ptr.run_plan_tournament(orch, md, "spec", spec_hash=_SPEC_HASH)

    assert len(captured_rngs) == 2
    rng_a, rng_b = captured_rngs
    assert rng_a is not None and rng_b is not None
    a_draw = rng_a.random()
    b_draw = rng_b.random()
    # Same spec_hash + same default seeding path → identical streams.
    assert a_draw == b_draw


# ---------------------------------------------------------------------------
# v0.14.0 — run_plan_tournament branch_config parameter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_plan_tournament_branch_config_overrides_role_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``branch_config.model_overrides`` is set, the AdapterLLMClient
    constructed for the branch carries those overrides."""
    from config.schema import BranchConfig

    captured_clients: list[Any] = []

    class _CaptureClientCfg:
        def __init__(self, *, handler: Any, client: Any, cfg: Any,
                     artifact_dir: Path, rng: Any = None,
                     judge_plugins: Any = None) -> None:
            captured_clients.append(client)

        async def run(self, *, task_prompt: str, initial: str) -> tuple[str, list]:
            return initial, []

    monkeypatch.setattr(ptr, "Tournament", _CaptureClientCfg)
    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(tmp_path, adapter)
    md = _plan_md("medium")
    bc = BranchConfig(
        model_overrides={"developer": "claude-sonnet-4-5", "judge": "claude-haiku-4-5"},
        lane="distant-scout",
    )

    await ptr.run_plan_tournament(
        orch,
        md,
        "spec",
        spec_hash=_SPEC_HASH,
        branch_index=0,
        branch_seed=100,
        branch_config=bc,
    )

    assert len(captured_clients) == 1
    client = captured_clients[0]
    # The client must carry the per-role override map.
    overrides = getattr(client, "_role_model_overrides", None)
    assert overrides is not None
    assert overrides.get("developer") == "claude-sonnet-4-5"
    assert overrides.get("judge") == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_run_plan_tournament_branch_config_none_uses_resolve_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``branch_config=None`` (default) → no per-role override map; the
    client's ``_role_model_overrides`` is None or empty."""
    captured_clients: list[Any] = []

    class _CaptureClientCfg:
        def __init__(self, *, handler: Any, client: Any, cfg: Any,
                     artifact_dir: Path, rng: Any = None,
                     judge_plugins: Any = None) -> None:
            captured_clients.append(client)

        async def run(self, *, task_prompt: str, initial: str) -> tuple[str, list]:
            return initial, []

    monkeypatch.setattr(ptr, "Tournament", _CaptureClientCfg)
    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(tmp_path, adapter)
    md = _plan_md("medium")

    await ptr.run_plan_tournament(orch, md, "spec", spec_hash=_SPEC_HASH)

    assert len(captured_clients) == 1
    client = captured_clients[0]
    overrides = getattr(client, "_role_model_overrides", None)
    # Either None or empty dict — both express "no overrides".
    assert not overrides


@pytest.mark.asyncio
async def test_run_plan_tournament_branch_config_lane_in_artifact_dir(
    tmp_path: Path,
    capture_tournament: type[_CapturingTournament],
) -> None:
    """``branch_config.lane`` suffixes the artifact dir name:
    ``tournaments/multi-{hash[:8]}/branch-{i}-{lane}/``."""
    from config.schema import BranchConfig

    adapter = StubAdapter({"explorer": ok("ok")})
    orch = _make_orch(tmp_path, adapter)
    md = _plan_md("medium")
    bc = BranchConfig(lane="distant-scout")

    await ptr.run_plan_tournament(
        orch,
        md,
        "spec",
        spec_hash=_SPEC_HASH,
        branch_index=2,
        branch_seed=42,
        branch_config=bc,
    )

    captured = capture_tournament.captured_artifact_dir
    assert captured is not None
    parts = captured.parts
    assert f"multi-{_SPEC_HASH[:8]}" in parts
    # Lane-suffixed dir name.
    assert "branch-2-distant-scout" in parts
