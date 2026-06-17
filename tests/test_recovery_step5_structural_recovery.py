"""Step 5 (RECOVERY-CONTRACT §7 Step 5; gate R3) — structural-action recovery.

Step 5 fixes the EMPIRICALLY-CONFIRMED delivery binding constraint
(field-probes/SYNTHESIS.md): the universal execute failure was the cascade
``conflict_3way_failed → resolver re_architect → fell_through → task blocked``,
reproduced on a feature, a refactor, and a 50k-file repo. Root cause: the
resolver's STRUCTURAL actions (``re_architect`` / ``re_plan`` / ``narrow_scope`` /
``split_task``) emitted only a PROSE ``rationale``; ``parse_corrective_direction``
splits on TOP-LEVEL bullets, so prose → 0 corrective tasks → ``_resolver_corrective``
returns ``None`` → the resolver DECLINES → the task hard-blocks.

The fix (``execute_phase._synthesize_corrective_direction`` +
``_apply_resolution``): when the structural action did not supply a STRUCTURED
``params['direction']``, synthesize a bulleted direction from the task context +
action type so the parser yields >= 1 corrective task. The original task is then
``skipped`` (recovered), not ``blocked``.

This module proves R3 for ALL THREE structural classes
(``CONFLICT_3WAY_FAILED``, ``DAG_INVALID``, ``EDIT_SCOPE_VIOLATION``) through the
real resolver path (``deterministic_action`` → ``_apply_resolution`` →
``_resolver_corrective``), driven via ``_try_retry_or_escalate`` under
``resolver_enabled``:

  * the original task ends ``status != "blocked"`` (skipped), and
  * corrective tasks were APPENDED to the phase.

Non-vacuity / broken-control: reverting the synthesis (forcing the PROSE
rationale into the parser) makes ``parse_corrective_direction`` return 0 →
``_resolver_corrective`` returns None → the task would block. We assert that
broken control directly so the gate cannot pass on nothing.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import execute_phase as ep
from orchestrator import failure_classes as _fcls
from orchestrator.corrective_parser import parse_corrective_direction
from state.plan_manager import PlanManager
from state.schemas import (
    AcceptanceCriterion,
    BlockerContext,
    Phase,
    Plan,
    ResolutionAction,
    Task,
)

from stub_adapter import StubAdapter, fail, ok

pytestmark = pytest.mark.resolver_enabled

_STRUCTURAL_CLASSES = (
    _fcls.CONFLICT_3WAY_FAILED,
    _fcls.DAG_INVALID,
    _fcls.EDIT_SCOPE_VIOLATION,
)


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan() -> Plan:
    return Plan(
        plan_id="p-step5-structural",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Add the widget factory",
                        description="d1",
                        files=["src/widget.py", "src/factory.py"],
                        complexity="medium",
                    ),
                ],
                acceptance=[AcceptanceCriterion(id="ph-1", description="ok")],
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    (repo / "math_utils.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)


def _make_cfg() -> Any:
    cfg = default_config()
    cfg.tournaments.phase_review.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.auto_disable_for_models = []
    cfg.qa_retry_min_interval_s = 0.0
    cfg.qa_retry_limit = 1
    return cfg


async def _build_orch(repo: Path, *, session: str) -> Orchestrator:
    cfg = _make_cfg()
    registry = build_registry(cfg)
    adapter = StubAdapter(
        {"explorer": ok("ok"), "developer": fail("developer always fails")}
    )
    pm = PlanManager(repo, session_id=f"{session}-init")
    await pm.init_plan(_mk_plan())
    return Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id=f"{session}-exec",
    )


# ---------------------------------------------------------------------------
# R3: each structural class recovers (skipped, not blocked) + injects tasks.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure_class", _STRUCTURAL_CLASSES)
@pytest.mark.asyncio
async def test_structural_class_recovers_via_resolver(
    tmp_path: Path, failure_class: str
) -> None:
    """For each structural class, the resolver injects corrective tasks and the
    original task ends ``status != 'blocked'`` (skipped) — driven through the real
    deterministic-action → _apply_resolution → _resolver_corrective path."""
    repo = tmp_path / f"repo-{failure_class}"
    repo.mkdir()
    _git_init(repo)

    orch = await _build_orch(repo, session=f"step5-{failure_class}")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")
    base_task_count = len(plan.phases[0].tasks)

    result = await ep._try_retry_or_escalate(
        orch,
        task,
        retry_limit=orch.cfg.qa_retry_limit,
        reason=f"structural failure: {failure_class}",
        failure_class=failure_class,
    )

    # 1) The original task is recovered (skipped), NOT blocked.
    assert getattr(result, "status", None) != "blocked", (
        f"{failure_class}: resolver failed to recover structurally "
        f"(status={getattr(result, 'status', None)})"
    )
    assert getattr(result, "status", None) == "skipped", (
        f"{failure_class}: expected the original task to be skipped after "
        f"corrective injection, got {getattr(result, 'status', None)}"
    )

    # 2) Corrective tasks were APPENDED to the phase.
    plan2 = await orch.plan_manager.load()
    assert plan2 is not None
    phase = plan2.phases[0]
    assert len(phase.tasks) > base_task_count, (
        f"{failure_class}: no corrective tasks were appended "
        f"({len(phase.tasks)} <= {base_task_count})"
    )
    corrective = [
        t for t in phase.tasks if t.metadata.get("origin") == "phase_review_corrective"
    ]
    assert corrective, (
        f"{failure_class}: appended tasks are not corrective-origin: "
        f"{[t.metadata for t in phase.tasks]}"
    )


# ---------------------------------------------------------------------------
# The synthesized direction is itself parseable (>= 1 task) for every action.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "min_tasks"),
    [
        ("re_architect", 1),
        ("re_plan", 1),
        ("narrow_scope", 1),
        ("split_task", 2),
    ],
)
def test_synthesized_direction_parses_to_tasks(
    action: str, min_tasks: int
) -> None:
    """The deterministic synthesis helper must produce a direction
    ``parse_corrective_direction`` turns into >= ``min_tasks`` corrective tasks."""
    task = Task(
        id="1.1",
        phase_id="1",
        title="Add the widget factory",
        description="d",
        files=["src/widget.py", "src/factory.py"],
        complexity="medium",
    )
    direction = ep._synthesize_corrective_direction(
        task, action, rationale="patches keep colliding"
    )
    tasks = parse_corrective_direction(direction, phase_id="1", base_task_count=1)
    assert len(tasks) >= min_tasks, (
        f"{action}: synthesized direction parsed to {len(tasks)} tasks "
        f"(< {min_tasks}); direction was:\n{direction}"
    )


# ---------------------------------------------------------------------------
# NON-VACUITY / broken-control: reverting the synthesis (PROSE direction) makes
# the parser return 0 → _resolver_corrective returns None → the task BLOCKS.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_broken_control_prose_direction_yields_zero_then_blocks(
    tmp_path: Path,
) -> None:
    """Broken-control proof: bypass the synthesis by handing ``_apply_resolution``
    a structural action whose ONLY signal is a prose rationale AND whose
    synthesized fallback we revert. We do this by directly exercising the two
    halves:

      * ``parse_corrective_direction(<prose>)`` returns 0 (the bug), and
      * ``_resolver_corrective(orch, task, <prose>)`` returns None (declines),

    proving the gate is non-vacuous: without the synthesis the structural action
    cannot recover and the caller would fall through to a legacy block.
    """
    # Half 1: prose parses to ZERO tasks (the original inertness).
    prose = (
        "merge conflict could not be resolved mechanically: rethink the task at "
        "component altitude so the patches stop colliding."
    )
    zero = parse_corrective_direction(prose, phase_id="1", base_task_count=1)
    assert zero == [], (
        "broken-control invalid: prose direction unexpectedly parsed to "
        f"{len(zero)} tasks"
    )

    # Half 2: _resolver_corrective with that prose declines (returns None), so a
    # caller relying on it would fall through to the legacy block.
    repo = tmp_path / "repo-control"
    repo.mkdir()
    _git_init(repo)
    orch = await _build_orch(repo, session="step5-control")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")

    declined = await ep._resolver_corrective(orch, task, prose)
    assert declined is None, (
        "broken-control invalid: _resolver_corrective recovered on PROSE "
        "(the synthesis is supposed to be the ONLY thing that makes it work)"
    )

    # And the SAME task, with a SYNTHESIZED direction, DOES recover — proving the
    # difference is the synthesis, not the harness.
    synth = ep._synthesize_corrective_direction(
        task, "re_architect", rationale=prose
    )
    recovered = await ep._resolver_corrective(orch, task, synth)
    assert recovered is not None and recovered.status == "skipped", (
        "synthesis path failed to recover where prose declined"
    )


# ---------------------------------------------------------------------------
# An LLM-supplied STRUCTURED direction must be preserved (not clobbered by the
# synthesized fallback).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_supplied_structured_direction_is_preserved(
    tmp_path: Path,
) -> None:
    """When ``params['direction']`` already has bullets, ``_apply_resolution`` uses
    it verbatim rather than synthesizing a generic fallback."""
    repo = tmp_path / "repo-supplied"
    repo.mkdir()
    _git_init(repo)
    orch = await _build_orch(repo, session="step5-supplied")
    plan = await orch.plan_manager.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    task = await orch.plan_manager.update_task_status(task.id, "in_progress")
    base = len(plan.phases[0].tasks)

    action = ResolutionAction(
        action="re_architect",
        rationale="prose only",
        params={
            "direction": (
                "- First specific corrective step supplied by the LLM resolver.\n"
                "- Second specific corrective step supplied by the LLM resolver."
            )
        },
    )
    ctx = BlockerContext(
        failure_class=_fcls.CONFLICT_3WAY_FAILED, task_id=task.id, phase_id="1"
    )
    result = await ep._apply_resolution(orch, task, ctx, action)
    assert result is not None and result.status == "skipped"

    plan2 = await orch.plan_manager.load()
    assert plan2 is not None
    corrective = [
        t
        for t in plan2.phases[0].tasks
        if t.metadata.get("origin") == "phase_review_corrective"
    ]
    # The two supplied bullets become two corrective tasks (not the single
    # synthesized re_architect bullet).
    assert len(corrective) == 2, (
        f"supplied structured direction not used verbatim: "
        f"{[t.title for t in corrective]} (base was {base})"
    )
