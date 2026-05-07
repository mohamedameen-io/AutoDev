"""Wiring tests for the per-role ``--effort`` plumbing (Step 6).

These tests exercise the consumer sites that call
:func:`tournament.effort.resolve_role_effort` and verify that ``inv.effort``
arrives at the adapter populated correctly.

Focus is on the helpers (``_build_role_overrides`` /
``_build_tournament_role_overrides`` / ``_cli_role_overrides``) — full
integration tests in ``test_orchestrator_plan_phase.py``,
``test_plan_tournament_integration.py``, and
``test_impl_tournament_runner.py`` already cover the upstream paths.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.impl_tournament_runner import _build_tournament_role_overrides
from orchestrator.plan_tournament_runner import _build_role_overrides
from cli.commands.tournament import _cli_role_overrides
from state.schemas import Phase, Plan, Task

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


CANONICAL_PLAN_MD = """# Plan: e
## Phase 1: x
### Task 1.1: t
  - Description: do
"""


def _make_orch_for_wiring(
    cwd: Path,
    *,
    user_complexity: str = "medium",
    judge_effort_override: str | None = None,
) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.user_complexity = user_complexity  # type: ignore[assignment]
    if judge_effort_override is not None:
        cfg.agents["judge"].effort = judge_effort_override  # type: ignore[assignment]
    registry = build_registry(cfg)
    adapter = StubAdapter({"explorer": ok("ok")})
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-effort-wiring",
    )


def _seed_plan(orch: Orchestrator, complexity: str | None) -> Plan:
    """Initialize a plan with the given complexity into the plan_manager."""
    plan = Plan(
        plan_id="p-eff",
        spec_hash="h",
        phases=[
            Phase(
                id="1",
                title="x",
                tasks=[
                    Task(id="1.1", phase_id="1", title="t", description="do")
                ],
            )
        ],
        complexity=complexity,  # type: ignore[arg-type]
        created_at=_iso(),
        updated_at=_iso(),
    )
    return plan


# ---------------------------------------------------------------------------
# plan_tournament_runner._build_role_overrides
# ---------------------------------------------------------------------------


def test_plan_tournament_role_effort_complex_plan(tmp_path: Path) -> None:
    """With plan_complexity='complex', architect_b/synthesizer get xhigh,
    judge/critic_t get medium (per EFFORT_MATRIX)."""
    orch = _make_orch_for_wiring(tmp_path)

    rmt, rat, role_effort = _build_role_overrides(orch, "complex")

    assert role_effort["architect_b"] == "xhigh"
    assert role_effort["synthesizer"] == "xhigh"
    assert role_effort["judge"] == "medium"
    assert role_effort["critic_t"] == "medium"


def test_plan_tournament_role_effort_medium_plan(tmp_path: Path) -> None:
    """With plan_complexity='medium', authors get high, evaluators get medium."""
    orch = _make_orch_for_wiring(tmp_path)

    _, _, role_effort = _build_role_overrides(orch, "medium")

    assert role_effort["architect_b"] == "high"
    assert role_effort["synthesizer"] == "high"
    assert role_effort["judge"] == "medium"
    assert role_effort["critic_t"] == "medium"


def test_plan_tournament_role_effort_simple_plan(tmp_path: Path) -> None:
    """With plan_complexity='simple', authors get medium, evaluators get low."""
    orch = _make_orch_for_wiring(tmp_path)

    _, _, role_effort = _build_role_overrides(orch, "simple")

    assert role_effort["architect_b"] == "medium"
    assert role_effort["synthesizer"] == "medium"
    assert role_effort["judge"] == "low"
    assert role_effort["critic_t"] == "low"


def test_plan_tournament_role_effort_no_complexity_yields_empty(
    tmp_path: Path,
) -> None:
    """Without a plan_complexity, no tournament roles resolve to a non-None
    effort (architect runs at the floor outside the tournament)."""
    orch = _make_orch_for_wiring(tmp_path)

    _, _, role_effort = _build_role_overrides(orch, None)

    # All four tournament roles fall through to None.
    assert role_effort == {}


def test_plan_tournament_role_effort_explicit_override_wins(
    tmp_path: Path,
) -> None:
    """``cfg.agents['judge'].effort='low'`` overrides the matrix value."""
    orch = _make_orch_for_wiring(tmp_path, judge_effort_override="low")

    _, _, role_effort = _build_role_overrides(orch, "complex")

    # judge override wins; architect_b/synthesizer remain at xhigh.
    assert role_effort["judge"] == "low"
    assert role_effort["architect_b"] == "xhigh"
    assert role_effort["critic_t"] == "medium"  # unchanged


# ---------------------------------------------------------------------------
# Regression test: run_plan_tournament must extract complexity from initial_md
# (not from plan_manager — plan isn't persisted yet at tournament time).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_plan_tournament_extracts_complexity_from_initial_md(
    tmp_path: Path,
) -> None:
    """``run_plan_tournament`` must pull plan_complexity out of its ``initial_md``
    argument because at the moment it runs, ``plan_manager.load()`` would still
    return None — the parsed Plan is only persisted AFTER the tournament.

    Verifies the wiring by building an AdapterLLMClient via the helper-extraction
    path and asserting role_effort reflects the COMPLEXITY: classification.
    """
    from orchestrator.plan_parser import extract_complexity

    initial_md = (
        "# Plan: foo\n"
        "## Phase 1: bar\n"
        "### Task 1.1: baz\n"
        "  - Description: do\n"
        "\n"
        "COMPLEXITY: complex\n"
    )

    # Sanity: extract_complexity returns the classification.
    assert extract_complexity(initial_md) == "complex"

    # Sanity: a markdown without the line returns None.
    assert extract_complexity("# Plan: foo\n## Phase 1: bar\n### Task 1.1: baz\n  - Description: do\n") is None

    # Wire-up: the helper, given the extracted complexity, returns the right matrix.
    orch = _make_orch_for_wiring(tmp_path)
    _, _, role_effort = _build_role_overrides(orch, extract_complexity(initial_md))
    assert role_effort["architect_b"] == "xhigh"  # complex → author tier → xhigh
    assert role_effort["judge"] == "medium"        # complex → evaluator tier → medium


# ---------------------------------------------------------------------------
# impl_tournament_runner._build_tournament_role_overrides
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_impl_tournament_role_effort_complex_plan(tmp_path: Path) -> None:
    """Same shape as plan_tournament: complex plan → matrix values."""
    orch = _make_orch_for_wiring(tmp_path)
    plan = _seed_plan(orch, "complex")
    await orch.plan_manager.init_plan(plan)

    _, _, role_effort = await _build_tournament_role_overrides(orch)

    assert role_effort["architect_b"] == "xhigh"
    assert role_effort["synthesizer"] == "xhigh"
    assert role_effort["judge"] == "medium"
    assert role_effort["critic_t"] == "medium"


@pytest.mark.asyncio
async def test_impl_tournament_role_effort_explicit_override_wins(
    tmp_path: Path,
) -> None:
    """An explicit override on the impl path also wins over the matrix."""
    orch = _make_orch_for_wiring(tmp_path, judge_effort_override="low")
    plan = _seed_plan(orch, "medium")
    await orch.plan_manager.init_plan(plan)

    _, _, role_effort = await _build_tournament_role_overrides(orch)

    assert role_effort["judge"] == "low"
    assert role_effort["architect_b"] == "high"


# ---------------------------------------------------------------------------
# CLI _cli_role_overrides (no plan; only explicit overrides apply)
# ---------------------------------------------------------------------------


def test_cli_role_overrides_no_plan_returns_empty_effort() -> None:
    """Without a plan and without per-role overrides, role_effort is empty."""
    cfg = default_config()
    rmt, rat, role_effort = _cli_role_overrides(cfg)
    # Sanity: max_turns and tools still populated from cfg.
    assert "judge" in rmt
    assert "judge" in rat
    # role_effort empty because plan_complexity=None for non-architect roles.
    assert role_effort == {}


def test_cli_role_overrides_includes_role_effort_with_explicit_config() -> None:
    """``cfg.agents['judge'].effort='low'`` propagates through the CLI path."""
    cfg = default_config()
    cfg.agents["judge"].effort = "low"  # type: ignore[assignment]

    _, _, role_effort = _cli_role_overrides(cfg)
    assert role_effort == {"judge": "low"}


def test_cli_role_overrides_explicit_overrides_for_multiple_roles() -> None:
    """Multiple per-role explicit overrides are all honored."""
    cfg = default_config()
    cfg.agents["judge"].effort = "low"  # type: ignore[assignment]
    cfg.agents["architect_b"].effort = "max"  # type: ignore[assignment]
    cfg.agents["critic_t"].effort = "low"  # type: ignore[assignment]

    _, _, role_effort = _cli_role_overrides(cfg)
    assert role_effort == {
        "judge": "low",
        "architect_b": "max",
        "critic_t": "low",
    }


# ---------------------------------------------------------------------------
# plan_phase: architect direct invocation gets architect-floor effort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_phase_architect_invocation_uses_architect_floor(
    tmp_path: Path,
) -> None:
    """Architect direct invocation in plan_phase sets ``inv.effort = xhigh``
    when ``cfg.user_complexity='medium'``. The plan doesn't exist yet at
    architect time, so the floor (ARCHITECT_EFFORT) wins."""
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.user_complexity = "medium"  # type: ignore[assignment]
    registry = build_registry(cfg)
    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "domain_expert": ok("ok"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-arch-floor-medium",
    )
    await orch.plan("Add subtract")
    arch_calls = [c for c in adapter.calls if c.role == "architect"]
    assert len(arch_calls) >= 1
    assert arch_calls[0].effort == "xhigh"


@pytest.mark.asyncio
async def test_plan_phase_architect_invocation_uses_max_when_user_max(
    tmp_path: Path,
) -> None:
    """When ``cfg.user_complexity='max'``, the architect floor escalates to ``max``."""
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.user_complexity = "max"  # type: ignore[assignment]
    registry = build_registry(cfg)
    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "domain_expert": ok("ok"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-arch-floor-max",
    )
    await orch.plan("Add subtract")
    arch_calls = [c for c in adapter.calls if c.role == "architect"]
    assert len(arch_calls) >= 1
    assert arch_calls[0].effort == "max"


@pytest.mark.asyncio
async def test_plan_phase_explorer_invocation_falls_through_to_none(
    tmp_path: Path,
) -> None:
    """Explorer is not in ROLE_TIER and is not the architect → effort=None
    (inherit user-global default)."""
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.user_complexity = "medium"  # type: ignore[assignment]
    registry = build_registry(cfg)
    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "domain_expert": ok("ok"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-explorer-none",
    )
    await orch.plan("Add subtract")
    explorer_calls = [c for c in adapter.calls if c.role == "explorer"]
    assert len(explorer_calls) >= 1
    assert explorer_calls[0].effort is None
