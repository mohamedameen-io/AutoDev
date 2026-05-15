"""Unit tests for :mod:`orchestrator.test_result_classifier` (v0.32.0 Phase 3).

Pin the six-way diagnosis taxonomy that lets the orchestrator
distinguish "no tests existed" from "runner crashed" from "stdout
capture failed". The classifier is a pure data transformation so the
tests are simple AAA-style assertions over a lightweight stand-in for
``AgentResult``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from orchestrator.test_result_classifier import (
    classify_test_result,
    redact_stderr_tail,
)


@dataclass
class _FakeResult:
    """Minimal stand-in matching the ``_AgentResultLike`` Protocol.

    Defined locally so the tests don't pull the full ``AgentResult``
    pydantic dependency tree (and so a future schema change to
    ``AgentResult`` doesn't silently break this contract).
    """

    success: bool = True
    text: str = ""
    error: str | None = None
    raw_stderr: str = ""


def test_classify_ok() -> None:
    """Nonzero ``total`` short-circuits to ``"ok"`` regardless of pass/fail."""
    result = _FakeResult(success=True, text="RESULTS: passed=42 failed=0 total=42")
    assert classify_test_result(result, (42, 0, 42)) == "ok"


def test_classify_ok_with_failures() -> None:
    """``"ok"`` fires even when some tests failed — branching is downstream."""
    result = _FakeResult(success=False, text="some failures")
    assert classify_test_result(result, (10, 2, 12)) == "ok"


def test_classify_no_tests_found_via_text() -> None:
    """Successful run with ``"no tests"`` in output → ``no_tests_found``."""
    result = _FakeResult(success=True, text="no tests found in scope")
    assert classify_test_result(result, (0, 0, 0)) == "no_tests_found"


def test_classify_no_tests_found_via_skipped() -> None:
    """Successful run with ``"skipped"`` verdict → ``no_tests_found``."""
    result = _FakeResult(success=True, text="VERDICT: SKIPPED")
    assert classify_test_result(result, (0, 0, 0)) == "no_tests_found"


def test_classify_collection_failed() -> None:
    """Failed run mentioning ``collection`` → ``collection_failed``."""
    result = _FakeResult(
        success=False,
        text="ERROR collecting tests: ImportError",
        raw_stderr="collection failure during conftest",
    )
    assert classify_test_result(result, (0, 0, 0)) == "collection_failed"


def test_classify_runtime_crash_via_timeout() -> None:
    """Failed run with ``timeout`` in ``error`` → ``runtime_crash``."""
    result = _FakeResult(
        success=False,
        text="some output",
        error="Timeout after 300s",
        raw_stderr="",
    )
    assert classify_test_result(result, (0, 0, 0)) == "runtime_crash"


def test_classify_runtime_crash_via_killed() -> None:
    """Failed run with ``killed`` in ``raw_stderr`` → ``runtime_crash``."""
    result = _FakeResult(
        success=False,
        text="some output",
        raw_stderr="Process killed by signal 9",
    )
    assert classify_test_result(result, (0, 0, 0)) == "runtime_crash"


def test_classify_capture_failed() -> None:
    """Failed run with empty text AND empty stderr → ``capture_failed``."""
    result = _FakeResult(success=False, text="", raw_stderr="", error=None)
    assert classify_test_result(result, (0, 0, 0)) == "capture_failed"


def test_classify_no_signal_catchall() -> None:
    """Failure with no diagnostic cues → ``no_signal``."""
    # success=False, text contains *something* (so not capture_failed),
    # no "collection", no "timeout", no "killed", and total=0.
    result = _FakeResult(
        success=False,
        text="some uninformative string",
        error=None,
        raw_stderr="",
    )
    assert classify_test_result(result, (0, 0, 0)) == "no_signal"


def test_redact_stderr_tail_drops_secret_lines() -> None:
    """Lines with secret markers are dropped before tail-trimming."""
    stderr = (
        "first ok line\n"
        "Authorization: Bearer abc123\n"
        "second ok line\n"
        "password=hunter2\n"
        "token=xxx\n"
        "api_key=sk-ABC\n"
        "third ok line\n"
    )
    out = redact_stderr_tail(stderr, tail_chars=1000)
    assert "Bearer" not in out
    assert "password=" not in out
    assert "token=" not in out
    assert "api_key=" not in out
    assert "first ok line" in out
    assert "second ok line" in out
    assert "third ok line" in out


def test_redact_stderr_tail_handles_empty() -> None:
    """Empty input returns ``""`` (not ``None``)."""
    assert redact_stderr_tail("", tail_chars=1000) == ""
    assert redact_stderr_tail(None, tail_chars=1000) == ""  # type: ignore[arg-type]


def test_redact_stderr_tail_respects_tail_size() -> None:
    """Tail trimming applies AFTER redaction so secrets cannot leak."""
    stderr = "a" * 2000 + "\nlast"
    out = redact_stderr_tail(stderr, tail_chars=100)
    assert len(out) == 100
    assert out.endswith("last")


@pytest.mark.parametrize(
    "diag,result,counts",
    [
        ("ok", _FakeResult(success=True, text=""), (1, 0, 1)),
        (
            "no_tests_found",
            _FakeResult(success=True, text="no tests collected"),
            (0, 0, 0),
        ),
        (
            "collection_failed",
            _FakeResult(success=False, text="collection error"),
            (0, 0, 0),
        ),
        (
            "runtime_crash",
            _FakeResult(
                success=False, text="x", error="timeout exceeded"
            ),
            (0, 0, 0),
        ),
        (
            "capture_failed",
            _FakeResult(success=False, text="", raw_stderr=""),
            (0, 0, 0),
        ),
        (
            "no_signal",
            _FakeResult(success=False, text="weird"),
            (0, 0, 0),
        ),
    ],
)
def test_classify_table(diag: str, result: _FakeResult, counts: tuple[int, int, int]) -> None:
    """Table-driven smoke test — one row per diagnosis."""
    assert classify_test_result(result, counts) == diag
