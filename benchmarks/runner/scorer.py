"""Scoring helpers for the AutoDev real-task benchmark.

The benchmark is scored by *behaviour*, not by diff text:
  1. Reset the task repo to its broken initial state.
  2. Apply the agent's diff (or the ground-truth diff, in tests).
  3. Run ``test_command.sh``.
  4. Exit code 0 = PASS, anything else = FAIL.

This module also reads results.json files for cross-release comparison and
exposes helpers used by both ``run_benchmark`` and the unit tests.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


# ---------------------------------------------------------------------------
# Patch application
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatchApplyResult:
    """Outcome of applying a unified diff to a repo directory."""

    applied: bool
    error: str | None
    stderr: str


def apply_patch_to_repo(repo_dir: Path, patch_text: str) -> PatchApplyResult:
    """Apply a unified diff to ``repo_dir`` using GNU/BSD ``patch -p1``.

    An empty ``patch_text`` is treated as an explicit "no changes" — the
    function returns ``applied=False`` with a sentinel error so the scorer
    can mark the task as FAIL without crashing.
    """
    if not patch_text or not patch_text.strip():
        return PatchApplyResult(applied=False, error="empty diff", stderr="")

    proc = subprocess.run(
        ["patch", "-p1", "--forward", "--silent"],
        input=patch_text,
        cwd=str(repo_dir),
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return PatchApplyResult(
            applied=False,
            error=f"patch -p1 exited {proc.returncode}",
            stderr=proc.stderr,
        )
    return PatchApplyResult(applied=True, error=None, stderr=proc.stderr)


# ---------------------------------------------------------------------------
# Per-task scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreResult:
    """Outcome of running a task's ``test_command.sh`` after a patch."""

    passed: bool
    exit_code: int
    apply_error: str | None
    stdout_tail: str
    stderr_tail: str


def _tail(text: str, max_chars: int = 4000) -> str:
    if not text or len(text) <= max_chars:
        return text
    return "...<truncated>...\n" + text[-max_chars:]


def score_task_with_patch(
    task_dir: Path,
    patch_text: str,
    *,
    workdir: Path,
    test_timeout_seconds: int = 120,
) -> ScoreResult:
    """Reset the task repo into ``workdir`` and verify ``patch_text``.

    Layout used:
      ``workdir/repo/`` — fresh copy of ``task_dir/repo/`` with the patch
      applied (if applicable). The caller is responsible for cleaning up
      ``workdir``.
    """
    src_repo = task_dir / "repo"
    if not src_repo.is_dir():
        raise FileNotFoundError(f"task repo missing: {src_repo}")

    fresh_repo = workdir / "repo"
    if fresh_repo.exists():
        shutil.rmtree(fresh_repo)
    shutil.copytree(src_repo, fresh_repo)

    apply = apply_patch_to_repo(fresh_repo, patch_text)
    if not apply.applied:
        return ScoreResult(
            passed=False,
            exit_code=-1,
            apply_error=apply.error,
            stdout_tail="",
            stderr_tail=_tail(apply.stderr),
        )

    test_cmd = task_dir / "test_command.sh"
    if not test_cmd.is_file():
        raise FileNotFoundError(f"task test_command.sh missing: {test_cmd}")

    # Stage the test script next to the patched repo so the canonical
    # `cd "$(dirname "$0")/repo"` line in test_command.sh lands in our
    # patched copy, not the original on-disk task_dir/repo.
    staged_test_cmd = workdir / "test_command.sh"
    shutil.copy2(test_cmd, staged_test_cmd)

    try:
        proc = subprocess.run(
            ["bash", str(staged_test_cmd)],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=test_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return ScoreResult(
            passed=False,
            exit_code=-2,
            apply_error=f"test_command timed out after {test_timeout_seconds}s",
            stdout_tail=_tail(exc.stdout or ""),
            stderr_tail=_tail(exc.stderr or ""),
        )

    return ScoreResult(
        passed=proc.returncode == 0,
        exit_code=proc.returncode,
        apply_error=None,
        stdout_tail=_tail(proc.stdout),
        stderr_tail=_tail(proc.stderr),
    )


# ---------------------------------------------------------------------------
# Diff extraction from a ledger file
# ---------------------------------------------------------------------------


def extract_diff_from_ledger(ledger_path: Path) -> str:
    """Return the most recent ``diff`` payload found in an autodev ledger.

    The autodev ledger (``.autodev/plan-ledger.jsonl``) is one JSON object per
    line. We accept any of the conventions seen in autodev's history:

    - top-level ``"diff"`` field;
    - top-level ``"final_diff"`` field;
    - ``"event": "execute_diff"`` with a ``"diff"`` payload;
    - ``"op": "git_diff"`` with a ``"diff"`` payload.

    The *last* matching record wins (most-recent diff). Returns ``""`` if
    nothing matches or the file does not exist.
    """
    if not ledger_path.is_file():
        return ""

    last_diff: str = ""
    with ledger_path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            for key in ("diff", "final_diff"):
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    last_diff = value
            payload = obj.get("payload")
            if isinstance(payload, dict):
                value = payload.get("diff")
                if isinstance(value, str) and value.strip():
                    last_diff = value
    return last_diff


# ---------------------------------------------------------------------------
# Cross-release comparison
# ---------------------------------------------------------------------------


def _summary_pass_rate(summary: dict) -> float:
    total = int(summary.get("total", 0) or 0)
    passed = int(summary.get("passed", 0) or 0)
    if total == 0:
        return 0.0
    return passed / total


def score_benchmark_results(
    current: dict,
    baseline: dict | None = None,
    *,
    pass_rate_drop_threshold: float = 0.10,
) -> dict:
    """Compare a fresh ``results.json`` against an optional baseline.

    Returns a dict with the keys:
      ``pass_rate``, ``baseline_pass_rate``, ``pass_rate_delta``,
      ``regressed`` (True if drop > threshold), ``per_task`` (list of
      ``{task_id, status, baseline_status, regressed}``).
    """
    current_summary = current.get("summary", {})
    pass_rate = _summary_pass_rate(current_summary)

    baseline_pass_rate = (
        _summary_pass_rate(baseline.get("summary", {})) if baseline else None
    )
    delta = (
        pass_rate - baseline_pass_rate
        if baseline_pass_rate is not None
        else None
    )
    regressed = (
        delta is not None and delta < -abs(pass_rate_drop_threshold)
    )

    per_task: list[dict] = []
    baseline_by_id: dict[str, str] = {}
    if baseline:
        for entry in baseline.get("results", []):
            tid = entry.get("task_id")
            if tid:
                baseline_by_id[tid] = entry.get("status", "UNKNOWN")
    for entry in current.get("results", []):
        tid = entry.get("task_id", "")
        status = entry.get("status", "UNKNOWN")
        b_status = baseline_by_id.get(tid)
        per_task.append(
            {
                "task_id": tid,
                "status": status,
                "baseline_status": b_status,
                "regressed": (
                    b_status == "PASS" and status != "PASS"
                    if b_status is not None
                    else False
                ),
            }
        )

    return {
        "pass_rate": pass_rate,
        "baseline_pass_rate": baseline_pass_rate,
        "pass_rate_delta": delta,
        "regressed": regressed,
        "per_task": per_task,
    }


def load_results(path: Path | str) -> dict:
    """Load a results.json artefact from disk."""
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8"))


def iter_task_dirs(tasks_root: Path) -> Iterable[Path]:
    """Yield each task directory under ``tasks_root`` in sorted order."""
    if not tasks_root.is_dir():
        return
    for child in sorted(tasks_root.iterdir()):
        if child.is_dir() and (child / "meta.json").is_file():
            yield child
