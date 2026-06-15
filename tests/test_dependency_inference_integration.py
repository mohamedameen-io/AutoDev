"""Integration tests for v0.41.0 A2 dependency inference.

Covers the full path the Run-3 failure exercised:

1. ``parse_plan_markdown`` runs the inference pass so a parsed plan carries
   the inferred ``depends_on`` edge (parser-level wiring).
2. ``PlanManager.next_pending_tasks`` then refuses to release the consumer
   until the producer is terminal (scheduler honors the inferred edge).
3. ``orchestrator.dag.warn_unordered_file_sharers`` fires the plan-gate
   WARNING when two same-phase tasks share a file with no ordering.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.dag import (
    validate_phase_dag,
    warn_unordered_file_sharers,
)
from orchestrator.plan_parser import parse_plan_markdown
from state.plan_manager import PlanManager
from state.schemas import Phase, Task


# ---------------------------------------------------------------------------
# parser-level wiring: parse_plan_markdown infers deps
# ---------------------------------------------------------------------------


_PLAN_RUN3 = """# Plan: Serializer routing

## Phase 1: Wire the serializer

### Task 1.1: Add the serializer
  - Description: Create the JSON serializer.
  - Files: [new] src/api/serialize.py

### Task 1.2: Route the handler through the serializer
  - Description: Update the handler to call the serializer.
  - Files: src/api/handler.py, [new] src/api/serialize.py
"""


def test_parser_infers_depends_on_from_file_overlap() -> None:
    """A parsed plan carries the inferred 1.2 depends_on 1.1 edge."""
    plan = parse_plan_markdown(_PLAN_RUN3)
    tasks = {t.id: t for t in plan.phases[0].tasks}
    assert tasks["1.2"].depends_on == ["1.1"]
    assert tasks["1.1"].depends_on == []


def test_parser_inferred_dag_is_valid() -> None:
    """The inferred edge passes validate_phase_dag (no cycle / undefined ref)."""
    plan = parse_plan_markdown(_PLAN_RUN3)
    validate_phase_dag(plan.phases[0])


# ---------------------------------------------------------------------------
# scheduler honors the inferred edge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_serializes_inferred_dependency(tmp_path: Path) -> None:
    """next_pending_tasks won't release 1.2 until 1.1 is terminal."""
    plan = parse_plan_markdown(_PLAN_RUN3, spec_hash="cafef00d")
    # Sanity: inference populated the edge before persistence.
    assert {t.id: t.depends_on for t in plan.phases[0].tasks}["1.2"] == ["1.1"]

    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(plan)

    # With both pending, only 1.1 is releasable (1.2 is gated on 1.1).
    batch = await pm.next_pending_tasks(limit=10)
    released = {t.id for t in batch}
    assert "1.1" in released
    assert "1.2" not in released

    # Drive 1.1 to a terminal state (pending -> blocked is a valid edge and
    # blocked is terminal for depends_on satisfaction).
    await pm.update_task_status("1.1", "blocked")

    batch2 = await pm.next_pending_tasks(limit=10)
    released2 = {t.id for t in batch2}
    # Now the dependency is satisfied → 1.2 is releasable.
    assert "1.2" in released2


# ---------------------------------------------------------------------------
# dag plan-gate WARNING on shared-file-no-dep
# ---------------------------------------------------------------------------


def _t(
    tid: str,
    *,
    files: list[str] | None = None,
    depends_on: list[str] | None = None,
) -> Task:
    return Task(
        id=tid,
        phase_id="1",
        title=f"task {tid}",
        description=f"do {tid}",
        files=list(files or []),
        depends_on=list(depends_on or []),
    )


def test_warn_fires_on_shared_file_no_dep() -> None:
    phase = Phase(
        id="1",
        title="p",
        tasks=[
            _t("1.1", files=["src/api/serialize.py"]),
            _t("1.2", files=["src/api/serialize.py"]),
        ],
    )
    warnings = warn_unordered_file_sharers(phase)
    assert len(warnings) == 1
    assert "1.1" in warnings[0] and "1.2" in warnings[0]


def test_warn_silent_when_dependency_present() -> None:
    phase = Phase(
        id="1",
        title="p",
        tasks=[
            _t("1.1", files=["src/api/serialize.py"]),
            _t("1.2", files=["src/api/serialize.py"], depends_on=["1.1"]),
        ],
    )
    assert warn_unordered_file_sharers(phase) == []


def test_warn_silent_on_transitive_ordering() -> None:
    """A path through an intermediate task counts as ordered."""
    phase = Phase(
        id="1",
        title="p",
        tasks=[
            _t("1.1", files=["src/shared.py"]),
            _t("1.2", depends_on=["1.1"]),
            _t("1.3", files=["src/shared.py"], depends_on=["1.2"]),
        ],
    )
    # 1.3 -> 1.2 -> 1.1 orders 1.3 after 1.1 transitively.
    assert warn_unordered_file_sharers(phase) == []


def test_warn_silent_when_no_shared_files() -> None:
    phase = Phase(
        id="1",
        title="p",
        tasks=[
            _t("1.1", files=["src/a.py"]),
            _t("1.2", files=["src/b.py"]),
        ],
    )
    assert warn_unordered_file_sharers(phase) == []


def test_validate_phase_dag_does_not_raise_on_unordered_sharers() -> None:
    """The warning is soft: validate_phase_dag still passes the phase."""
    phase = Phase(
        id="1",
        title="p",
        tasks=[
            _t("1.1", files=["src/api/serialize.py"]),
            _t("1.2", files=["src/api/serialize.py"]),
        ],
    )
    # Must NOT raise — shared-file-no-dep is a warning, not a hard failure.
    validate_phase_dag(phase)
