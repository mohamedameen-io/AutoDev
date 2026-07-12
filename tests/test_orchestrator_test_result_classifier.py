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
    parse_bugs_found,
    redact_stderr_tail,
    reports_missing_change,
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
    # WS1: the CLI-reported dispatch subtype (``AgentResult.subtype``). The
    # classifier reads it to attribute a turn-exhausted ``test_engineer``
    # dispatch to ``turn_budget_exhausted`` rather than ``capture_failed``.
    subtype: str | None = None


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


# ── WS1: turn-budget exhaustion attribution ──────────────────────────────


def test_classify_turn_budget_exhausted_error_max_turns() -> None:
    """A failed dispatch with subtype ``error_max_turns`` and no captured
    counts → ``turn_budget_exhausted`` (NOT ``capture_failed``).

    This is the core WS1 contract: the CLI's own ``error_max_turns`` signal
    is the distinguisher the classifier previously ignored, so a
    turn-exhausted ``test_engineer`` was misattributed to ``capture_failed``.
    """
    result = _FakeResult(
        success=False, text="", raw_stderr="", subtype="error_max_turns"
    )
    assert classify_test_result(result, (0, 0, 0)) == "turn_budget_exhausted"


def test_classify_turn_budget_exhausted_escalation_exhausted() -> None:
    """The synthetic ``error_max_turns_escalation_exhausted`` subtype (emitted
    once the per-(task, role) budget-escalation ladder is spent) also maps to
    ``turn_budget_exhausted``."""
    result = _FakeResult(
        success=False,
        text="",
        raw_stderr="",
        subtype="error_max_turns_escalation_exhausted",
    )
    assert classify_test_result(result, (0, 0, 0)) == "turn_budget_exhausted"


def test_classify_total_wins_over_turn_budget_subtype() -> None:
    """``total > 0`` short-circuits to ``"ok"`` even when the subtype is a
    turn-exhaustion subtype — a run that reported counts clearly worked, so
    the subtype must not override the collected result."""
    result = _FakeResult(
        success=False, text="RESULTS: passed=5 failed=0 total=5",
        subtype="error_max_turns",
    )
    assert classify_test_result(result, (5, 0, 5)) == "ok"


def test_classify_capture_failed_still_fires_without_turn_subtype() -> None:
    """Regression pin: a genuinely unexplained empty failure (no
    turn-exhaustion subtype) still classifies as ``capture_failed``. The WS1
    rule must be additive — it must NOT swallow the existing capture-failed
    path for failures the CLI did not attribute to turn exhaustion.
    """
    result = _FakeResult(
        success=False, text="", raw_stderr="", subtype=None
    )
    assert classify_test_result(result, (0, 0, 0)) == "capture_failed"


def test_classify_non_turn_subtype_does_not_map_to_turn_budget() -> None:
    """A non-turn-exhaustion subtype (e.g. an infra ``rate_limited``) does NOT
    map to ``turn_budget_exhausted``; empty text/stderr still falls to
    ``capture_failed`` via the existing rung."""
    result = _FakeResult(
        success=False, text="", raw_stderr="", subtype="rate_limited"
    )
    assert classify_test_result(result, (0, 0, 0)) == "capture_failed"


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


# ---------------------------------------------------------------------------
# WS4: ``BUGS FOUND:`` contract parser + missing-change detector.
# ---------------------------------------------------------------------------


def test_parse_bugs_found_absent_section_returns_none() -> None:
    """No ``BUGS FOUND:`` line → ``None``."""
    assert parse_bugs_found("RESULTS: passed=1 failed=0 total=1\nall good") is None


def test_parse_bugs_found_none_body_returns_none() -> None:
    """An explicit ``BUGS FOUND: none`` (and punctuated variants) → ``None``."""
    assert parse_bugs_found("BUGS FOUND: none") is None
    assert parse_bugs_found("BUGS FOUND: none.") is None
    assert parse_bugs_found("BUGS FOUND: None — clean") is None


def test_parse_bugs_found_extracts_body() -> None:
    """A real bug body is returned stripped."""
    body = parse_bugs_found(
        "RESULTS: passed=0 failed=0 total=0\n"
        "BUGS FOUND: off-by-one in range() bound\n"
    )
    assert body == "off-by-one in range() bound"


def test_parse_bugs_found_stops_at_next_section_header() -> None:
    """The body does not bleed into a trailing recognised section."""
    body = parse_bugs_found(
        "BUGS FOUND: the source change is missing from this diff\n"
        "COVERAGE: 42%\n"
    )
    assert body == "the source change is missing from this diff"
    assert "COVERAGE" not in (body or "")


def test_reports_missing_change_django_shape() -> None:
    """The django-10914 quote is detected as a missing-change signal."""
    text = (
        "RESULTS: passed=0 failed=0 total=0\n"
        "No tests found that exercise this change.\n"
        "BUGS FOUND: global_settings.py:307 still defines "
        "FILE_UPLOAD_PERMISSIONS = None; the source change is missing "
        "from this diff.\n"
    )
    assert reports_missing_change(text) is True


def test_reports_missing_change_false_for_bugs_none() -> None:
    """``BUGS FOUND: none`` is not a missing-change signal (legitimate case)."""
    assert reports_missing_change("BUGS FOUND: none") is False


def test_reports_missing_change_false_for_absent_section() -> None:
    """No ``BUGS FOUND:`` section → not a missing-change signal."""
    assert reports_missing_change("RESULTS: passed=0 failed=0 total=0") is False


def test_reports_missing_change_false_for_unrelated_bug() -> None:
    """A real bug that is NOT about a missing change must not over-trigger."""
    text = "BUGS FOUND: divide-by-zero when denominator is 0"
    assert reports_missing_change(text) is False


def test_reports_missing_change_false_for_benign_no_source_change_prose() -> None:
    """I-1 regression: benign "no source change was necessary" must NOT trip.

    A docs / no-op task whose test_engineer reports there were no source changes
    to make is a LEGITIMATE no-test-surface case. A state-framed phrasing like
    "no source change" must NOT be read as a missing-EXPECTED-change signal —
    that would be the opposite-direction regression WS-4 exists to avoid.
    """
    body = "BUGS FOUND: N/A. No source change was necessary for this docs task."
    assert reports_missing_change(body) is False


def test_reports_missing_change_false_for_source_unchanged_prose() -> None:
    """I-1 regression: benign "source is unchanged" narration must NOT trip."""
    body = "BUGS FOUND: Note: the public API and source is unchanged from baseline."
    assert reports_missing_change(body) is False
