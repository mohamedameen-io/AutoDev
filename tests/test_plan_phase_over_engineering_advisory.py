"""Tests for the B1 over-engineering advisory in plan_phase.

``_advise_over_engineering`` must fire a ``plan_phase.over_engineering_advisory``
warning (and append a ledger entry) for two structural smells:

1. dependency_manifest — a task touches a known manifest file
   (requirements.txt, Cargo.toml, package.json, etc.)
2. new_file_bloat — a task creates >= 3 new files

Pattern follows ``tests/test_plan_phase_decomposition_advisory.py``.
"""

from __future__ import annotations

import pytest

from orchestrator.plan_phase import _advise_over_engineering
from state.schemas import Phase, Plan, Task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TIMESTAMP = "2026-06-17T00:00:00+00:00"


def _plan(tasks: list[Task]) -> Plan:
    """Wrap tasks in a minimal Phase/Plan."""
    phase = Phase(id="1", title="Phase 1", tasks=tasks)
    return Plan(
        plan_id="test-plan",
        spec_hash="abc123",
        phases=[phase],
        created_at=_TIMESTAMP,
        updated_at=_TIMESTAMP,
    )


def _task(
    task_id: str = "1.1",
    files: list[str] | None = None,
    files_new: list[str] | None = None,
) -> Task:
    return Task(
        id=task_id,
        phase_id="1",
        title="Test task",
        description="A test task.",
        files=files or [],
        files_new=files_new or [],
    )


def _orch_stub(ledger_ops: list) -> object:
    """Return a minimal orchestrator stub that collects ledger ops."""

    class FakePlanManager:
        async def ledger_append(self, *, op: str, payload: dict) -> None:
            ledger_ops.append((op, payload))

    return type(
        "OrchStub",
        (),
        {"plan_manager": FakePlanManager()},
    )()


# ---------------------------------------------------------------------------
# Smoke: import
# ---------------------------------------------------------------------------

def test_advise_over_engineering_is_importable() -> None:
    """The function must be importable from orchestrator.plan_phase."""
    from orchestrator.plan_phase import _advise_over_engineering  # noqa: F401


# ---------------------------------------------------------------------------
# Smell 1: dependency manifest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manifest_path",
    [
        "requirements.txt",
        "pyproject.toml",
        "Cargo.toml",
        "package.json",
        "go.mod",
        "Gemfile",
    ],
)
async def test_dependency_manifest_in_files_emits(manifest_path: str) -> None:
    """A task with a known manifest in files must emit a dependency_manifest op."""
    ledger_ops: list = []
    orch = _orch_stub(ledger_ops)
    plan = _plan([_task(files=["src/foo.py", manifest_path])])

    await _advise_over_engineering(orch, plan)

    emitted = [p for (op, p) in ledger_ops if op == "over_engineering_advisory"]
    assert len(emitted) >= 1, (
        f"Expected over_engineering_advisory ledger op for '{manifest_path}', "
        f"got: {ledger_ops}"
    )
    dep_ops = [p for p in emitted if p.get("smell") == "dependency_manifest"]
    assert dep_ops, f"Expected smell='dependency_manifest' in ops: {emitted}"
    assert dep_ops[0]["task_id"] == "1.1"
    assert dep_ops[0]["source"] == "planner_advisory"


@pytest.mark.asyncio
async def test_dependency_manifest_in_files_new_emits() -> None:
    """A task with a manifest in files_new must also emit."""
    ledger_ops: list = []
    orch = _orch_stub(ledger_ops)
    plan = _plan([_task(files_new=["requirements.txt"])])

    await _advise_over_engineering(orch, plan)

    dep_ops = [
        p
        for (op, p) in ledger_ops
        if op == "over_engineering_advisory" and p.get("smell") == "dependency_manifest"
    ]
    assert dep_ops, f"Expected dependency_manifest op, got: {ledger_ops}"


@pytest.mark.asyncio
async def test_dependency_manifest_payload_fields() -> None:
    """Ledger entry for dependency_manifest must carry correct fields."""
    ledger_ops: list = []
    orch = _orch_stub(ledger_ops)
    plan = _plan([_task(files=["requirements.txt"])])

    await _advise_over_engineering(orch, plan)

    dep_ops = [
        p
        for (op, p) in ledger_ops
        if op == "over_engineering_advisory" and p.get("smell") == "dependency_manifest"
    ]
    assert len(dep_ops) == 1
    payload = dep_ops[0]
    assert payload["task_id"] == "1.1"
    assert payload["source"] == "planner_advisory"
    assert payload["attempt"] == 0
    assert "manifests" in payload
    assert "requirements.txt" in payload["manifests"]


# ---------------------------------------------------------------------------
# Smell 2: new-file bloat
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_file_bloat_threshold_emits() -> None:
    """A task with >= 6 new files must emit a new_file_bloat op.

    Threshold is 6 (not 3) to avoid alert fatigue: a normal task with
    module + test + types = 3 new files fires at threshold=3.
    """
    ledger_ops: list = []
    orch = _orch_stub(ledger_ops)
    plan = _plan(
        [
            _task(
                files_new=[
                    "src/a.py",
                    "src/b.py",
                    "src/c.py",
                    "src/d.py",
                    "src/e.py",
                    "src/f.py",
                ]
            )
        ]
    )

    await _advise_over_engineering(orch, plan)

    bloat_ops = [
        p
        for (op, p) in ledger_ops
        if op == "over_engineering_advisory" and p.get("smell") == "new_file_bloat"
    ]
    assert bloat_ops, f"Expected new_file_bloat op, got: {ledger_ops}"
    assert bloat_ops[0]["task_id"] == "1.1"
    assert bloat_ops[0]["source"] == "planner_advisory"
    assert bloat_ops[0]["new_file_count"] == 6


@pytest.mark.asyncio
async def test_new_file_below_threshold_no_emit() -> None:
    """A task with < 6 new files and no manifest must produce no advisory op."""
    ledger_ops: list = []
    orch = _orch_stub(ledger_ops)
    plan = _plan([_task(files_new=["src/a.py", "src/b.py", "src/c.py", "src/d.py", "src/e.py"])])

    await _advise_over_engineering(orch, plan)

    assert not ledger_ops, (
        f"Unexpected ledger ops for 5 new files: {ledger_ops}"
    )


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clean_task_no_emit() -> None:
    """A task with no manifest and < 6 new files must produce no ledger op."""
    ledger_ops: list = []
    orch = _orch_stub(ledger_ops)
    plan = _plan([_task(files=["src/main.py"], files_new=["src/helper.py"])])

    await _advise_over_engineering(orch, plan)

    assert not ledger_ops


@pytest.mark.asyncio
async def test_ledger_failure_never_raises() -> None:
    """If ledger_append raises, _advise_over_engineering must not propagate."""

    class ExplodingPlanManager:
        async def ledger_append(self, *, op: str, payload: dict) -> None:
            raise RuntimeError("ledger exploded")

    orch = type("OrchStub", (), {"plan_manager": ExplodingPlanManager()})()
    plan = _plan([_task(files=["requirements.txt"])])

    # Must not raise
    await _advise_over_engineering(orch, plan)


@pytest.mark.asyncio
async def test_both_smells_produce_two_ops() -> None:
    """A task with both smells must produce two distinct ledger ops."""
    ledger_ops: list = []
    orch = _orch_stub(ledger_ops)
    plan = _plan(
        [
            _task(
                files=["requirements.txt"],
                files_new=[
                    "src/a.py",
                    "src/b.py",
                    "src/c.py",
                    "src/d.py",
                    "src/e.py",
                    "src/f.py",
                ],
            )
        ]
    )

    await _advise_over_engineering(orch, plan)

    advisory_ops = [p for (op, p) in ledger_ops if op == "over_engineering_advisory"]
    assert len(advisory_ops) == 2
    smells = {p["smell"] for p in advisory_ops}
    assert smells == {"dependency_manifest", "new_file_bloat"}


@pytest.mark.asyncio
async def test_plan_not_mutated() -> None:
    """_advise_over_engineering must never mutate the plan."""
    ledger_ops: list = []
    orch = _orch_stub(ledger_ops)
    task = _task(files=["requirements.txt"], files_new=["a.py", "b.py", "c.py"])
    plan = _plan([task])
    original_files = list(task.files)
    original_files_new = list(task.files_new)

    await _advise_over_engineering(orch, plan)

    assert task.files == original_files
    assert task.files_new == original_files_new
    assert len(plan.phases[0].tasks) == 1


# ---------------------------------------------------------------------------
# Regression: bare "requirements" directory segment must NOT fire
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        # A file nested inside a requirements/ directory — the bare segment
        # "requirements" must not match because there is no bare "requirements"
        # entry in _DEPENDENCY_MANIFEST_PATTERNS any more; only "requirements.txt"
        # (and the glob "requirements/*.txt") remain.
        "requirements/base.txt",
        "requirements/dev.txt",
        "src/requirements/constraints.txt",
        # A path whose basename is the bare word "requirements" (e.g. a dir entry)
        "requirements",
    ],
)
async def test_bare_requirements_segment_does_not_fire(path: str) -> None:
    """Paths that are *not* 'requirements.txt' must not produce a dependency_manifest op.

    Regression guard for the over-broad bare ``"requirements"`` pattern that
    was removed from ``_DEPENDENCY_MANIFEST_PATTERNS``.  A file inside a
    ``requirements/`` directory, or a bare directory entry named ``requirements``,
    must not trigger the advisory — only a file literally named
    ``requirements.txt`` (or a ``*.gemspec`` glob match, etc.) should.
    """
    ledger_ops: list = []
    orch = _orch_stub(ledger_ops)
    plan = _plan([_task(files=[path])])

    await _advise_over_engineering(orch, plan)

    dep_ops = [
        p
        for (op, p) in ledger_ops
        if op == "over_engineering_advisory" and p.get("smell") == "dependency_manifest"
    ]
    assert not dep_ops, (
        f"Path {path!r} should NOT trigger a dependency_manifest advisory, "
        f"but got: {dep_ops}"
    )
