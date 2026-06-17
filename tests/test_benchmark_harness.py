"""Field-synthesis harness fixes for the AutoDev benchmark (Phase 3).

These tests pin two scoring defects surfaced by the WS-4 field probes
(``thoughts/investigations/autodev-stabilization/field-probes/SYNTHESIS.md``):

  P1 — **committed-fix false-FAIL.** The runner extracted the agent's diff via
       ``git diff HEAD`` (plus a ledger extractor that returns nothing). When
       AutoDev *commits* its fix, the change is part of ``HEAD`` so
       ``git diff HEAD`` is empty → the scorer marks the task FAIL even though
       the bug was fixed. The fix scores by diffing the *initial* commit (the
       broken state, captured before the run) against the final worktree state,
       excluding ``.autodev/.claude/.cursor``.

  P5 — **vacuous refactor PASS.** Behaviour-preserving (refactor) tasks were
       scored by test exit code alone, so a no-op "passes". The fix requires a
       structural change (non-empty, non-excluded diff) in addition to the test
       passing for tasks flagged behaviour-preserving.

Each test states its RED-on-HEAD failure mode and is paired with a
broken-control assertion (reverting the fix re-introduces the bug).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from benchmarks.runner.run_benchmark import DEFAULT_TASKS_ROOT
from benchmarks.runner.scorer import score_task_with_patch
from benchmarks.runner.task_runner import (
    _SubprocessResult,
    run_task,
)


# ---------------------------------------------------------------------------
# Gate (a): a COMMITTED fix must score PASS (not the P1 false-FAIL).
# ---------------------------------------------------------------------------


def _commit_stub_factory(patch_text: str):
    """Build an autodev stub that *commits* the ground-truth fix on execute.

    This mimics the real field behaviour observed in P1: AutoDev applies its
    fix and ``git commit``s it, leaving no uncommitted (`git diff HEAD`)
    changes and no ledger ``diff`` payload. A HEAD-relative scorer sees nothing.
    """

    def stub(args, cwd, timeout):  # noqa: ANN001 - test stub signature
        if "execute" in args:
            subprocess.run(
                ["patch", "-p1", "--forward", "--silent"],
                input=patch_text,
                cwd=str(cwd),
                text=True,
                capture_output=True,
            )
            subprocess.run(["git", "add", "."], cwd=str(cwd), capture_output=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "fix"],
                cwd=str(cwd),
                capture_output=True,
            )
        return _SubprocessResult(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            elapsed_seconds=0.01,
        )

    return stub


def test_committed_fix_is_scored_pass(tmp_path: Path):
    """A fix that AutoDev *commits* must be detected and scored PASS.

    RED-on-HEAD: the runner's HEAD-relative diff (``git diff HEAD``) is empty
    for a committed fix and the ledger has no ``diff`` payload, so ``agent_diff``
    is empty → the scorer returns ``apply_error='empty diff'`` and status FAIL.
    GREEN: the runner captures the broken initial commit before the run and
    diffs initial→final, so the committed fix is recovered.
    """
    task_dir = DEFAULT_TASKS_ROOT / "task_001_py_typeerror"
    gt_patch = (task_dir / "ground_truth.patch").read_text(encoding="utf-8")

    result = run_task(
        task_dir,
        autodev_invoker=_commit_stub_factory(gt_patch),
        workdir_root=tmp_path,
    )

    assert result.status == "PASS", (
        "committed fix false-FAILed (P1): "
        f"status={result.status} apply_error={result.apply_error} "
        f"diff_size={result.secondary.get('diff_size_lines')} "
        f"stderr={result.stderr_tail}"
    )
    # The recovered diff must be non-empty — i.e. we actually saw the change.
    assert result.secondary["diff_size_lines"] > 0
    assert result.apply_error is None


def test_uncommitted_fix_still_scored_pass(tmp_path: Path):
    """Regression guard: the prior (uncommitted) path must keep working.

    The ledger-then-working-tree path used by the existing suite must not
    regress when we add the initial→final commit diff.
    """
    task_dir = DEFAULT_TASKS_ROOT / "task_001_py_typeerror"
    gt_patch = (task_dir / "ground_truth.patch").read_text(encoding="utf-8")

    def stub(args, cwd, timeout):  # noqa: ANN001 - test stub signature
        if "execute" in args:
            # Apply the fix but DO NOT commit — leave it in the worktree.
            subprocess.run(
                ["patch", "-p1", "--forward", "--silent"],
                input=gt_patch,
                cwd=str(cwd),
                text=True,
                capture_output=True,
            )
        return _SubprocessResult(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            elapsed_seconds=0.01,
        )

    result = run_task(task_dir, autodev_invoker=stub, workdir_root=tmp_path)
    assert result.status == "PASS", result.error or result.stderr_tail
    assert result.secondary["diff_size_lines"] > 0


def test_committed_noise_only_in_excluded_dirs_is_not_a_fix(tmp_path: Path):
    """Broken-control for gate (a): a commit that ONLY touches excluded dirs
    (``.autodev``/``.claude``/``.cursor``) must NOT be mistaken for a fix.

    Without the exclusion, the initial→final diff would be non-empty (autodev
    scaffolding) and the patch-apply scorer would try to apply scaffolding as a
    fix — masking the real no-op as a PASS. With exclusion, the recovered diff
    is empty → FAIL.
    """
    task_dir = DEFAULT_TASKS_ROOT / "task_001_py_typeerror"

    def stub(args, cwd, timeout):  # noqa: ANN001 - test stub signature
        if "execute" in args:
            # Write scaffolding into an excluded dir and commit it — but never
            # touch the actual source. The bug stays unfixed.
            scratch = cwd / ".autodev"
            scratch.mkdir(parents=True, exist_ok=True)
            (scratch / "plan-ledger.jsonl").write_text("{}\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=str(cwd), capture_output=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "scaffold"],
                cwd=str(cwd),
                capture_output=True,
            )
        return _SubprocessResult(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            elapsed_seconds=0.01,
        )

    result = run_task(task_dir, autodev_invoker=stub, workdir_root=tmp_path)
    assert result.status == "FAIL", (
        "scaffolding-only commit was mistaken for a fix: "
        f"status={result.status} diff_size={result.secondary.get('diff_size_lines')}"
    )
    assert result.secondary["diff_size_lines"] == 0


# ---------------------------------------------------------------------------
# Gate (b): an EMPTY diff on a behaviour-preserving (refactor) task must FAIL.
# ---------------------------------------------------------------------------


def _make_refactor_task(root: Path) -> Path:
    """Create a behaviour-preserving task whose test passes WITHOUT any change.

    The repo's ``test_command.sh`` exits 0 on the original source, so a no-op
    "refactor" passes the test. The only thing that distinguishes a real
    refactor from a no-op is a structural change to the source.
    """
    task = root / "task_refactor"
    repo = task / "repo"
    repo.mkdir(parents=True)
    (repo / "mod.py").write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    (repo / "test_mod.py").write_text(
        "from mod import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (task / "test_command.sh").write_text(
        '#!/bin/bash\nset -e\ncd "$(dirname "$0")/repo"\n'
        "python -m pytest test_mod.py -q\n",
        encoding="utf-8",
    )
    return task


def test_refactor_empty_diff_is_scored_fail(tmp_path: Path):
    """A behaviour-preserving task with an EMPTY diff must FAIL, not pass.

    RED-on-HEAD: ``score_task_with_patch`` has no structural-change concept; an
    empty patch on a task whose test already passes returns ``passed=True``
    (vacuous PASS, the P5 defect). With ``require_structural_change=True`` an
    empty diff is rejected before the test even runs.
    """
    task = _make_refactor_task(tmp_path / "tasks")
    workdir = tmp_path / "work"
    workdir.mkdir()

    result = score_task_with_patch(
        task,
        "",  # empty diff: a no-op "refactor"
        workdir=workdir,
        require_structural_change=True,
    )
    assert not result.passed, (
        "empty refactor diff vacuously PASSed (P5): "
        f"passed={result.passed} apply_error={result.apply_error}"
    )
    # The structural-change gate, not the test, is what failed it.
    assert result.structural_change is False


def test_refactor_noop_nonempty_diff_is_scored_fail(tmp_path: Path):
    """The *dangerous* vacuous case: a NON-empty diff that nets to no change.

    A strictly-empty diff is already caught by the empty-diff sentinel — but the
    P5 field defect is a refactor that "did nothing" while still passing the
    test. Here the agent rewrites mod.py to byte-identical content (a context-
    only patch). It is non-empty (so it slips past the empty-diff sentinel) and
    applies cleanly, yet the patched tree equals the source → must FAIL.

    RED-on-HEAD: with test-only scoring this passes (test exits 0). GREEN: the
    post-apply tree comparison sees no net change and fails it.
    """
    task = _make_refactor_task(tmp_path / "tasks")
    workdir = tmp_path / "work"
    workdir.mkdir()

    # A patch whose hunk replaces the file with byte-identical content.
    noop_patch = (
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-def add(a, b):\n"
        "-    return a + b\n"
        "+def add(a, b):\n"
        "+    return a + b\n"
    )
    result = score_task_with_patch(
        task,
        noop_patch,
        workdir=workdir,
        require_structural_change=True,
    )
    assert not result.passed, (
        "non-empty no-op refactor vacuously PASSed (P5): "
        f"passed={result.passed} apply_error={result.apply_error} "
        f"structural_change={result.structural_change}"
    )
    assert result.structural_change is False


def test_refactor_real_diff_passes(tmp_path: Path):
    """Broken-control / positive case: a REAL behaviour-preserving change that
    still passes the test must score PASS.

    Reverting the structural-change gate would let the empty diff pass too — so
    this asserts the gate is non-vacuous (it accepts a genuine refactor while
    rejecting the no-op above).
    """
    task = _make_refactor_task(tmp_path / "tasks")
    workdir = tmp_path / "work"
    workdir.mkdir()

    # A genuine, behaviour-preserving refactor of mod.py (sum vs +).
    patch_text = (
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-def add(a, b):\n"
        "-    return a + b\n"
        "+def add(a, b):\n"
        "+    return sum((a, b))\n"
    )
    result = score_task_with_patch(
        task,
        patch_text,
        workdir=workdir,
        require_structural_change=True,
    )
    assert result.passed, (
        f"genuine refactor was rejected: apply_error={result.apply_error} "
        f"exit={result.exit_code} stdout={result.stdout_tail} "
        f"stderr={result.stderr_tail}"
    )
    assert result.structural_change is True


def test_non_refactor_default_does_not_require_structural_change(tmp_path: Path):
    """Default behaviour (require_structural_change=False) is unchanged.

    A normal bugfix task is gated by the test, not by a structural-change
    assertion, so the existing scoring contract is preserved.
    """
    task = _make_refactor_task(tmp_path / "tasks")
    workdir = tmp_path / "work"
    workdir.mkdir()

    # Empty diff, but the test passes — without the refactor gate this is PASS,
    # matching the pre-existing (non-refactor) contract.
    result = score_task_with_patch(task, "", workdir=workdir)
    # Empty diff is still rejected by the existing empty-diff sentinel for the
    # default path (apply fails), so this stays FAIL — but for the *empty-diff*
    # reason, not the structural-change reason.
    assert result.structural_change is None
    assert result.apply_error == "empty diff"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
