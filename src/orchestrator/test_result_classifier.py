"""Test runner self-diagnostic classifier (v0.32.0 Phase 3, Gap C).

Today the test gate writes ``<task>-test.json`` evidence with
``{"passed":0, "failed":0, "total":0, "output_text":""}`` whenever any
of these five distinct failure modes happen:

  1. No tests exist for the changed code (legitimate).
  2. Test runner crashed before collecting any tests.
  3. Tests ran and passed but stdout capture failed.
  4. Test runner output in unrecognized format.
  5. Test capture truncated/lost.

The orchestrator can't tell them apart and soft-blocks. This module
makes each one classifiable so the orchestrator can branch sensibly:
``no_tests_found`` proceeds, ``collection_failed`` retries once,
``runtime_crash`` retries with wider timeout, ``capture_failed``
retries once (infrastructure bug), ``no_signal`` soft-blocks with an
explicit reason.

The classifier is a *pure* synchronous data transformation — no I/O,
no async — so it composes cleanly with both the live orchestrator and
unit tests.
"""

from __future__ import annotations

from typing import Literal, Protocol


# Public diagnosis taxonomy. Mirror of the ``Literal`` on
# :class:`state.schemas.TestEvidence.diagnosis`.
TestDiagnosis = Literal[
    "ok",
    "no_tests_found",
    "collection_failed",
    "runtime_crash",
    "capture_failed",
    "turn_budget_exhausted",
    "no_signal",
]


# WS1: CLI ``subtype`` values that mean "the agent ran out of turns", NOT "the
# work is wrong / the runner is broken". ``error_max_turns`` is the per-attempt
# cap the CLI emits; ``error_max_turns_escalation_exhausted`` is the synthetic
# subtype the budget-escalation tracker returns once the per-(task, role)
# escalation ladder is spent. Kept in lockstep with
# ``orchestrator.execute_phase._TURN_EXHAUSTION_SUBTYPES`` (duplicated here
# rather than imported so this module stays free of the adapter/orchestrator
# dependency tree — see the module docstring).
_TURN_BUDGET_EXHAUSTION_SUBTYPES: frozenset[str] = frozenset(
    {"error_max_turns", "error_max_turns_escalation_exhausted"}
)


class _AgentResultLike(Protocol):
    """Structural type for the subset of ``AgentResult`` we read.

    Defined as a Protocol rather than importing ``AgentResult`` directly
    so the classifier remains importable from test modules without
    pulling the full adapter dependency tree, and so callers can pass
    lightweight stand-ins in tests.
    """

    success: bool
    text: str
    error: str | None
    raw_stderr: str
    # WS1: the CLI-reported dispatch subtype (``AgentResult.subtype``).
    # ``None`` when the adapter surfaced no subtype (e.g. a genuine subprocess
    # death with no parseable stdout). Read to attribute a turn-exhausted
    # ``test_engineer`` dispatch to ``turn_budget_exhausted`` instead of the
    # generic ``capture_failed``.
    subtype: str | None


def classify_test_result(
    agent_result: _AgentResultLike,
    parsed_counts: tuple[int, int, int],
) -> TestDiagnosis:
    """Classify a test runner outcome into one of six diagnoses.

    Heuristics applied in strict order (first match wins):

      1. ``total > 0`` → ``"ok"`` regardless of pass/fail. Once any
         tests were collected and reported, the runner clearly worked
         and downstream branches handle pass-vs-fail.
      1b. ``success=False`` and ``subtype`` is a turn-exhaustion subtype
         (``error_max_turns`` / ``error_max_turns_escalation_exhausted``)
         → ``"turn_budget_exhausted"``. WS1: the CLI's own signal that the
         ``test_engineer`` dispatch ran out of turns — an
         infrastructure-class budget failure, NOT a broken runner. Ordered
         after the ``total>0`` short-circuit (a run that reported counts
         clearly worked, so the subtype must not override it) and BEFORE
         all text heuristics (an empty turn-exhausted transcript would
         otherwise be misattributed to ``capture_failed`` — the exact
         defect WS1 fixes).
      2. ``success=True`` and ``"no test"`` or ``"skipped"`` in the
         lower-cased output text → ``"no_tests_found"``. Legitimate
         "nothing to test" path; the orchestrator should proceed.
      3. ``success=False`` and ``"collection"`` in the combined
         output+stderr (lower-cased) → ``"collection_failed"``.
         pytest's collection-error phase is distinct from a runtime
         crash and warrants a single retry before hard-fail.
      4. ``success=False`` and either ``"timeout"`` in ``error`` (lower-
         cased) or ``"killed"`` in ``raw_stderr`` (lower-cased) →
         ``"runtime_crash"``. Process died mid-flight; retry with a
         wider timeout.
      5. ``success=False`` and both ``text`` and ``raw_stderr`` empty
         → ``"capture_failed"``. Infrastructure bug — stdout/stderr
         pipe never produced output.
      6. Otherwise → ``"no_signal"``. Catch-all for genuinely
         inconclusive results; the orchestrator soft-blocks with an
         explicit "test result inconclusive — no diagnostic signal"
         reason rather than masquerading as ``capture_failed``.

    The order matters: ``"ok"`` comes first so a successful run with
    nonzero counts never accidentally falls into a text-matching trap
    (e.g. a passing test whose name happens to contain "skipped").
    """
    _passed, _failed, total = parsed_counts

    # 1. Any collected tests → ok.
    if total > 0:
        return "ok"

    # 1b. WS1: turn-budget exhaustion. Read defensively via ``getattr`` so
    # lightweight stand-ins that predate the ``subtype`` field (e.g. legacy
    # test stubs) don't raise ``AttributeError`` — mirrors the tolerant
    # ``agent_result.text or ""`` style below. Placed before every text
    # heuristic so an empty turn-exhausted transcript is attributed to the
    # CLI's own ``error_max_turns`` signal instead of ``capture_failed``.
    subtype = getattr(agent_result, "subtype", None)
    if not agent_result.success and subtype in _TURN_BUDGET_EXHAUSTION_SUBTYPES:
        return "turn_budget_exhausted"

    text = agent_result.text or ""
    text_lower = text.lower()
    stderr = agent_result.raw_stderr or ""
    stderr_lower = stderr.lower()
    error = agent_result.error or ""
    error_lower = error.lower()

    # 2. Successful run with explicit "no tests" / "skipped" phrasing.
    if agent_result.success and (
        "no test" in text_lower or "skipped" in text_lower
    ):
        return "no_tests_found"

    # 3. Collection-phase failure (pytest's ERRORS during collection).
    if not agent_result.success and "collection" in (
        text_lower + " " + stderr_lower
    ):
        return "collection_failed"

    # 4. Runtime crash signals — timeout in error or killed in stderr.
    if not agent_result.success and (
        "timeout" in error_lower or "killed" in stderr_lower
    ):
        return "runtime_crash"

    # 5. Empty capture — infrastructure bug.
    if not agent_result.success and not text and not stderr:
        return "capture_failed"

    # 6. Catch-all.
    return "no_signal"


def redact_stderr_tail(stderr: str, tail_chars: int = 1000) -> str:
    """Return the last ``tail_chars`` of ``stderr`` with secrets dropped.

    Drops any line containing one of the canonical secret-marker
    substrings (``Bearer``, ``password=``, ``token=``, ``api_key=``)
    case-insensitively. The tail is taken AFTER redaction so a tail
    boundary can never split a secret in half and leak the suffix.

    Returns "" if ``stderr`` is falsy. Public so the orchestrator can
    populate :attr:`state.schemas.TestEvidence.runner_stderr_tail` and
    tests can pin the redaction contract independently.
    """
    if not stderr:
        return ""
    secret_markers = ("bearer", "password=", "token=", "api_key=")
    cleaned_lines: list[str] = []
    for line in stderr.splitlines():
        line_lower = line.lower()
        if any(marker in line_lower for marker in secret_markers):
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    return cleaned[-tail_chars:]


__all__ = ["TestDiagnosis", "classify_test_result", "redact_stderr_tail"]
