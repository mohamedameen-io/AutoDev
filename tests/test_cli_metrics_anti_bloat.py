"""Tests for ``autodev metrics anti-bloat`` (v0.22.0 Phase 6).

Three scenarios per the Phase 6 plan:

(a) Clean range — no Python files changed. The script writes one record
    per commit (with all-zero metrics) and exits 0.
(b) Range covering known synthetic commits — assert the per-commit metrics
    match what we wrote.
(c) Markdown rendering — feed a small ledger to the renderer and check the
    expected column headers come out without crashing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.commands.metrics import _render_markdown, anti_bloat_cmd


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "anti_bloat_metrics.py"


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _head_sha(cwd: Path) -> str:
    return _git(["rev-parse", "HEAD"], cwd).strip()


def _make_commit(repo: Path, fname: str, body: str, message: str) -> str:
    (repo / fname).write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", fname], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=str(repo), check=True)
    return _head_sha(repo)


def _run_script(*, cwd: Path, from_sha: str, out: Path, cache_dir: Path) -> int:
    proc = subprocess.run(
        [
            "uv",
            "run",
            "python",
            str(_SCRIPT),
            "--from",
            from_sha,
            "--to",
            "HEAD",
            "--out",
            str(out),
            "--cwd",
            str(cwd),
            "--cache-dir",
            str(cache_dir),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print("STDOUT:", proc.stdout)
        print("STDERR:", proc.stderr)
    return proc.returncode


def test_clean_range_no_python_changes(tmp_git_repo: Path, tmp_path: Path) -> None:
    """(a) When the only commits in range touch non-Python files, every record
    has all-zero metrics. We expect one record per commit (the script
    always emits one — the metrics happen to all be zero because nothing
    Python changed)."""
    base = _head_sha(tmp_git_repo)
    _make_commit(tmp_git_repo, "NOTES.md", "# notes\n", "docs: note 1")
    _make_commit(tmp_git_repo, "NOTES.md", "# notes\n\nmore\n", "docs: note 2")

    out = tmp_path / "history.jsonl"
    cache = tmp_path / "cache"
    rc = _run_script(cwd=tmp_git_repo, from_sha=base, out=out, cache_dir=cache)
    assert rc == 0
    records = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert len(records) == 2
    for r in records:
        # All metrics must be zero — no Python touched.
        assert r["bohr_quad"]["token_count"] == 0
        assert r["static"]["loc_executable"] == 0
        assert r["yap_score"] == 0
        assert r["slim_at_k"] == {"k": 1, "score": 0.0}
        assert r["model_used"] is None


def test_range_with_python_commits_metrics_match(
    tmp_git_repo: Path, tmp_path: Path
) -> None:
    """(b) Two commits each touching a known Python file with known LOC."""
    pytest.importorskip("radon")  # loc_executable assertions need radon
    base = _head_sha(tmp_git_repo)
    # Commit 1: 3 executable LOC (one function, two statements).
    sha1 = _make_commit(
        tmp_git_repo,
        "mod_a.py",
        "def a(x):\n    y = x + 1\n    return y\n",
        "feat: add a",
    )
    # Commit 2: 5 executable LOC (one function, four statements).
    sha2 = _make_commit(
        tmp_git_repo,
        "mod_b.py",
        (
            "def b(x):\n"
            "    a = x + 1\n"
            "    b = x + 2\n"
            "    c = x + 3\n"
            "    return a + b + c\n"
        ),
        "feat: add b",
    )

    out = tmp_path / "history.jsonl"
    cache = tmp_path / "cache"
    rc = _run_script(cwd=tmp_git_repo, from_sha=base, out=out, cache_dir=cache)
    assert rc == 0
    records = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
    assert len(records) == 2

    # Records are oldest-first per the script docstring.
    by_sha = {r["merged_sha"]: r for r in records}
    assert sha1 in by_sha
    assert sha2 in by_sha
    r1 = by_sha[sha1]
    r2 = by_sha[sha2]
    # radon raw.sloc counts non-blank, non-comment lines — both files have
    # exactly N executable lines.
    assert r1["static"]["loc_executable"] == 3
    assert r2["static"]["loc_executable"] == 5
    # functions_per_file: one callable per commit's diff
    assert r1["bohr_quad"]["functions_per_file"] == 1
    assert r2["bohr_quad"]["functions_per_file"] == 1
    # yap_score == aggregate loc_executable per the v1 placeholder
    assert r1["yap_score"] == 3
    assert r2["yap_score"] == 5


def test_render_markdown_contains_headers(tmp_path: Path) -> None:
    """(c) Markdown renderer accepts a small ledger and contains the
    expected columns. We exercise the rendering function directly to keep
    the test fast and isolated from the CLI subprocess path."""
    records = [
        {
            "task_id": "feat: x",
            "merged_sha": "abcdef1234567890",
            "timestamp": "2026-01-01T00:00:00",
            "bohr_quad": {
                "token_count": 42,
                "defensive_ratio": 0.1,
                "doc_density": 0.5,
                "functions_per_file": 2,
            },
            "static": {
                "loc_executable": 17,
                "cyclomatic_max": 3,
                "cyclomatic_mean": 2.0,
                "n_abstractions": 2,
                "dead_symbols": 0,
                "commented_out_blocks": 0,
                "duplicate_clusters": 0,
            },
            "yap_score": 17,
            "slim_at_k": {"k": 1, "score": 0.0},
            "model_used": None,
        }
    ]
    md = _render_markdown(records)
    for header in ("commit", "task", "tokens", "def_ratio", "doc_dens", "loc", "cc_max", "yap"):
        assert header in md, f"Missing column header: {header}"
    # Row content checks.
    assert "abcdef1" in md
    assert "feat: x" in md
    assert "| 17 |" in md  # loc column

    # Also verify the CLI dispatcher accepts --report markdown without
    # invoking the script (--no-run path).
    ledger = tmp_path / "history.jsonl"
    ledger.write_text(json.dumps(records[0]) + "\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        anti_bloat_cmd,
        [
            "--from",
            "0" * 40,
            "--out",
            str(ledger),
            "--report",
            "markdown",
            "--no-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "tokens" in result.output


def test_render_markdown_empty_ledger() -> None:
    """Empty ledger renders a friendly notice instead of an empty table."""
    md = _render_markdown([])
    assert "No records" in md
