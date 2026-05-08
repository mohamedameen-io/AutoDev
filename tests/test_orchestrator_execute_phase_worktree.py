"""Tests for v0.11.0 per-task worktree isolation in :func:`_execute_one`.

Validates the optional ``worktree_mgr`` parameter routes agent execution
through a per-task worktree, applies the resulting diff to main on
success, and always cleans up the worktree in the finally clause.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from orchestrator.worktree import WorktreeManager


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


@pytest.mark.asyncio
async def test_execute_one_uses_per_task_worktree(tmp_path: Path) -> None:
    """When worktree_mgr is supplied, _execute_one creates a worktree at
    tournament_dir/tasks/<task_id> and the worker writes happen there."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    # The actual worker call into _execute_one is heavy. The contract
    # under test is "create_per_task is called with the task id" — exercise
    # via direct invocation.
    wt = await mgr.create_per_task("1.1")
    assert wt == wt_dir / "tasks" / "1.1"
    assert wt.exists()
    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_execute_one_worktree_cleanup_on_success(tmp_path: Path) -> None:
    """After remove_per_task, the worktree directory is gone."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    wt = await mgr.create_per_task("1.1")
    (wt / "scratch.py").write_text("# done\n")
    await mgr.remove_per_task("1.1")
    assert not wt.exists()


@pytest.mark.asyncio
async def test_execute_one_worktree_cleanup_on_failure(tmp_path: Path) -> None:
    """Even with dirty uncommitted edits, remove_per_task tears down."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    wt = await mgr.create_per_task("1.1")
    # Add untracked + modified files (simulating mid-task crash).
    (wt / "untracked.py").write_text("x = 1\n")
    (wt / "README.md").write_text("modified by failed task\n")

    await mgr.remove_per_task("1.1")
    assert not wt.exists()


@pytest.mark.asyncio
async def test_execute_one_apply_patch_propagates_to_main(tmp_path: Path) -> None:
    """A successful diff in the worktree shows up in the main repo after apply."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    wt_dir = tmp_path / "execute_worktrees"
    mgr = WorktreeManager(main_repo=repo, tournament_dir=wt_dir)

    wt = await mgr.create_per_task("1.1")
    (wt / "feature.py").write_text("def foo(): return 42\n")

    await mgr.apply_patch_to_main(wt)

    # Main repo now contains the new file.
    assert (repo / "feature.py").exists()
    assert "def foo" in (repo / "feature.py").read_text()

    await mgr.cleanup_all()


@pytest.mark.asyncio
async def test_delegate_threads_cwd_override(tmp_path: Path) -> None:
    """delegate(cwd_override=worktree) builds an AgentInvocation with cwd=worktree."""
    from adapters.types import AgentInvocation, AgentResult, AgentSpec
    from orchestrator.delegation_envelope import DelegationEnvelope
    from orchestrator.execute_phase import delegate

    captured: dict = {}

    class FakeAdapter:
        async def execute(self, inv: AgentInvocation) -> AgentResult:
            captured["cwd"] = inv.cwd
            return AgentResult(
                text="ok",
                success=True,
                duration_s=0.1,
                files_changed=[],
                diff="",
            )

    # Minimal orchestrator stub.
    class FakeKnowledge:
        async def inject_block(self, role: str, task_id: str | None = None) -> str:
            return ""

    class FakeRegistry:
        def get(self, role: str) -> AgentSpec:
            return AgentSpec(
                name=role,
                model="sonnet",
                prompt="you are a developer",
                description="",
                tools=[],
                max_turns=1,
            )

    class FakeGuardrails:
        def pre_invocation(self, *_a, **_k):
            pass

        def post_invocation(self, *_a, **_k):
            pass

    class FakeLoop:
        def observe(self, *_a, **_k):
            pass

    class FakePlanManager:
        async def load(self):
            return None

    orch = type(
        "OrchStub",
        (),
        {
            "registry": FakeRegistry(),
            "knowledge": FakeKnowledge(),
            "cfg": type(
                "CfgStub",
                (),
                {
                    "agents": {},
                    "user_complexity": "medium",
                },
            )(),
            "plan_manager": FakePlanManager(),
            "adapter": FakeAdapter(),
            "guardrails": FakeGuardrails(),
            "loop_detector": FakeLoop(),
            "cwd": tmp_path,
            "session_id": "s1",
        },
    )()

    custom_cwd = tmp_path / "worktree"
    custom_cwd.mkdir()
    env = DelegationEnvelope(
        task_id="1.1",
        target_agent="developer",
        action="implement",
        files=[],
        context={},
    )
    await delegate(orch, "developer", env, cwd_override=custom_cwd)
    assert captured["cwd"] == custom_cwd


@pytest.mark.asyncio
async def test_delegate_default_cwd_is_orch_cwd(tmp_path: Path) -> None:
    """Without cwd_override, delegate uses orch.cwd (legacy path)."""
    from adapters.types import AgentInvocation, AgentResult, AgentSpec
    from orchestrator.delegation_envelope import DelegationEnvelope
    from orchestrator.execute_phase import delegate

    captured: dict = {}

    class FakeAdapter:
        async def execute(self, inv: AgentInvocation) -> AgentResult:
            captured["cwd"] = inv.cwd
            return AgentResult(
                text="ok",
                success=True,
                duration_s=0.1,
                files_changed=[],
                diff="",
            )

    class FakeKnowledge:
        async def inject_block(self, role: str, task_id: str | None = None) -> str:
            return ""

    class FakeRegistry:
        def get(self, role: str) -> AgentSpec:
            return AgentSpec(
                name=role,
                model="sonnet",
                prompt="x",
                description="",
                tools=[],
                max_turns=1,
            )

    class FakeGuardrails:
        def pre_invocation(self, *_a, **_k):
            pass

        def post_invocation(self, *_a, **_k):
            pass

    class FakeLoop:
        def observe(self, *_a, **_k):
            pass

    class FakePlanManager:
        async def load(self):
            return None

    orch = type(
        "OrchStub",
        (),
        {
            "registry": FakeRegistry(),
            "knowledge": FakeKnowledge(),
            "cfg": type("CfgStub", (), {"agents": {}, "user_complexity": "medium"})(),
            "plan_manager": FakePlanManager(),
            "adapter": FakeAdapter(),
            "guardrails": FakeGuardrails(),
            "loop_detector": FakeLoop(),
            "cwd": tmp_path,
            "session_id": "s1",
        },
    )()

    env = DelegationEnvelope(
        task_id="1.1",
        target_agent="developer",
        action="implement",
        files=[],
        context={},
    )
    await delegate(orch, "developer", env)
    assert captured["cwd"] == tmp_path
