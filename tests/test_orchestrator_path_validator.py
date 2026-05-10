"""v0.22.4 B4 regression: structured path normalization pipeline.

Pre-B4 the architect could emit markdown-formatted paths (backticks,
parentheticals, trailing punctuation) that the schema validator's
narrow checks (absolute / parent-relative rejection) let through —
they then tripped ``EditScopeViolation`` at execute time, wedging tasks.
v0.22.4 B4 promotes normalization into a structured pipeline with
machine-readable :class:`PathValidationError` for architect retry.
"""

from __future__ import annotations

import pytest

from orchestrator.path_validator import (
    PathValidationError,
    normalize_path,
    validate_paths_batch,
)


# ── Well-formed inputs round-trip cleanly ───────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("foo.py", "foo.py"),
        ("src/foo.py", "src/foo.py"),
        ("./foo.py", "foo.py"),
        ("./src/foo.py", "src/foo.py"),
        ("src//foo.py", "src/foo.py"),
        ("src/./foo.py", "src/foo.py"),
        ("src/foo/", "src/foo"),
        ("  foo.py  ", "foo.py"),
        # Outer quotes stripped (single, double, backtick).
        ("`foo.py`", "foo.py"),
        ("'foo.py'", "foo.py"),
        ('"foo.py"', "foo.py"),
        # Trailing punctuation stripped (one char only).
        ("foo.py.", "foo.py"),
        ("foo.py,", "foo.py"),
        ("foo.py;", "foo.py"),
        ("foo.py:", "foo.py"),
        ("foo.py)", "foo.py"),
        ("foo.py]", "foo.py"),
    ],
)
def test_normalize_path_normalizes(raw: str, expected: str) -> None:
    assert normalize_path(raw) == expected


# ── Rejected inputs raise with structured fields ───────────────────


@pytest.mark.parametrize(
    "raw,reason_substr",
    [
        ("/abs/path.py", "absolute"),
        ("../escape.py", "parent"),
        ("foo/../escape.py", "parent"),
        ("foo\nbar.py", "control"),
        ("foo\tbar.py", "control"),
        ("foo\x00bar.py", "control"),
        ("", "empty"),
        ("   ", "empty"),
    ],
)
def test_normalize_path_rejects(raw: str, reason_substr: str) -> None:
    with pytest.raises(PathValidationError) as exc:
        normalize_path(raw)
    assert reason_substr in exc.value.reason


def test_normalize_path_glob_allowed_by_default() -> None:
    assert normalize_path("**/foo.py", allow_glob=True) == "**/foo.py"


def test_normalize_path_glob_rejected_when_disallowed() -> None:
    with pytest.raises(PathValidationError) as exc:
        normalize_path("**/foo.py", allow_glob=False)
    assert "glob" in exc.value.reason


def test_path_validation_error_carries_fields() -> None:
    err = PathValidationError("/abs.py", reason="absolute_path", suggestion="abs.py")
    assert err.raw == "/abs.py"
    assert err.reason == "absolute_path"
    assert err.suggestion == "abs.py"
    # Human-readable message includes the raw + reason.
    assert "/abs.py" in str(err)
    assert "absolute_path" in str(err)


# ── Batch helper ───────────────────────────────────────────────────


def test_validate_paths_batch_partitions_clean_and_dirty() -> None:
    paths = ["foo.py", "/abs.py", "src/bar.py", "../escape.py"]
    normalized, errors = validate_paths_batch(paths)
    assert normalized == ["foo.py", "src/bar.py"]
    assert len(errors) == 2
    assert errors[0].reason == "absolute_path"
    assert errors[1].reason == "parent_segment"


def test_validate_paths_batch_empty_returns_empty() -> None:
    assert validate_paths_batch([]) == ([], [])


def test_validate_paths_batch_all_clean() -> None:
    normalized, errors = validate_paths_batch(["a.py", "b/c.py"])
    assert normalized == ["a.py", "b/c.py"]
    assert errors == []


# ── NFC normalization ──────────────────────────────────────────────




# ── Suggestion field is populated where useful ─────────────────────


def test_absolute_path_suggestion_strips_leading_slash() -> None:
    with pytest.raises(PathValidationError) as exc:
        normalize_path("/src/foo.py")
    assert exc.value.suggestion == "src/foo.py"
