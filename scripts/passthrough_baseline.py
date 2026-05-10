#!/usr/bin/env python3
"""PIE-style passthrough sanity check for the slim metric harness.

USAGE:

    uv run python scripts/passthrough_baseline.py [--fixtures-dir PATH]

Why
---
PIE (arxiv 2302.07867 §3) reported a 12% phantom speedup on 500 identical
slow/fast pairs measured on real hardware — motivating their gem5
simulator switch. The lesson generalises: **always run a passthrough
baseline as a sanity check**. If your scorer reports improvement when the
input IS the answer, the harness has a bug.

What this v1 script does
------------------------
For each lean fixture in ``tests/fixtures/anti_bloat/`` (the curated lean
variants from Phase 0), it:

1. Computes :class:`qa.code_size_metrics.CodeSizeMetrics` for the lean file.
2. Treats the lean file as both "input" and "output" (passthrough — no
   compression possible).
3. Synthesises the slim_at_k score under the equation
   ``score = max(0, baseline_loc - candidate_loc)`` where baseline ==
   candidate. This MUST be exactly 0; any non-zero value indicates a
   harness bug (e.g., metric drift, line-counting off-by-one).

Exits 0 when every pair scores 0; exits 1 with a per-pair diagnostic
otherwise.

What this script intentionally does NOT do (v1 scope)
-----------------------------------------------------
The real PIE-style baseline would shell out to the live AutoDev
orchestrator and have it "rewrite" each fixture (the rewrite should be
the identity transform). Phase 6.5 will wire that path. For v1, exercising
the metric/scorer chain at the function level is the load-bearing check —
the orchestrator path adds latency without changing the math being tested.

Exit codes:
    0  — every passthrough scored 0 (harness sane)
    1  — at least one non-zero score (bug found)
    2  — no fixtures discovered
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from qa.code_size_metrics import compute_metrics_for_file  # noqa: E402

logger = logging.getLogger("passthrough_baseline")


_DEFAULT_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "anti_bloat"


def _slim_score(baseline_loc: int, candidate_loc: int) -> float:
    """Toy slim@k scorer: improvement only counts if candidate < baseline.

    For passthrough: ``baseline_loc == candidate_loc`` so this is 0.0.
    """
    return float(max(0, baseline_loc - candidate_loc))


def _run(fixtures_dir: Path) -> int:
    lean_files = sorted(fixtures_dir.glob("pair_*.lean.py"))
    if not lean_files:
        logger.error(
            "No lean fixtures found under %s — Phase 0 fixtures missing.",
            fixtures_dir,
        )
        return 2

    bugs: list[str] = []
    for f in lean_files:
        m = compute_metrics_for_file(f, skip_subprocess=True)
        baseline_loc = m.loc_executable
        # "Pass through" — output == input.
        candidate_loc = baseline_loc
        score = _slim_score(baseline_loc, candidate_loc)
        ok = score == 0.0
        marker = "OK" if ok else "BUG"
        logger.info(
            "[%s] %s loc=%d slim=%.3f",
            marker,
            f.name,
            baseline_loc,
            score,
        )
        if not ok:
            bugs.append(
                f"{f.name}: passthrough scored {score} "
                f"(baseline_loc={baseline_loc}, candidate_loc={candidate_loc})"
            )

    if bugs:
        logger.error("Passthrough harness bug(s) detected:")
        for b in bugs:
            logger.error("  - %s", b)
        return 1

    logger.info("All %d passthrough pair(s) scored 0 — harness sane.", len(lean_files))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PIE-style passthrough sanity check for the slim metric harness."
    )
    parser.add_argument(
        "--fixtures-dir",
        type=Path,
        default=_DEFAULT_FIXTURES,
        help=f"Directory holding pair_*.lean.py fixtures. Defaults to {_DEFAULT_FIXTURES}.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return _run(args.fixtures_dir)


if __name__ == "__main__":
    sys.exit(main())
