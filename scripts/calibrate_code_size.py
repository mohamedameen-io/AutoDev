#!/usr/bin/env python3
"""Calibrate the code_size QA gate against the human-labelled PR corpus.

USAGE:

    uv run python scripts/calibrate_code_size.py [--corpus-dir PATH]

Reads PRs from ``tests/calibration/code_size/`` (or ``--corpus-dir``),
runs the gate against each PR's diff, compares to the human labels, and
reports per-rule precision. Used to decide which rules can be promoted
from ``severity="warn"`` to ``severity="block"`` (per the plan's
calibration-first promotion policy: precision >= 85% required).

v1 behavior
-----------
The corpus is empty. The script prints a notice and exits 0 so it does
not break CI on day one. Populate the corpus as PRs accumulate; later
versions will compute per-rule precision/recall/F1 against the labels.

Exit codes:
    0  — success (or empty corpus)
    1  — corpus malformed
    2  — at least one rule below 85% precision (future)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CORPUS = _REPO_ROOT / "tests" / "calibration" / "code_size"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calibrate the code_size QA gate against a labelled PR corpus."
    )
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=_DEFAULT_CORPUS,
        help=(
            "Directory containing PR-<n>/diff.patch + label.json subdirs. "
            f"Defaults to {_DEFAULT_CORPUS.relative_to(_REPO_ROOT)}."
        ),
    )
    args = parser.parse_args()

    corpus_dir: Path = args.corpus_dir
    if not corpus_dir.exists():
        print(
            f"calibration corpus directory does not exist: {corpus_dir}",
            file=sys.stderr,
        )
        return 1

    pr_dirs = [
        p
        for p in corpus_dir.iterdir()
        if p.is_dir() and p.name.startswith("PR-")
    ]
    if not pr_dirs:
        print(
            f"calibration corpus is empty; populate {corpus_dir.relative_to(_REPO_ROOT)}/ "
            "before running. The code_size gate stays at severity='warn' until "
            "per-rule precision >= 85% on a 50-PR sample (see README.md)."
        )
        return 0

    # v1 placeholder: corpus is empty, so per-PR scoring is not yet implemented.
    # When corpus entries land, enumerate each PR-<n>/, apply the diff to a
    # temp worktree, run qa.code_size.run_code_size, compare findings to
    # label.json, and emit per-rule precision/recall.
    print(
        f"found {len(pr_dirs)} PR(s) in calibration corpus — scoring not yet implemented (v1 stub)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
