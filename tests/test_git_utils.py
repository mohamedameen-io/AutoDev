"""Tests for src.adapters.git_utils."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock


from adapters.git_utils import (
    _diff_files,
    _git_diff,
    _git_diff_range,
    _git_porcelain_set,
    _git_rev_parse_head,
    extract_files_from_diff,
)


def _init_git_repo(path: Path) -> None:
    """Initialise a minimal git repo at *path* suitable for testing."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path),
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# _git_porcelain_set
# ---------------------------------------------------------------------------


def test_git_porcelain_set_returns_none_when_not_git_repo(tmp_path: Path) -> None:
    result = _git_porcelain_set(tmp_path)
    assert result is None


def test_git_porcelain_set_returns_set_in_git_repo(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    # Create an untracked file so the set is non-empty.
    (tmp_path / "hello.txt").write_text("hi")
    result = _git_porcelain_set(tmp_path)
    assert isinstance(result, set)
    assert "hello.txt" in result


def test_git_porcelain_set_returns_empty_set_for_clean_repo(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    # Commit a file so the repo is clean.
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("content")
    subprocess.run(
        ["git", "add", "tracked.txt"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    result = _git_porcelain_set(tmp_path)
    assert result == set()


# ---------------------------------------------------------------------------
# _diff_files
# ---------------------------------------------------------------------------


def test_diff_files_returns_empty_when_sets_equal() -> None:
    s = {"a.py", "b.py"}
    assert _diff_files(s, s) == []


def test_diff_files_returns_new_files_when_sets_differ() -> None:
    before = {"a.py"}
    after = {"a.py", "b.py", "c.py"}
    assert _diff_files(before, after) == ["b.py", "c.py"]


def test_diff_files_returns_empty_when_before_is_none() -> None:
    assert _diff_files(None, {"a.py"}) == []


def test_diff_files_returns_empty_when_after_is_none() -> None:
    assert _diff_files({"a.py"}, None) == []


def test_diff_files_returns_empty_when_both_none() -> None:
    assert _diff_files(None, None) == []


# ---------------------------------------------------------------------------
# _git_diff
# ---------------------------------------------------------------------------


def test_git_diff_returns_none_when_not_git_repo(tmp_path: Path) -> None:
    result = _git_diff(tmp_path)
    assert result is None


def test_git_diff_returns_none_for_repo_with_no_commits(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    # No commits yet — `git diff HEAD` will fail (non-zero exit).
    result = _git_diff(tmp_path)
    assert result is None


def test_git_diff_returns_diff_string_when_files_changed(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    tracked = tmp_path / "file.txt"
    tracked.write_text("original\n")
    subprocess.run(
        ["git", "add", "file.txt"], cwd=str(tmp_path), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    # Modify the file so there is a diff.
    tracked.write_text("modified\n")
    result = _git_diff(tmp_path)
    assert result is not None
    assert "file.txt" in result


def test_git_diff_returns_none_for_clean_repo(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    tracked = tmp_path / "file.txt"
    tracked.write_text("original\n")
    subprocess.run(
        ["git", "add", "file.txt"], cwd=str(tmp_path), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    # No changes — diff should be empty string, which maps to None.
    result = _git_diff(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# Extended coverage — mocked subprocess paths
# ---------------------------------------------------------------------------


def test_porcelain_set_subprocess_error(tmp_path: Path) -> None:
    """OSError from subprocess.run → returns None."""
    _init_git_repo(tmp_path)
    with patch("subprocess.run", side_effect=OSError("boom")):
        result = _git_porcelain_set(tmp_path)
    assert result is None


def test_porcelain_set_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero returncode → returns None."""
    _init_git_repo(tmp_path)
    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    with patch("subprocess.run", return_value=mock_result):
        result = _git_porcelain_set(tmp_path)
    assert result is None


def test_porcelain_set_rename_entries(tmp_path: Path) -> None:
    """Rename line 'R  old -> new' should capture the new path."""
    _init_git_repo(tmp_path)
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "R  old.py -> new.py\n"
    with patch("subprocess.run", return_value=mock_result):
        result = _git_porcelain_set(tmp_path)
    assert result is not None
    assert "new.py" in result
    assert "old.py" not in result


def test_porcelain_set_short_lines_skipped(tmp_path: Path) -> None:
    """Lines shorter than 4 chars should be skipped."""
    _init_git_repo(tmp_path)
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "??\nM  valid.py\n"
    with patch("subprocess.run", return_value=mock_result):
        result = _git_porcelain_set(tmp_path)
    assert result is not None
    assert "valid.py" in result
    assert len(result) == 1  # short line was skipped


def test_git_diff_subprocess_error(tmp_path: Path) -> None:
    """OSError from subprocess.run → returns None."""
    with patch("subprocess.run", side_effect=OSError("fail")):
        result = _git_diff(tmp_path)
    assert result is None


def test_git_diff_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero returncode → returns None."""
    mock_result = MagicMock()
    mock_result.returncode = 1
    with patch("subprocess.run", return_value=mock_result):
        result = _git_diff(tmp_path)
    assert result is None


def test_git_diff_empty_output(tmp_path: Path) -> None:
    """Empty stdout → returns None (falsy string maps to None)."""
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = ""
    with patch("subprocess.run", return_value=mock_result):
        result = _git_diff(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# v0.9.0: _git_diff_range and _git_rev_parse_head
# ---------------------------------------------------------------------------


def _commit_file(repo: Path, name: str, content: str, msg: str) -> str:
    """Add and commit ``name`` with ``content``. Returns the new HEAD SHA."""
    (repo / name).write_text(content)
    subprocess.run(
        ["git", "add", name],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def test_git_diff_range_returns_unified_diff_between_commits(tmp_path: Path) -> None:
    """``_git_diff_range`` returns the unified diff for ``from_sha..to_sha``."""
    _init_git_repo(tmp_path)
    sha_a = _commit_file(tmp_path, "a.txt", "first\n", "init")
    sha_b = _commit_file(tmp_path, "a.txt", "first\nsecond\n", "add line")
    diff = _git_diff_range(tmp_path, sha_a, sha_b)
    assert diff is not None
    assert "+second" in diff
    # The diff header references the file changed.
    assert "a.txt" in diff


def test_git_diff_range_handles_invalid_sha_returns_none(tmp_path: Path) -> None:
    """Bogus shas → git returns non-zero → ``None``."""
    _init_git_repo(tmp_path)
    _commit_file(tmp_path, "a.txt", "x\n", "init")
    diff = _git_diff_range(tmp_path, "deadbeef", "cafebabe")
    assert diff is None


def test_git_diff_range_subprocess_error(tmp_path: Path) -> None:
    """``OSError`` from subprocess → ``None``."""
    with patch("subprocess.run", side_effect=OSError("fail")):
        result = _git_diff_range(tmp_path, "a", "b")
    assert result is None


def test_git_rev_parse_head_returns_sha(tmp_path: Path) -> None:
    """``_git_rev_parse_head`` returns the current HEAD sha."""
    _init_git_repo(tmp_path)
    sha = _commit_file(tmp_path, "a.txt", "x\n", "init")
    result = _git_rev_parse_head(tmp_path)
    assert result == sha


def test_git_rev_parse_head_outside_repo_returns_none(tmp_path: Path) -> None:
    """No ``.git`` dir → ``None`` without invoking subprocess."""
    result = _git_rev_parse_head(tmp_path)
    assert result is None


def test_git_rev_parse_head_subprocess_error(tmp_path: Path) -> None:
    """``OSError`` from subprocess → ``None``."""
    _init_git_repo(tmp_path)
    with patch("subprocess.run", side_effect=OSError("fail")):
        result = _git_rev_parse_head(tmp_path)
    assert result is None


# ---------------------------------------------------------------------------
# v0.13.0: extract_files_from_diff (lifted from phase_review_runner)
# ---------------------------------------------------------------------------


def test_extract_files_from_diff_empty_returns_empty_list() -> None:
    assert extract_files_from_diff("") == []


def test_extract_files_from_diff_parses_single_file() -> None:
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "index 1234..5678 100644\n"
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "+new\n"
    )
    assert extract_files_from_diff(diff) == ["foo.py"]


def test_extract_files_from_diff_dedupes() -> None:
    """Duplicates (e.g. multiple hunks per file) appear once in first-seen order."""
    diff = (
        "+++ b/foo.py\n"
        "+++ b/bar.py\n"
        "+++ b/foo.py\n"
    )
    assert extract_files_from_diff(diff) == ["foo.py", "bar.py"]


def test_extract_files_from_diff_skips_dev_null() -> None:
    """``+++ /dev/null`` (deletion target) is excluded by the prefix match."""
    diff = (
        "--- a/old.py\n"
        "+++ /dev/null\n"
        "--- a/keep.py\n"
        "+++ b/keep.py\n"
    )
    assert extract_files_from_diff(diff) == ["keep.py"]


def test_extract_files_from_diff_phase_review_alias_unchanged() -> None:
    """The legacy private name on phase_review_runner still resolves
    to the lifted function (no consumer breakage)."""
    from orchestrator import phase_review_runner

    assert phase_review_runner._extract_files_from_diff is extract_files_from_diff


# ---------------------------------------------------------------------------
# F-4: extract_files_from_diff is binary-aware (``+++ b/`` precedence,
# ``diff --git`` header / ``Binary files .. differ`` fallback for binary
# sections that carry no ``+++ b/`` line). A binary edit produced by
# ``git diff --binary`` has a ``GIT binary patch`` payload (or, without
# ``--binary``, a ``Binary files a/x and b/x differ`` line) and NO
# ``+++ b/<path>`` header — so the legacy parser returned [] for it,
# leaving binary edits invisible to apply-time scope gating.
# ---------------------------------------------------------------------------


def test_extract_files_from_diff_binary_only_uses_header_fallback() -> None:
    """A binary-only section (no ``+++ b/``) falls back to the b-side header."""
    diff = (
        "diff --git a/x.bin b/x.bin\n"
        "index 0000000000000000000000000000000000000000..1111111111111111111111111111111111111111 100644\n"
        "GIT binary patch\n"
        "literal 4\n"
        "Mc${NkU|<4b00031\n"
        "\n"
    )
    assert extract_files_from_diff(diff) == ["x.bin"]


def test_extract_files_from_diff_binary_files_differ_uses_header_fallback() -> None:
    """The abbreviated ``Binary files .. differ`` form (no ``+++ b/``)
    still resolves to the b-side path via the ``diff --git`` header."""
    diff = (
        "diff --git a/logo.png b/logo.png\n"
        "index 1111111..2222222 100644\n"
        "Binary files a/logo.png and b/logo.png differ\n"
    )
    assert extract_files_from_diff(diff) == ["logo.png"]


def test_extract_files_from_diff_mixed_text_and_binary() -> None:
    """A diff with one text section and one binary section returns BOTH
    paths — the text path from its ``+++ b/`` line, the binary path from
    its header fallback. Order is first-seen (text, then binary)."""
    diff = (
        "diff --git a/keep.py b/keep.py\n"
        "--- a/keep.py\n"
        "+++ b/keep.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
        "diff --git a/img.png b/img.png\n"
        "index 0000000..2222222 100644\n"
        "GIT binary patch\n"
        "literal 8\n"
        "Mc${NkU|<4b00031\n"
    )
    assert extract_files_from_diff(diff) == ["keep.py", "img.png"]


def test_extract_files_from_diff_plus_b_takes_precedence_over_header() -> None:
    """PRECEDENCE GUARD: a section that HAS a (malformed) ``+++ b/`` line
    must use THAT line, not the clean ``diff --git`` header. The malformed
    path (>255 chars) is rejected by the sanitiser, so the section
    contributes nothing — the clean header must NOT be parsed as a
    fallback. This pins the ``+++ b/``-wins rule that keeps every existing
    sanitisation test byte-identical."""
    long_segment = "a" * 256
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        f"+++ b/{long_segment}\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
    )
    assert extract_files_from_diff(diff) == []


def test_extract_files_from_diff_binary_header_sanitizes_path() -> None:
    """The header-fallback path is subject to the SAME sanitisation as the
    ``+++ b/`` path: an over-length b-side header path is rejected."""
    long_segment = "a" * 256
    diff = (
        f"diff --git a/{long_segment} b/{long_segment}\n"
        "index 1111..2222 100644\n"
        f"Binary files a/{long_segment} and b/{long_segment} differ\n"
    )
    assert extract_files_from_diff(diff) == []


def test_extract_files_from_diff_binary_skips_dev_null_header() -> None:
    """A binary DELETION (b-side is ``/dev/null``) contributes no path —
    mirrors the ``+++ /dev/null`` exclusion for text deletions."""
    diff = (
        "diff --git a/gone.bin b/dev/null\n"
        "deleted file mode 100644\n"
        "index 1111..0000\n"
        "Binary files a/gone.bin and /dev/null differ\n"
    )
    # The header b-side is literally ``dev/null`` (git strips the leading
    # slash in ``b/dev/null``); the ``Binary files .. and /dev/null differ``
    # line carries the canonical ``/dev/null`` sentinel which we skip.
    assert extract_files_from_diff(diff) == []
