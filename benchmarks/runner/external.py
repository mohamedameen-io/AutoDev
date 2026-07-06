"""External-benchmark CLI: solve/score split over the adapter/scorer protocols.

The Phase-1 external benchmark decouples the two halves into separate, resumable
runs joined by a ``predictions.jsonl`` handoff file:

    # 1. Solve on the host (arm64), writing predictions.jsonl
    python -m benchmarks.runner.external --solve-only \\
        --dataset swe-bench-lite --instances <ids-or-file> \\
        --workdir-root /tmp/bench --predictions-out preds.jsonl

    # 2. Score the predictions via the cloud scorer
    python -m benchmarks.runner.external --score-only \\
        --predictions preds.jsonl --run-id <id> --report-out report.json

The core :func:`run_solve` / :func:`run_score` functions take the adapter/scorer
(and, for solve, the instances + invoker) as parameters, so they are fully
unit-testable with in-memory fakes — no autodev, no network. ``main`` wires
argparse and lazily loads the concrete SWE-bench-Lite adapter/dataset (P1.2) and
sb-cli scorer (P1.3); until those land it exits with a clear message rather than
importing them at module load.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import sys
from pathlib import Path
from typing import Iterable, Sequence

from benchmarks.adapters.base import (
    BenchmarkAdapter,
    Instance,
    InstancePrepareError,
)
from benchmarks.runner.solve import SolveInvoker, default_solve_invoker, solve
from benchmarks.scorers.base import ScoreReport, Scorer


# ---------------------------------------------------------------------------
# jsonl helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, records: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if line:
                out.append(json.loads(line))
    return out


# ---------------------------------------------------------------------------
# Core flows (injectable — the CLI and the tests both call these)
# ---------------------------------------------------------------------------


def run_solve(
    adapter: BenchmarkAdapter,
    instances: Iterable[Instance],
    invoker: SolveInvoker,
    *,
    workdir_root: Path,
    predictions_out: Path,
) -> list[dict]:
    """Solve every instance and write ``predictions.jsonl``.

    For each instance: ``adapter.prepare`` materialises a git workdir + profile,
    ``adapter.intent`` supplies the intent, :func:`solve` drives autodev and
    recovers the diff, and ``adapter.predict`` turns the outcome into a prediction
    record. Returns the list of prediction records (also written to disk).
    """
    predictions: list[dict] = []
    model_name = str(getattr(adapter, "model_name", "autodev"))
    for i, instance in enumerate(instances):
        instance_id = str(instance.get("instance_id", f"inst_{i}"))
        workdir = workdir_root / f"inst_{i:04d}"
        try:
            profile = adapter.prepare(instance, workdir)
            intent = adapter.intent(instance)
            outcome = solve(workdir, intent, profile, invoker)
            predictions.append(dict(adapter.predict(instance, workdir, outcome)))
        except InstancePrepareError:
            # Expected per-instance setup failure (e.g. an invalid base_commit):
            # record a patch-less prediction (scorer marks it ERROR) so the
            # instance is accounted for, then continue — never abort the whole
            # sweep. Any OTHER exception propagates (a genuine harness bug).
            predictions.append(
                {
                    "instance_id": instance_id,
                    "model_name_or_path": model_name,
                    "model_patch": "",
                }
            )
    _write_jsonl(predictions_out, predictions)
    return predictions


def run_score(
    scorer: Scorer,
    predictions_path: Path,
    *,
    run_id: str,
    report_out: Path | None = None,
) -> ScoreReport:
    """Score a ``predictions.jsonl`` and optionally write a JSON report."""
    predictions = _read_jsonl(predictions_path)
    report = scorer.score(predictions, run_id=run_id)
    if report_out is not None:
        report_out.parent.mkdir(parents=True, exist_ok=True)
        report_out.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "summary": report.counts(),
                    "instances": [dataclasses.asdict(s) for s in report.instances],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return report


# ---------------------------------------------------------------------------
# Lazy loaders for the concrete Phase-1 components (P1.2 / P1.3)
# ---------------------------------------------------------------------------


def _load_solve_components(
    args: argparse.Namespace,
) -> tuple[BenchmarkAdapter, list[Instance], SolveInvoker]:
    """Lazily import the SWE-bench-Lite adapter + dataset (P1.2).

    Uses ``importlib`` so the not-yet-existing modules are never statically
    imported at module load. Raises ``SystemExit`` with a clear message until
    P1.2 lands them.
    """
    try:
        adapters_mod = importlib.import_module("benchmarks.adapters.swebench_lite")
        dataset_mod = importlib.import_module("benchmarks.datasets.swebench_lite")
    except ImportError as exc:  # pragma: no cover - exercised once P1.2 exists
        raise SystemExit(
            "--solve-only requires the SWE-bench-Lite adapter/dataset (Phase-1 "
            f"P1.2), not yet available: {exc}"
        )
    adapter: BenchmarkAdapter = adapters_mod.build_adapter(args)
    instances: list[Instance] = list(dataset_mod.load_instances(args))
    return adapter, instances, default_solve_invoker


def _load_scorer(args: argparse.Namespace) -> Scorer:
    """Lazily import the sb-cli scorer (P1.3)."""
    try:
        scorer_mod = importlib.import_module("benchmarks.scorers.sbcli")
    except ImportError as exc:  # pragma: no cover - exercised once P1.3 exists
        raise SystemExit(
            "--score-only requires the sb-cli scorer (Phase-1 P1.3), not yet "
            f"available: {exc}"
        )
    scorer: Scorer = scorer_mod.build_scorer(args)
    return scorer


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.runner.external",
        description=(
            "External-benchmark solve/score CLI (Phase-1 SWE-bench-Lite tripwire)."
        ),
    )
    flow = parser.add_mutually_exclusive_group()
    flow.add_argument(
        "--solve-only",
        action="store_true",
        help="Solve instances on the host and write predictions.jsonl.",
    )
    flow.add_argument(
        "--score-only",
        action="store_true",
        help="Score an existing predictions.jsonl via the cloud scorer.",
    )
    # solve args
    parser.add_argument("--dataset", default="swe-bench-lite", help="Dataset id.")
    parser.add_argument(
        "--instances", default=None, help="Instance selector (ids or a file)."
    )
    parser.add_argument(
        "--workdir-root", type=Path, default=None, help="Root for per-instance workdirs."
    )
    parser.add_argument(
        "--predictions-out", type=Path, default=None, help="Where to write predictions.jsonl."
    )
    # score args
    parser.add_argument(
        "--predictions", type=Path, default=None, help="predictions.jsonl to score."
    )
    parser.add_argument("--run-id", default=None, help="Run id stamped on the report.")
    parser.add_argument(
        "--report-out", type=Path, default=None, help="Where to write the JSON report."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.solve_only:
        if args.workdir_root is None or args.predictions_out is None:
            print(
                "--solve-only requires --workdir-root and --predictions-out",
                file=sys.stderr,
            )
            return 2
        adapter, instances, invoker = _load_solve_components(args)
        run_solve(
            adapter,
            instances,
            invoker,
            workdir_root=args.workdir_root,
            predictions_out=args.predictions_out,
        )
        return 0

    if args.score_only:
        if args.predictions is None or args.run_id is None:
            print("--score-only requires --predictions and --run-id", file=sys.stderr)
            return 2
        scorer = _load_scorer(args)
        report = run_score(
            scorer,
            args.predictions,
            run_id=args.run_id,
            report_out=args.report_out,
        )
        counts = report.counts()
        # Non-zero if any instance errored (infra/quota) — never a silent 0.
        return 0 if counts["errored"] == 0 else 1

    print("one of --solve-only / --score-only is required", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
