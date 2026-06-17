"""CLI entry point for the AutoDev real-task benchmark.

Usage:

    python -m benchmarks.runner.run_benchmark \\
        --task all \\
        --autodev-version 0.32.0 \\
        --output benchmarks/results/v0.32.0_<ts>.json \\
        --platform claude_code

    python -m benchmarks.runner.run_benchmark \\
        --task task_001_py_typeerror,task_002_ts_nullcheck \\
        --output - \\
        --platform cursor

The runner does NOT install autodev for you — it expects ``autodev`` to be
on ``$PATH``. Per-task wall-clock timeout defaults to 600 s (configurable via
``--task-timeout``).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .scorer import score_benchmark_results
from .task_runner import (
    DEFAULT_TEST_TIMEOUT_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    TaskResult,
    discover_tasks,
    filter_tasks,
    run_task,
)

BENCHMARK_VERSION = "v1"
DEFAULT_TASKS_ROOT = Path(__file__).resolve().parent.parent / "tasks" / "v1"


def _summarise(results: Sequence[TaskResult]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status != "PASS")
    pass_rate = (passed / total) if total else 0.0
    return {
        "passed": passed,
        "failed": failed,
        "total": total,
        "pass_rate": round(pass_rate, 4),
    }


def build_results_doc(
    results: Sequence[TaskResult],
    *,
    autodev_version: str,
    platform: str,
) -> dict:
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "autodev_version": autodev_version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": platform,
        "summary": _summarise(results),
        "results": [r.to_dict() for r in results],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.runner.run_benchmark",
        description="Run the AutoDev real-task benchmark and emit results.json.",
    )
    parser.add_argument(
        "--task",
        default="all",
        help='Task selector: "all" (default) or comma-separated task IDs.',
    )
    parser.add_argument(
        "--tasks-root",
        type=Path,
        default=DEFAULT_TASKS_ROOT,
        help=f"Path to the tasks/v1 directory (default: {DEFAULT_TASKS_ROOT}).",
    )
    parser.add_argument(
        "--autodev-version",
        default="dev",
        help="String stamped into results.json under 'autodev_version'.",
    )
    parser.add_argument(
        "--platform",
        default="claude_code",
        choices=["claude_code", "cursor"],
        help="Adapter under test (informational; stamped into results.json).",
    )
    parser.add_argument(
        "--output",
        default="-",
        help='Where to write results.json. "-" (default) prints to stdout.',
    )
    parser.add_argument(
        "--task-timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Per-autodev-command timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--test-timeout",
        type=int,
        default=DEFAULT_TEST_TIMEOUT_SECONDS,
        help=f"Per-task test_command.sh timeout in seconds (default: {DEFAULT_TEST_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Optional prior results.json to diff against (regression check).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List discovered tasks and exit (no autodev invocation).",
    )
    return parser


def _json_default(obj: object) -> str:
    """Fallback serialiser for json.dumps: coerce bytes → str, anything else
    to its repr so result emission never crashes on a stray non-serialisable
    value (defensive net — the real fix is decoding bytes at capture time)."""
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    return repr(obj)


def _emit(doc: dict, dest: str) -> None:
    payload = json.dumps(doc, indent=2, sort_keys=False, default=_json_default)
    if dest == "-":
        print(payload)
    else:
        out_path = Path(dest)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    all_tasks = discover_tasks(args.tasks_root)
    if not all_tasks:
        print(
            f"no tasks discovered under {args.tasks_root}",
            file=sys.stderr,
        )
        return 2

    selected = filter_tasks(all_tasks, args.task)
    if not selected:
        print(
            f"task selector '{args.task}' matched no tasks; available: "
            + ", ".join(t.name for t in all_tasks),
            file=sys.stderr,
        )
        return 2

    if args.list:
        for t in selected:
            print(t.name)
        return 0

    results: list[TaskResult] = []
    for task_dir in selected:
        print(f"[bench] running {task_dir.name}", file=sys.stderr, flush=True)
        result = run_task(
            task_dir,
            autodev_timeout_seconds=args.task_timeout,
            test_timeout_seconds=args.test_timeout,
        )
        print(
            f"[bench] {task_dir.name}: {result.status}",
            file=sys.stderr,
            flush=True,
        )
        results.append(result)

    doc = build_results_doc(
        results,
        autodev_version=args.autodev_version,
        platform=args.platform,
    )

    if args.baseline is not None and args.baseline.is_file():
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        doc["comparison"] = score_benchmark_results(doc, baseline)

    _emit(doc, args.output)
    # Non-zero exit if anything failed, so CI can gate on it.
    return 0 if doc["summary"]["failed"] == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
