"""v0.33.0 Tier A integration test — plan-phase unblock.

Mirrors the v0.32 fixture shape: Task 1.1 creates ``notes_artifact.md``
with ``[new]``; Task 3.1 references the same file with no prefix. Pre-
v0.33.0 the validator would raise ``PathValidationError`` on Task 3.1
even though Task 1.1 is going to produce the file. v0.33.0 A1 unions
``files_new`` across the plan and admits Task 3.1's reference.

No real adapters / orchestrator dispatch here — the unit-test surface
exercises the validator end-to-end against a real git tree built on
``tmp_path``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from orchestrator.file_existence_validator import validate_files_exist
from state.schemas import AcceptanceCriterion, Phase, Plan, Task


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(repo), check=True
    )
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True)


def test_plan_with_cross_task_new_files_validates(tmp_path: Path) -> None:
    """Cross-task ``[new]`` reference passes the validator under v0.33.0."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "foo.py").write_text("# foo\n", encoding="utf-8")
    _git_init(tmp_path)

    task_creator = Task(
        id="1.1",
        phase_id="1",
        title="Produce notes artifact",
        description="Create the cross-task artifact.",
        files=["notes_artifact.md"],
        files_new=["notes_artifact.md"],
        acceptance=[
            AcceptanceCriterion(id="ac-1", description="file produced")
        ],
    )
    task_consumer = Task(
        id="3.1",
        phase_id="3",
        title="Consume notes artifact",
        description="Reference the artifact produced by Task 1.1.",
        files=["notes_artifact.md", "src/foo.py"],
        acceptance=[
            AcceptanceCriterion(id="ac-2", description="artifact consumed")
        ],
    )
    phase_one = Phase(
        id="1",
        title="Create",
        description="d",
        tasks=[task_creator],
        edit_scope=None,
    )
    phase_three = Phase(
        id="3",
        title="Consume",
        description="d",
        tasks=[task_consumer],
        edit_scope=None,
    )
    plan = Plan(
        plan_id="plan-v033-integration",
        spec_hash="cafebabe",
        phases=[phase_one, phase_three],
        edit_scope=[],
        created_at="2026-05-16T00:00:00+00:00",
        updated_at="2026-05-16T00:00:00+00:00",
    )

    resolutions: list[dict[str, str]] = []
    assert (
        validate_files_exist(plan, tmp_path, resolutions=resolutions) is None
    )
    # Task 3.1's reference is the only plan-global admission; Task 1.1
    # short-circuits via its own files_new.
    admitted = [r for r in resolutions if r["task_id"] == "3.1"]
    assert len(admitted) == 1
    assert admitted[0]["declaring_task_id"] == "1.1"
    assert admitted[0]["path"] == "notes_artifact.md"
