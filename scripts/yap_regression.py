#!/usr/bin/env python3
"""YapBench regression harness (v1 stub).

USAGE:

    uv run python scripts/yap_regression.py [--subset N] [--orchestrator-cmd "..."]

Goal
----
Compute YapScore / YapIndex (YapBench §3.3) over a configurable subset of
the 304-prompt × 3-category brevity benchmark. For each prompt, runs the
AutoDev orchestrator, then scores:

    YapScore = max(0, len(answer_chars) - len(baseline_chars))
    YapIndex = median(YapScore) per category

A record per run is appended to ``.autodev/yap_history.jsonl`` for the
longitudinal panel to track "did the most recent model upgrade make
verbosity worse?" — the YapBench headline finding (§3.3) is that
gpt-3.5-turbo (2023) beats every 2025-2026 frontier model on brevity.

v1 status
---------
The YapBench dataset is NOT downloaded in this repo (per Phase 0 only the
README placeholder lives at ``tests/benchmarks/yapbench/``). This script:

* If the dataset file does not exist, prints a short notice and exits 0
  so CI does not break.
* If the dataset file exists (future state), TODO: load prompts, iterate,
  shell out to the orchestrator, score, and write a record.

TODO for the maintainer landing the dataset
-------------------------------------------
1. Drop ``tests/benchmarks/yapbench/yapbench_dataset.parquet`` (or .jsonl)
   into the repo. Format: ``{prompt, category, baseline_answer}``.
2. Replace the ``_run_orchestrator`` stub with an ``asyncio`` runner that
   shells out to the configured CLI and captures the answer text.
3. Add a category breakdown to the JSONL record so the longitudinal CLI
   can plot per-category trends.

Exit codes:
    0  — success (or dataset absent)
    1  — dataset malformed / orchestrator harness error
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger("yap_regression")


_REPO_ROOT = Path(__file__).resolve().parent.parent
_DATASET_DIR = _REPO_ROOT / "tests" / "benchmarks" / "yapbench"
# Two candidate filenames so populating either lights the path up.
_CANDIDATES = (
    _DATASET_DIR / "yapbench_dataset.parquet",
    _DATASET_DIR / "yapbench_dataset.jsonl",
)
_DEFAULT_OUT = _REPO_ROOT / ".autodev" / "yap_history.jsonl"


def _dataset_path() -> Path | None:
    for c in _CANDIDATES:
        if c.is_file():
            return c
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="YapBench regression harness (v1 stub — exits 0 when dataset absent)."
    )
    parser.add_argument(
        "--subset",
        type=int,
        default=None,
        help="Run on only the first N prompts (default: all).",
    )
    parser.add_argument(
        "--orchestrator-cmd",
        type=str,
        default=None,
        help="Shell command template that emits an answer to stdout for {prompt}.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=_DEFAULT_OUT,
        help=f"JSONL ledger path. Defaults to {_DEFAULT_OUT}.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    ds = _dataset_path()
    if ds is None:
        logger.info(
            "YapBench dataset not present at %s — see README to populate.",
            _DATASET_DIR.relative_to(_REPO_ROOT),
        )
        return 0

    # TODO: implement the live path when the dataset lands. The shape is:
    #   for prompt, baseline in load(ds)[: args.subset]:
    #       answer = _run_orchestrator(prompt, args.orchestrator_cmd)
    #       yap = max(0, len(answer) - len(baseline))
    #       record["scores"].append({"prompt": prompt, "yap_score": yap, "category": category})
    #   record["yap_index"] = median per category
    #   write to args.out
    logger.info(
        "Dataset present at %s — live scoring path not yet implemented (v1 stub). "
        "See module docstring TODO list.",
        ds.relative_to(_REPO_ROOT),
    )
    # Write a sentinel record so operators can see the script ran.
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "status": "stub",
                    "dataset_path": str(ds.relative_to(_REPO_ROOT)),
                    "subset": args.subset,
                    "orchestrator_cmd": args.orchestrator_cmd,
                    "yap_index": None,
                }
            )
            + "\n"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
