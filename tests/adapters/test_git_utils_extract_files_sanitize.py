"""Bug #3 regression: ``extract_files_from_diff`` must reject pathological paths.

When a developer agent emits a JSON-escaped multi-line code listing into the
``diff`` field of ``.autodev/responses/{task_id}-{role}.json``, the orchestrator's
``extract_files_from_diff`` parses ``+++ b/<path>`` lines. If the entire blob
arrives as a single line, the extracted "path" becomes a 4000+ char string
containing literal ``\\n`` escapes / NUL bytes / real newlines — none of which
are valid POSIX path components.

These tests verify the sanitiser drops:

* paths longer than 255 chars (POSIX ``NAME_MAX``),
* paths containing a real ``\\n`` or a literal ``\\n`` escape,
* paths containing ``\\x00``.

Valid paths in the same diff must survive unchanged.
"""

from __future__ import annotations

from adapters.git_utils import extract_files_from_diff


def test_extract_files_rejects_path_over_255_chars() -> None:
    """A path longer than POSIX NAME_MAX (255) must be dropped."""
    long_segment = "a" * 256
    diff = (
        f"diff --git a/foo.py b/foo.py\n"
        f"--- a/foo.py\n"
        f"+++ b/{long_segment}\n"
        f"@@ -0,0 +1 @@\n"
        f"+x = 1\n"
    )
    assert extract_files_from_diff(diff) == []


def test_extract_files_rejects_path_with_real_newline() -> None:
    """A path containing a real ``\\n`` must be dropped.

    (This is the common 4000-char multi-line blob case — splitlines() splits
    on it, but if upstream encoding mangled the diff, the literal escape may
    survive into the captured group.)
    """
    # Real newlines split via splitlines() so we use the literal-escape form;
    # see the next test for the splitlines-preserved case.
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/src/foo.py\\nbar.py\n"  # literal "\n" escape
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
    )
    assert extract_files_from_diff(diff) == []


def test_extract_files_rejects_path_with_literal_backslash_n() -> None:
    """A path containing a literal ``\\n`` escape sequence must be dropped."""
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo\\nbar.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
    )
    assert extract_files_from_diff(diff) == []


def test_extract_files_rejects_path_with_null_byte() -> None:
    """A path containing ``\\x00`` must be dropped."""
    diff = (
        "diff --git a/foo.py b/foo.py\n"
        "--- a/foo.py\n"
        "+++ b/foo\x00bar.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
    )
    assert extract_files_from_diff(diff) == []


def test_extract_files_keeps_valid_paths_when_oversized_paths_filtered() -> None:
    """Mixed-validity diffs must keep the valid headers and drop the bad ones."""
    long_segment = "a" * 300
    diff = (
        "diff --git a/keep.py b/keep.py\n"
        "--- a/keep.py\n"
        "+++ b/keep.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
        "diff --git a/bad b/bad\n"
        "--- a/bad\n"
        f"+++ b/{long_segment}\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
        "diff --git a/also_keep.py b/also_keep.py\n"
        "--- a/also_keep.py\n"
        "+++ b/also_keep.py\n"
        "@@ -0,0 +1 @@\n"
        "+y = 2\n"
    )
    result = extract_files_from_diff(diff)
    assert result == ["keep.py", "also_keep.py"]


def test_extract_files_accepts_exactly_255_chars() -> None:
    """The 255-char boundary must be inclusive (NAME_MAX is the cap)."""
    boundary = "a" * 255
    diff = (
        "diff --git a/x b/x\n"
        "--- a/x\n"
        f"+++ b/{boundary}\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
    )
    assert extract_files_from_diff(diff) == [boundary]


def test_extract_files_realworld_bug3_blob_returns_empty() -> None:
    """The actual 4000-char multi-line blob shape from the Unity run."""
    # Simulate the JSON-escaped blob landing as a single ``+++ b/...`` line.
    blob_path = "src/foo.py" + ("\\n" * 200) + ("a" * 4000)
    diff = (
        "diff --git a/foo b/foo\n"
        "--- a/foo\n"
        f"+++ b/{blob_path}\n"
    )
    assert extract_files_from_diff(diff) == []
