#!/usr/bin/env python3
"""Longitudinal code-size metrics over a git commit range.

USAGE:

    uv run python scripts/anti_bloat_metrics.py \
        --from <sha> [--to HEAD] [--out PATH] [--cache-dir PATH]

Walks every commit in ``<from>..<to>`` (exclusive of ``<from>``, inclusive
of ``<to>``), extracts the Python files each commit changes, computes the
:class:`qa.code_size_metrics.CodeSizeMetrics` per file at that commit, and
appends one JSONL record per commit to the output file (default
``.autodev/anti_bloat_history.jsonl``).

Output schema (one record per commit):
    {
      "task_id": "<commit subject or first 60 chars>",
      "merged_sha": "<full sha>",
      "timestamp": "<ISO8601>",
      "bohr_quad": {
        "token_count": int,
        "defensive_ratio": float,
        "doc_density": float,
        "functions_per_file": int,
      },
      "static": {
        "loc_executable": int,
        "cyclomatic_max": int,
        "cyclomatic_mean": float,
        "n_abstractions": int,
        "dead_symbols": int,
        "commented_out_blocks": int,
        "duplicate_clusters": int,
      },
      "yap_score": int,
      "slim_at_k": {"k": int, "score": float},
      "model_used": null,
    }

v1 placeholders pending Phase 6.5 enrichment
--------------------------------------------
* ``yap_score`` = aggregate ``loc_executable`` for the commit. We do not
  have a per-task minimum-passing-candidate baseline available from
  commit history alone; once Phase 6.5 wires the tournament artifacts in,
  this becomes ``LOC − shortest_passing_candidate_LOC`` per YapBench
  §3.3.
* ``slim_at_k`` is the ENAMEL §2.1 level metric ``{"k": 1, "score": 0.0}``
  placeholder. Phase 6.5 will compute the actual k-cohort delta from
  tournament artifacts.
* ``model_used`` is ``null`` because we cannot recover the model used to
  produce a commit from the commit itself; Phase 6.5 will join against
  ``.autodev/runs/`` artifacts.

Caching
-------
Per-file metrics are cached in
``~/.cache/autodev/code_size/<sha>/<file_hash>.json`` keyed by
``(commit_sha, file_path)``. Use ``--cache-dir`` to override. The cache
makes re-running the script over a long range cheap; commits whose
relevant files all hit the cache contribute only the JSON write cost.

Exit codes:
    0  — success
    1  — invalid range / git failure
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Iterable

# `pythonpath = ["src"]` in pyproject means tests can import this; for the
# CLI invocation we add src/ explicitly so `python scripts/...` works too.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from qa.code_size_metrics import (  # noqa: E402
    CodeSizeMetrics,
    aggregate,
    compute_metrics_for_file,
)
from adapters.git_utils import extract_files_from_diff  # noqa: E402

logger = logging.getLogger("anti_bloat_metrics")


_DEFAULT_OUT = Path(".autodev") / "anti_bloat_history.jsonl"
_DEFAULT_CACHE = Path.home() / ".cache" / "autodev" / "code_size"


def _git(args: list[str], cwd: Path) -> str:
    """Invoke git, returning stdout. Raises CalledProcessError on failure."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return proc.stdout


def _commits_in_range(repo: Path, from_sha: str, to_sha: str) -> list[str]:
    """Return commit SHAs in chronological (oldest-first) order.

    ``git rev-list <from>..<to>`` lists commits reachable from <to> but
    not from <from>, newest-first; we reverse so the JSONL ledger reads
    oldest-first (matches operator expectation: "scroll down to see what
    happened after I switched models").
    """
    out = _git(["rev-list", f"{from_sha}..{to_sha}"], cwd=repo).strip()
    if not out:
        return []
    return list(reversed(out.splitlines()))


def _commit_subject(repo: Path, sha: str) -> str:
    out = _git(["log", "-1", "--pretty=%s", sha], cwd=repo).strip()
    return out[:60] if out else sha[:7]


def _commit_timestamp_iso(repo: Path, sha: str) -> str:
    """Author date as ISO8601 (UTC)."""
    out = _git(["log", "-1", "--pretty=%aI", sha], cwd=repo).strip()
    return out or _dt.datetime.utcnow().isoformat()


def _commit_diff(repo: Path, sha: str) -> str:
    """Diff for *sha* against its first parent. Empty on initial commit."""
    try:
        return _git(["show", "--format=", "--unified=0", sha], cwd=repo)
    except subprocess.CalledProcessError:
        return ""


def _python_files_in_diff(diff: str) -> list[str]:
    return [p for p in extract_files_from_diff(diff) if p.endswith(".py")]


def _file_hash(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]


def _load_cached(cache_dir: Path, sha: str, repo_path: str) -> dict | None:
    cache_path = cache_dir / sha / f"{_file_hash(repo_path)}.json"
    if not cache_path.is_file():
        return None
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _store_cached(cache_dir: Path, sha: str, repo_path: str, payload: dict) -> None:
    cache_path = cache_dir / sha / f"{_file_hash(repo_path)}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as exc:
        logger.debug("anti_bloat_metrics.cache_write_failed: %s", exc)


def _metrics_for_commit_file(
    repo: Path, sha: str, repo_path: str, cache_dir: Path
) -> CodeSizeMetrics | None:
    """Compute (or load from cache) metrics for *repo_path* @ *sha*.

    Returns None when the file no longer exists at *sha* (e.g., deletion
    commits).
    """
    cached = _load_cached(cache_dir, sha, repo_path)
    if cached is not None:
        # Re-hydrate a CodeSizeMetrics from the dict so aggregate() works.
        m = CodeSizeMetrics()
        for k, v in cached.items():
            if hasattr(m, k):
                setattr(m, k, v)
        return m

    # Materialise the file at *sha* into a tmp path, then compute.
    try:
        blob = _git(["show", f"{sha}:{repo_path}"], cwd=repo)
    except subprocess.CalledProcessError:
        return None  # file deleted at this commit

    # Use a tmp file so compute_metrics_for_file (which is path-based)
    # can read the historical content, not whatever is on disk.
    tmp = repo / ".autodev" / "_anti_bloat_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    tmp_file = tmp / f"{_file_hash(repo_path)}.py"
    try:
        tmp_file.write_text(blob, encoding="utf-8")
        # skip_subprocess=True: we don't need vulture/eradicate/pylint
        # for the longitudinal panel; AST + radon is enough signal and
        # subprocess invocations dominate runtime over many commits.
        m = compute_metrics_for_file(tmp_file, skip_subprocess=True)
    finally:
        try:
            tmp_file.unlink()
        except OSError:
            pass

    _store_cached(cache_dir, sha, repo_path, m.to_dict())
    return m


def _build_record(
    repo: Path,
    sha: str,
    cache_dir: Path,
) -> dict:
    diff = _commit_diff(repo, sha)
    py_files = _python_files_in_diff(diff)

    per_file: list[CodeSizeMetrics] = []
    for fp in py_files:
        m = _metrics_for_commit_file(repo, sha, fp, cache_dir)
        if m is not None:
            per_file.append(m)
    agg = aggregate(per_file) if per_file else CodeSizeMetrics()

    return {
        "task_id": _commit_subject(repo, sha),
        "merged_sha": sha,
        "timestamp": _commit_timestamp_iso(repo, sha),
        "bohr_quad": {
            "token_count": agg.token_count,
            "defensive_ratio": round(agg.defensive_ratio, 4),
            "doc_density": round(agg.doc_density, 4),
            "functions_per_file": agg.functions_per_file,
        },
        "static": {
            "loc_executable": agg.loc_executable,
            "cyclomatic_max": agg.cyclomatic_max,
            "cyclomatic_mean": round(agg.cyclomatic_mean, 4),
            "n_abstractions": agg.n_abstractions,
            "dead_symbols": agg.dead_symbols,
            "commented_out_blocks": agg.commented_out_blocks,
            "duplicate_clusters": agg.duplicate_clusters,
        },
        # v1 placeholder per docstring "v1 placeholders pending Phase 6.5
        # enrichment". yap_score == aggregate loc_executable until we wire
        # in the per-task minimum-passing-candidate baseline.
        "yap_score": agg.loc_executable,
        "slim_at_k": {"k": 1, "score": 0.0},
        "model_used": None,
    }


def write_jsonl(records: Iterable[dict], out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
            n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Append per-commit code-size metrics for a git range to a JSONL "
            "ledger. v1 emits placeholder yap_score / slim_at_k pending "
            "Phase 6.5 tournament-artifact enrichment."
        )
    )
    parser.add_argument("--from", dest="from_sha", required=True, help="Range start SHA (exclusive).")
    parser.add_argument("--to", dest="to_sha", default="HEAD", help="Range end SHA (inclusive). Defaults to HEAD.")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=f"JSONL output. Defaults to <repo>/{_DEFAULT_OUT}.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=_DEFAULT_CACHE,
        help=f"Per-(sha,file) metric cache dir. Defaults to {_DEFAULT_CACHE}.",
    )
    parser.add_argument(
        "--cwd",
        type=Path,
        default=Path.cwd(),
        help="Repository root. Defaults to current directory.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    repo = args.cwd.resolve()
    out_path = args.out if args.out is not None else (repo / _DEFAULT_OUT)
    cache_dir = args.cache_dir.expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        commits = _commits_in_range(repo, args.from_sha, args.to_sha)
    except subprocess.CalledProcessError as exc:
        logger.error("git rev-list failed: %s", exc.stderr.strip())
        return 1

    if not commits:
        logger.info("No commits in range %s..%s — nothing to do.", args.from_sha, args.to_sha)
        return 0

    records = [_build_record(repo, sha, cache_dir) for sha in commits]
    n = write_jsonl(records, out_path)
    logger.info("Wrote %d record(s) to %s", n, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
