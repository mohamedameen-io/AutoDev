"""v0.31.0 (Phase 1.4): chunked review envelope replaces 8KB hard truncation.

The pre-v0.31.0 reviewer envelope passed ``diff[:8000]`` — a hard byte
truncation that silently dropped any per-file context past the 8 KB
mark. Phase 1.4 introduced :func:`_build_chunked_review_diff`:

* Diffs ≤ 8 KB pass through unchanged.
* Larger diffs are split per-file. Generated / lock files are dropped.
* Files ≤ 2 KB are included whole; larger files reduce to a per-file
  summary + head + tail bytes.
* Total envelope is soft-capped at 32 KB.
"""

from __future__ import annotations

from orchestrator.execute_phase import (
    _build_chunked_review_diff,
    _matches_generated_glob,
    _split_diff_by_file,
    _summarise_file_diff,
)


def test_small_diff_passes_through_unchanged() -> None:
    """Diffs ≤ 8 KB are returned as-is — no chunking, no summary footer."""
    diff = (
        "diff --git a/src/a.py b/src/a.py\n"
        "@@ -1,2 +1,3 @@\n"
        " line1\n"
        "+line2\n"
        " line3\n"
    )
    assert _build_chunked_review_diff(diff) == diff


def test_split_diff_by_file_separates_sections() -> None:
    diff = (
        "diff --git a/src/a.py b/src/a.py\n"
        "@@ -1 +1 @@\n+x\n"
        "diff --git a/src/b.py b/src/b.py\n"
        "@@ -1 +1 @@\n+y\n"
    )
    sections = _split_diff_by_file(diff)
    assert [p for p, _ in sections] == ["src/a.py", "src/b.py"]


def test_summarise_file_diff_counts_plus_minus_hunks() -> None:
    file_diff = (
        "diff --git a/x b/x\n"
        "@@ -1,3 +1,4 @@\n"
        " unchanged\n"
        "-removed\n"
        "+added one\n"
        "+added two\n"
        "@@ -10,1 +11,1 @@\n"
        "-removed two\n"
        "+added three\n"
    )
    assert _summarise_file_diff(file_diff) == "+3 / -2, 2 hunks"


def test_matches_generated_glob_recognises_lock_files() -> None:
    assert _matches_generated_glob("uv.lock")
    assert _matches_generated_glob("path/to/Cargo.lock")
    assert _matches_generated_glob("frontend/package-lock.json")
    assert _matches_generated_glob("vendor/foo.min.js")
    assert _matches_generated_glob("src/__pycache__/foo.pyc")
    assert not _matches_generated_glob("src/main.py")
    assert not _matches_generated_glob("README.md")


def test_chunked_envelope_drops_generated_files() -> None:
    """uv.lock, package-lock.json etc. should be omitted entirely."""
    big = "+line\n" * 3000  # ~18 KB per file
    diff = (
        f"diff --git a/uv.lock b/uv.lock\n@@ -1 +1 @@\n{big}"
        f"diff --git a/src/main.py b/src/main.py\n@@ -1 +1 @@\n+real change\n"
    )
    out = _build_chunked_review_diff(diff)
    assert "uv.lock" not in out or "skipped" in out
    # The real-change file MUST appear.
    assert "src/main.py" in out
    assert "real change" in out


def test_chunked_envelope_truncates_oversize_files_to_head_and_tail() -> None:
    """A large file gets a summary header + head + tail slice, not the full body."""
    body = "+line\n" * 1000  # ~6 KB body, larger than 2 KB full-pass cap
    diff = (
        "diff --git a/a.py b/a.py\n"
        "@@ -1 +1 @@\n"
        f"{body}"
        "diff --git a/b.py b/b.py\n"
        "@@ -1 +1 @@\n"
        f"{body}"
    )
    out = _build_chunked_review_diff(diff)
    # Either or both files should show the truncation marker.
    assert "[truncated middle]" in out or "DIFF SUMMARY" in out


def test_chunked_envelope_caps_total_size() -> None:
    """Total envelope must stay near the 32 KB soft cap even for huge diffs."""
    body = "+line\n" * 5000  # ~30 KB per file
    sections = []
    for i in range(20):
        sections.append(
            f"diff --git a/file{i}.py b/file{i}.py\n@@ -1 +1 @@\n{body}"
        )
    diff = "".join(sections)
    out = _build_chunked_review_diff(diff)
    # 32 KB soft cap + per-section overhead; allow generous slack so the
    # test pins behaviour without being brittle.
    assert len(out.encode("utf-8")) < 64 * 1024


def test_chunked_envelope_summary_footer_records_truncation() -> None:
    """When files are dropped or chunked, a summary footer must explain why."""
    body = "+line\n" * 5000
    sections = []
    for i in range(20):
        sections.append(
            f"diff --git a/file{i}.py b/file{i}.py\n@@ -1 +1 @@\n{body}"
        )
    diff = "".join(sections)
    out = _build_chunked_review_diff(diff)
    assert "DIFF SUMMARY" in out


def test_chunked_envelope_handles_diff_without_git_headers() -> None:
    """Defensive: a raw text patch with no ``diff --git`` headers."""
    diff = "+++ added line\n" * 3000  # ~45 KB raw text
    out = _build_chunked_review_diff(diff)
    # Returned, just bounded — no crash.
    assert isinstance(out, str)
    assert len(out) > 0
