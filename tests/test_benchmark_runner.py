"""Tests for the benchmark runner / scorer (Phase 7 of v0.32.0).

These tests verify the runner code itself — they DO NOT invoke the real
``autodev`` binary. End-to-end runs against a real agent live in CI on
release tags or in manual ``python -m benchmarks.runner.run_benchmark``
invocations. Here we substitute stub autodev invokers and exercise the
scoring helpers directly.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from benchmarks.runner.run_benchmark import (
    DEFAULT_TASKS_ROOT,
    build_results_doc,
    main,
)
from benchmarks.runner.scorer import (
    apply_patch_to_repo,
    extract_diff_from_ledger,
    iter_task_dirs,
    score_benchmark_results,
    score_task_with_patch,
)
from benchmarks.runner.task_runner import (
    TaskResult,
    _SubprocessResult,
    discover_tasks,
    run_task,
)


# ---------------------------------------------------------------------------
# Required corpus checks
# ---------------------------------------------------------------------------

REQUIRED_TASK_IDS = {
    "task_001_py_typeerror",
    "task_002_ts_nullcheck",
    "task_003_py_slice",
    "task_004_go_defer",
    "task_005_py_perf",
}


def test_v1_corpus_contains_all_five_tasks():
    discovered = {t.name for t in discover_tasks(DEFAULT_TASKS_ROOT)}
    assert discovered == REQUIRED_TASK_IDS


def test_task_metadata_validation():
    """Every task must declare the canonical meta.json keys."""
    required_keys = {
        "language",
        "difficulty",
        "expected_minutes",
        "license",
        "origin",
        "description",
    }
    allowed_difficulty = {"easy", "medium", "hard"}
    for task_dir in iter_task_dirs(DEFAULT_TASKS_ROOT):
        meta = json.loads((task_dir / "meta.json").read_text(encoding="utf-8"))
        missing = required_keys - meta.keys()
        assert not missing, f"{task_dir.name}: meta.json missing {missing}"
        assert meta["difficulty"] in allowed_difficulty, (
            f"{task_dir.name}: difficulty {meta['difficulty']!r} not in "
            f"{allowed_difficulty}"
        )
        assert isinstance(meta["expected_minutes"], int)
        assert isinstance(meta["description"], str) and meta["description"].strip()


def test_each_task_has_required_files():
    for task_dir in iter_task_dirs(DEFAULT_TASKS_ROOT):
        for relname in ("spec.md", "ground_truth.patch", "test_command.sh"):
            assert (task_dir / relname).is_file(), (
                f"{task_dir.name}: missing {relname}"
            )
        assert (task_dir / "repo").is_dir(), f"{task_dir.name}: missing repo/"


# ---------------------------------------------------------------------------
# Scoring: ground-truth patch applies and the test passes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "task_name",
    sorted(REQUIRED_TASK_IDS),
)
def test_score_exact_match_passes(tmp_path: Path, task_name: str):
    """Applying ground_truth.patch to a clean repo must make the test pass."""
    if task_name == "task_004_go_defer" and shutil.which("go") is None:
        pytest.skip("go toolchain not available")
    if task_name == "task_002_ts_nullcheck" and shutil.which("node") is None:
        pytest.skip("node not available")

    task_dir = DEFAULT_TASKS_ROOT / task_name
    patch_text = (task_dir / "ground_truth.patch").read_text(encoding="utf-8")
    result = score_task_with_patch(task_dir, patch_text, workdir=tmp_path)
    assert result.passed, (
        f"{task_name}: ground truth patch did NOT make test pass. "
        f"exit={result.exit_code} apply_error={result.apply_error}\n"
        f"stdout: {result.stdout_tail}\nstderr: {result.stderr_tail}"
    )


def test_score_no_diff_fails(tmp_path: Path):
    """Empty agent diff means the bug is unfixed → test must fail."""
    task_dir = DEFAULT_TASKS_ROOT / "task_001_py_typeerror"
    result = score_task_with_patch(task_dir, "", workdir=tmp_path)
    assert not result.passed
    assert result.apply_error == "empty diff"


def test_score_broken_diff_fails(tmp_path: Path):
    """A syntactically-invalid patch yields a graceful FAIL, not a crash."""
    task_dir = DEFAULT_TASKS_ROOT / "task_001_py_typeerror"
    bogus = "this is not a valid unified diff at all\n@@ broken @@\n"
    result = score_task_with_patch(task_dir, bogus, workdir=tmp_path)
    assert not result.passed
    assert result.apply_error is not None
    assert "patch" in result.apply_error.lower()


def test_apply_patch_returns_sentinel_for_empty():
    res = apply_patch_to_repo(Path("/nonexistent"), "")
    assert not res.applied
    assert res.error == "empty diff"


# ---------------------------------------------------------------------------
# Diff extraction from a fake ledger
# ---------------------------------------------------------------------------


def test_runner_extracts_diff_from_ledger(tmp_path: Path):
    ledger = tmp_path / "plan-ledger.jsonl"
    diff_text = (
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )
    lines = [
        json.dumps({"event": "begin"}),
        json.dumps({"event": "execute_diff", "payload": {"diff": "older diff"}}),
        json.dumps({"event": "execute_diff", "diff": diff_text}),
        json.dumps({"event": "end"}),
    ]
    ledger.write_text("\n".join(lines) + "\n", encoding="utf-8")
    extracted = extract_diff_from_ledger(ledger)
    assert extracted == diff_text


def test_extract_diff_from_missing_ledger_returns_empty():
    assert extract_diff_from_ledger(Path("/no/such/file")) == ""


def test_extract_diff_falls_back_to_payload(tmp_path: Path):
    ledger = tmp_path / "plan-ledger.jsonl"
    diff_text = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n"
    ledger.write_text(
        json.dumps({"op": "git_diff", "payload": {"diff": diff_text}}) + "\n",
        encoding="utf-8",
    )
    assert extract_diff_from_ledger(ledger) == diff_text


def test_extract_diff_skips_malformed_lines(tmp_path: Path):
    ledger = tmp_path / "plan-ledger.jsonl"
    diff_text = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-1\n+2\n"
    ledger.write_text(
        "not valid json\n"
        + json.dumps({"diff": diff_text})
        + "\nanother bad line\n",
        encoding="utf-8",
    )
    assert extract_diff_from_ledger(ledger) == diff_text


# ---------------------------------------------------------------------------
# Runner-level: stub autodev invoker
# ---------------------------------------------------------------------------


def test_runner_with_passing_stub(tmp_path: Path):
    """Stub autodev injects the ground-truth patch via a subprocess shim."""
    task_dir = DEFAULT_TASKS_ROOT / "task_001_py_typeerror"
    gt_patch = (task_dir / "ground_truth.patch").read_text(encoding="utf-8")

    def stub(args, cwd, timeout):
        # On `execute`, write the ground-truth patch into the agent's repo
        # and write a ledger entry that our extractor will find.
        if "execute" in args:
            ledger_dir = cwd / ".autodev"
            ledger_dir.mkdir(parents=True, exist_ok=True)
            (ledger_dir / "plan-ledger.jsonl").write_text(
                json.dumps({"event": "execute_diff", "diff": gt_patch}) + "\n",
                encoding="utf-8",
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
    assert result.secondary["invocations"] == 3
    # Diff size delta against ground truth should be zero (we used the GT itself).
    assert result.secondary["diff_size_delta_lines"] == 0


def test_runner_with_failing_stub(tmp_path: Path):
    """Empty diff → FAIL with apply_error=empty diff."""
    task_dir = DEFAULT_TASKS_ROOT / "task_001_py_typeerror"

    def stub(args, cwd, timeout):
        return _SubprocessResult(
            returncode=0,
            stdout="",
            stderr="",
            timed_out=False,
            elapsed_seconds=0.01,
        )

    result = run_task(task_dir, autodev_invoker=stub, workdir_root=tmp_path)
    assert result.status == "FAIL"
    assert result.apply_error == "empty diff"


def test_runner_respects_timeout(tmp_path: Path):
    """A timed-out autodev invocation aborts and marks FAIL."""
    task_dir = DEFAULT_TASKS_ROOT / "task_001_py_typeerror"

    def stub(args, cwd, timeout):
        return _SubprocessResult(
            returncode=-1,
            stdout="",
            stderr="killed",
            timed_out=True,
            elapsed_seconds=float(timeout),
        )

    result = run_task(
        task_dir,
        autodev_invoker=stub,
        autodev_timeout_seconds=1,
        workdir_root=tmp_path,
    )
    assert result.status == "FAIL"
    assert result.error is not None
    assert "timed out" in result.error
    # We should have aborted on the very first call rather than continuing.
    assert result.secondary["invocations"] == 1


# ---------------------------------------------------------------------------
# Cross-release comparison
# ---------------------------------------------------------------------------


def test_score_benchmark_results_detects_regression():
    baseline = {
        "summary": {"passed": 5, "failed": 0, "total": 5, "pass_rate": 1.0},
        "results": [
            {"task_id": f"t{i}", "status": "PASS"} for i in range(5)
        ],
    }
    current = {
        "summary": {"passed": 3, "failed": 2, "total": 5, "pass_rate": 0.6},
        "results": [
            {"task_id": "t0", "status": "PASS"},
            {"task_id": "t1", "status": "PASS"},
            {"task_id": "t2", "status": "PASS"},
            {"task_id": "t3", "status": "FAIL"},
            {"task_id": "t4", "status": "FAIL"},
        ],
    }
    summary = score_benchmark_results(current, baseline)
    assert summary["pass_rate"] == 0.6
    assert summary["baseline_pass_rate"] == 1.0
    assert summary["pass_rate_delta"] == pytest.approx(-0.4)
    assert summary["regressed"] is True
    regressed_tasks = [t for t in summary["per_task"] if t["regressed"]]
    assert {t["task_id"] for t in regressed_tasks} == {"t3", "t4"}


def test_score_benchmark_results_no_baseline():
    current = {
        "summary": {"passed": 4, "failed": 1, "total": 5, "pass_rate": 0.8},
        "results": [{"task_id": f"t{i}", "status": "PASS"} for i in range(5)],
    }
    summary = score_benchmark_results(current, None)
    assert summary["baseline_pass_rate"] is None
    assert summary["pass_rate_delta"] is None
    assert summary["regressed"] is False


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


def test_cli_list_returns_zero(capsys):
    rc = main(["--list"])
    out = capsys.readouterr().out
    assert rc == 0
    for tid in REQUIRED_TASK_IDS:
        assert tid in out


def test_cli_unknown_task_returns_two(capsys):
    rc = main(["--task", "task_does_not_exist"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "matched no tasks" in err


def test_build_results_doc_shape():
    doc = build_results_doc(
        [
            TaskResult(
                task_id="t1",
                status="PASS",
                secondary={"wall_time_s": 1.0, "invocations": 3},
            ),
            TaskResult(
                task_id="t2",
                status="FAIL",
                secondary={"wall_time_s": 2.0, "invocations": 3},
                apply_error="empty diff",
            ),
        ],
        autodev_version="0.32.0",
        platform="claude_code",
    )
    assert doc["benchmark_version"] == "v1"
    assert doc["autodev_version"] == "0.32.0"
    assert doc["platform"] == "claude_code"
    assert doc["summary"] == {
        "passed": 1,
        "failed": 1,
        "total": 2,
        "pass_rate": 0.5,
    }
    assert len(doc["results"]) == 2
    assert doc["results"][1]["apply_error"] == "empty diff"
