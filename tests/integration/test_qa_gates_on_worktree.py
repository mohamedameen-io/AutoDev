"""G3 (WS2-5): QA gates must verify the CHANGED tree (worktree), not the
pre-change MAIN repo.

DEFECT under test
-----------------
``_run_qa_gates`` bound ``cwd = orch.cwd`` unconditionally — the pre-change
MAIN repo. But in the parallel/serial execute path the developer's diff is
materialized in a per-task *worktree* (``cwd_override=worktree`` for every
delegate call). So syntax/lint/build/test gates scanned a CLEAN main tree
and PASSED even when the worktree diff was broken — a silent wrong-pass.

These tests are resolver-agnostic: they drive ``_run_qa_gates`` directly
with an ``OrchStub`` whose ``cwd`` is the clean MAIN repo, plus a real
``WorktreeManager`` + per-task worktree carrying a SYNTAX ERROR. The gate
must fail BECAUSE it scanned the broken worktree.

  * G3 (RED-on-HEAD):  syntax error lives ONLY in the worktree → the gate
    must return passed=False. On HEAD (cwd=orch.cwd) it vacuously PASSES
    because main is clean — that is the bug. This test FAILS on HEAD.
  * NON-VACUITY CONTROL: prove main is genuinely clean (so a pass cannot
    come from main being broken) AND prove the worktree is genuinely broken
    (so a fail cannot come from the worktree accidentally being clean).
  * BROKEN-CONTROL: reverting the cwd-threading (gate sees orch.cwd again)
    makes the broken worktree PASS → asserts the test goes red, i.e. the
    test actually exercises the fix and is not itself vacuous.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from adapters.types import AgentResult
from orchestrator import execute_phase as ep
from orchestrator.worktree import WorktreeManager


# ── helpers ───────────────────────────────────────────────────────────────


async def _git(cwd: Path, *args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): "
            f"{err.decode().strip() or out.decode().strip()}"
        )


async def _init_clean_main_repo(root: Path) -> None:
    """A git repo whose only .py file is SYNTACTICALLY VALID.

    A ``pyproject.toml`` is committed so ``detect_language`` resolves
    ``"python"`` (the real repos AutoDev runs against always carry a
    manifest); without it the syntax gate skips with "language not
    detected" and the cwd defect would be masked by the skip rather
    than caught.
    """
    root.mkdir(parents=True, exist_ok=True)
    await _git(root, "init", "-q")
    await _git(root, "config", "user.email", "t@t.t")
    await _git(root, "config", "user.name", "t")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "fixture"\nversion = "0.0.0"\n'
    )
    (root / "mod.py").write_text("def ok():\n    return 1\n")
    await _git(root, "add", "-A")
    await _git(root, "commit", "-q", "-m", "clean main")


def _qa_cfg_only_syntax() -> object:
    """A cfg with ONLY syntax_check enabled — isolates the cwd defect.

    Everything else (lint/build/test/secretscan/hallucination/mutation/
    code_size/diagnosis gates) is OFF so the only signal is the syntax
    gate, and the only thing that changes its verdict is WHICH tree it
    scans.
    """

    class QAGates:
        syntax_check = True
        lint = False
        build_check = False
        test_runner = False
        secretscan = False
        secretscan_baseline_enabled = False
        secretscan_per_extension_thresholds = None
        secretscan_auto_skip_huge_repo = True
        secretscan_force_run_on_huge_repo = False
        mutation_test_enabled = False
        mutation_test_threshold = 0.7
        code_size = False
        lint_timeout_s = 120.0
        test_timeout_s = 600.0
        build_check_timeout_s = 120.0  # WS2-11: build-gate timeout knob

    class Cfg:
        qa_gates = QAGates()
        hallucination_guard = False
        diagnosis = None  # diagnosis gates disabled

    return Cfg()


def _orch_stub(main_repo: Path) -> object:
    """An Orchestrator stand-in whose cwd is the CLEAN main repo."""
    return type(
        "OrchStub",
        (),
        {
            "cfg": _qa_cfg_only_syntax(),
            "cwd": main_repo,
            "plugin_registry": None,
            "_repo_capacity": None,
        },
    )()


class _FakeTask:
    id = "1.1"
    produces_diff = True
    metadata: dict = {}


# ── G3 + non-vacuity control ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_qa_gates_scan_worktree_not_clean_main(tmp_path: Path) -> None:
    """G3: a syntax error written ONLY to the worktree must fail the gate.

    RED-on-HEAD: today ``_run_qa_gates`` scans ``orch.cwd`` (clean main),
    so it returns None (pass) even though the worktree is broken. After the
    fix the gate scans the worktree and returns a non-None failure detail.
    """
    main_repo = tmp_path / "main"
    await _init_clean_main_repo(main_repo)

    mgr = WorktreeManager(
        main_repo=main_repo,
        tournament_dir=tmp_path / "wt",
        autodev_root=tmp_path / "autodev_root",
    )
    worktree = await mgr.create_per_task("1.1")
    try:
        # Break ONLY the worktree copy.
        (worktree / "mod.py").write_text("def broken(\n    return 1\n")

        # --- non-vacuity guards: prove the premise is real -------------------
        # The gate auto-detects language from a manifest (no forced lang), so
        # the premise checks mirror that path exactly.
        from qa.detect import detect_language
        from qa.syntax_check import run_syntax_check

        assert detect_language(main_repo) == "python"
        assert detect_language(worktree) == "python", (
            "premise broken: language not detected in worktree → the syntax "
            "gate would SKIP, masking the cwd defect instead of catching it"
        )
        # main is genuinely clean ⇒ a PASS cannot be "because main was broke".
        main_only = await run_syntax_check(main_repo)
        assert main_only.passed is True, (
            "premise broken: main repo is NOT syntactically clean; "
            "the G3 RED signal would be ambiguous"
        )
        # worktree is genuinely broken ⇒ a FAIL cannot be "because worktree "
        # "was accidentally clean".
        wt_only = await run_syntax_check(worktree)
        assert wt_only.passed is False, (
            "premise broken: worktree copy is NOT broken; the test would "
            "vacuously pass"
        )

        # --- the gate under test --------------------------------------------
        # WS8: syntax_check is now diff-scoped like every sibling gate, so a
        # developer_result whose diff touches ``mod.py`` is required to steer
        # the scan — mirroring how ``_run_qa_gates`` is actually invoked in
        # production (always with the developer's AgentResult). This still
        # isolates the G3 concern: the diff path is resolved relative to
        # ``cwd`` (the worktree, via cwd_override), so the test still
        # discriminates "which tree did the gate scan" — it would resolve to
        # the clean main's ``mod.py`` if cwd routing regressed.
        developer_result = AgentResult(
            text="ok", success=True, duration_s=0.1, diff="+++ b/mod.py\n"
        )
        out = await ep._run_qa_gates(
            _orch_stub(main_repo),
            _FakeTask(),
            developer_result=developer_result,
            cwd_override=worktree,
        )
        assert out is not None, (
            "QA gate vacuously PASSED: it scanned the clean MAIN repo "
            "(orch.cwd) instead of the broken worktree. This is the "
            "silent wrong-pass G3 fixes."
        )
        assert "syntax" in out.lower() or "py_compile" in out.lower(), (
            f"gate failed for the wrong reason: {out!r}"
        )
    finally:
        await mgr.remove_per_task("1.1")


@pytest.mark.asyncio
async def test_qa_gates_broken_control_reverting_cwd_passes_on_broken_worktree(
    tmp_path: Path,
) -> None:
    """BROKEN-CONTROL: simulate reverting the fix (gate sees orch.cwd again).

    With the cwd-override NOT supplied, ``_run_qa_gates`` falls back to
    ``orch.cwd`` (clean main). The broken worktree is then never scanned and
    the gate PASSES — exactly the silent wrong-pass we are fixing. This
    asserts the fix is load-bearing: without the override the bug returns.
    """
    main_repo = tmp_path / "main"
    await _init_clean_main_repo(main_repo)

    mgr = WorktreeManager(
        main_repo=main_repo,
        tournament_dir=tmp_path / "wt",
        autodev_root=tmp_path / "autodev_root",
    )
    worktree = await mgr.create_per_task("1.1")
    try:
        (worktree / "mod.py").write_text("def broken(\n    return 1\n")

        # No cwd_override → legacy behavior → scans clean main → PASS.
        out = await ep._run_qa_gates(_orch_stub(main_repo), _FakeTask())
        assert out is None, (
            "control sanity: without cwd_override the gate must still scan "
            "orch.cwd (clean main) and pass — proving the override is the "
            "thing that makes G3 catch the broken worktree"
        )
    finally:
        await mgr.remove_per_task("1.1")
