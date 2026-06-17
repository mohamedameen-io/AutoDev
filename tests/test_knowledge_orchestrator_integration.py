"""End-to-end: orchestrator records a lesson, next task's delegation sees it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from config.schema import QAGatesConfig
from orchestrator import Orchestrator

from stub_adapter import StubAdapter, ok


PLAN_MD = """
# Plan: Two small tasks

## Phase 1: Implement

### Task 1.1: First task
  - Description: First step
  - Files: math.py
  - Acceptance:
    - [ ] done

### Task 1.2: Second task
  - Description: Second step
  - Files: [new] util.py
  - Acceptance:
    - [ ] done
"""


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    (repo / "math.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=str(repo), check=True)


def _coder_ok() -> AgentResult:
    return AgentResult(
        success=True,
        text="done",
        diff=(
            "diff --git a/math.py b/math.py\n"
            "--- a/math.py\n"
            "+++ b/math.py\n"
            "@@ -0,0 +1 @@\n"
            "+def noop(): pass\n"
        ),
        files_changed=[Path("math.py")],
        duration_s=0.01,
    )


@pytest.mark.asyncio
async def test_lesson_recorded_by_store_is_injected_into_next_coder_call(
    tmp_path: Path,
) -> None:
    """After we record a lesson, the next coder invocation receives it in-prompt."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    cfg = default_config()
    cfg.platform = "claude_code"
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    # Keep hive isolated to tmp so we don't touch the user's real hive.
    cfg.hive.path = tmp_path / "hive.jsonl"
    # Dial down promotion thresholds so we don't inadvertently promote.
    cfg.knowledge.promotion_min_confirmations = 99
    # This test asserts the lesson→coder-prompt injection happens on the FIRST
    # (and only) developer call. The repo is a manifest-less python repo (just
    # ``math.py``), which the weighted ``qa/detect.py`` now correctly classifies
    # as python — so the real ``qa/test_runner.py`` gate would run pytest, find
    # 0 tests (the StubAdapter never materializes a test file in the worktree),
    # fail loud via Phase-1B's WS2-2 non-vacuity check, and trigger retries —
    # making the developer be invoked >1 time and breaking the single-injection
    # assertion. That gate is correct product behavior and has dedicated
    # coverage in ``tests/test_qa_test_runner.py``; this test is about the
    # lesson-injection flow, so disable the environment-tooling gates (mirrors
    # ``tests/integration/test_e2e_pipeline_full.py``).
    cfg.qa_gates = QAGatesConfig(
        syntax_check=False,
        lint=False,
        build_check=False,
        test_runner=False,
        secretscan=False,
        sast_scan=False,
        mutation_test=False,
    )

    registry = build_registry(cfg)
    adapter = StubAdapter(
        {
            "explorer": ok("nothing interesting"),
            "domain_expert": ok("no domain concerns"),
            "architect": ok(PLAN_MD),
            "developer": _coder_ok(),
            "reviewer": ok("APPROVED"),
            "test_engineer": ok("RESULTS: passed=0 failed=0 total=0"),
        }
    )

    orch = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="kn-int-1",
    )
    await orch.plan("trivial spec")

    # Seed a lesson before any task runs.
    recorded = await orch.knowledge.record(
        "always prefer atomic tmp-then-rename writes — never write in place",
        role_source="developer",
        confidence=0.6,
    )
    assert recorded is not None

    # Run the first task; its coder prompt must include the lesson block.
    await orch.execute(task_id="1.1")

    coder_prompts = adapter.prompts_for("developer")
    assert len(coder_prompts) == 1
    assert "Lessons learned from prior work:" in coder_prompts[0]
    assert "atomic tmp-then-rename" in coder_prompts[0]


@pytest.mark.asyncio
async def test_explorer_never_sees_lessons(tmp_path: Path) -> None:
    """Denylisted roles (explorer) must not receive the lessons block."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)

    cfg = default_config()
    cfg.platform = "claude_code"
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.hive.path = tmp_path / "hive.jsonl"

    registry = build_registry(cfg)
    adapter = StubAdapter(
        {
            "explorer": ok("no findings"),
            "domain_expert": ok("no concerns"),
            "architect": ok(PLAN_MD),
        }
    )
    orch = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="kn-int-2",
    )

    # Seed a lesson FIRST so if it were injected, it would appear.
    await orch.knowledge.record(
        "this lesson must never reach a denylisted role",
        role_source="developer",
        confidence=0.9,
    )
    await orch.plan("trivial spec")

    explorer_prompts = adapter.prompts_for("explorer")
    judge_prompts = adapter.prompts_for("judge")
    assert len(explorer_prompts) == 1
    for p in explorer_prompts:
        assert "Lessons learned from prior work:" not in p
    for p in judge_prompts:
        assert "Lessons learned from prior work:" not in p
