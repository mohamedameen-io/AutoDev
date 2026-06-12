"""Tests: AutoDev-internal drift-verifier META findings are scoped OUT of the
corrective_direction text fed to corrective generation (Gap 6 / Tier J).

Root cause (real run, UUM-136411 on the 358k-file repo): when the
``critic_drift_verifier`` response is malformed for AutoDev's parser (missing
``VERDICT:`` line, a non-standard verdict word like ``PASS`` / ``PARTIAL
PASS``, an empty response, an unregistered agent, or a convergence abort),
:func:`orchestrator.drift_verifier._parse_drift_response` records AutoDev's own
plumbing diagnostics as ``drift_findings`` (prefixed ``drift_verifier:`` /
``drift_convergence_failure:``). Those strings were then spliced verbatim into
``corrective_direction`` and parsed into corrective tasks (``0.c2`` =
"drift_verifier: verdict PASS treated as NEEDS_REVISION", ``0.c3`` =
"drift_verifier: response missing VERDICT line ..."). The developer then loops
trying to "fix" AutoDev's own verdict-parsing in the target repo and never
produces the actual code fix.

The fix scopes AutoDev's own run-mechanics OUT of the corrective text while
keeping SUBSTANTIVE, target-repo findings (``task X: MISSING/DRIFTED``, the
critic's ``drift report: ...`` body). The drift verdict's ``passed`` flag still
drives accept/reject control flow — only its diagnostic TEXT stops leaking.
"""

from __future__ import annotations

from orchestrator.drift_verifier import (
    _is_meta_finding,
    partition_drift_findings,
)


# ── _is_meta_finding ────────────────────────────────────────────────────────


def test_missing_verdict_line_is_meta() -> None:
    assert _is_meta_finding(
        "drift_verifier: response missing VERDICT line "
        "(skeptical fallback to failure)"
    )


def test_nonstandard_verdict_is_meta() -> None:
    assert _is_meta_finding(
        "drift_verifier: non-standard verdict 'PASS' treated as NEEDS_REVISION"
    )


def test_empty_response_is_meta() -> None:
    assert _is_meta_finding("drift_verifier: empty response")


def test_agent_not_registered_is_meta() -> None:
    assert _is_meta_finding(
        "drift_verifier: critic_drift_verifier agent not registered — "
        "cannot dispatch verification"
    )


def test_convergence_failure_is_meta() -> None:
    assert _is_meta_finding(
        "drift_convergence_failure: corrective patch is ≥90% identical to "
        "prior (similarity=0.95); escalating"
    )


def test_meta_prefix_is_case_insensitive_and_strips_leading_ws() -> None:
    assert _is_meta_finding("  DRIFT_VERIFIER: response missing VERDICT line")


def test_task_drift_finding_is_not_meta() -> None:
    # Substantive: a real per-task drift result about target-repo code.
    assert not _is_meta_finding("task 1.1: DRIFTED")


def test_task_missing_finding_is_not_meta() -> None:
    assert not _is_meta_finding("task 2.3: MISSING")


def test_drift_report_body_is_not_meta() -> None:
    # Substantive: the critic's own DRIFT REPORT body.
    assert not _is_meta_finding(
        "drift report: Unplanned additions: added unrelated helper | "
        "Dropped tasks: task 3.2 not implemented"
    )


# ── partition_drift_findings ────────────────────────────────────────────────


def test_partition_keeps_substantive_drops_meta() -> None:
    """A mix of meta + substantive findings: substantive kept, meta dropped."""
    findings = [
        "drift_verifier: non-standard verdict 'PASS' treated as NEEDS_REVISION",
        "task 1.1: DRIFTED",
        "drift report: Dropped tasks: 3.2",
    ]
    substantive, meta = partition_drift_findings(findings)
    assert substantive == ["task 1.1: DRIFTED", "drift report: Dropped tasks: 3.2"]
    assert meta == [
        "drift_verifier: non-standard verdict 'PASS' treated as NEEDS_REVISION"
    ]


def test_partition_all_meta_yields_no_substantive() -> None:
    """The real UUM-136411 case: only meta findings → nothing substantive."""
    findings = [
        "drift_verifier: response missing VERDICT line (skeptical fallback to failure)",
        "drift_verifier: non-standard verdict 'PARTIAL PASS — 11 of 12' treated as NEEDS_REVISION",
    ]
    substantive, meta = partition_drift_findings(findings)
    assert substantive == []
    assert meta == findings


def test_partition_all_substantive_yields_no_meta() -> None:
    """A clean target-repo review: every finding is substantive, none dropped."""
    findings = ["task 1.1: MISSING", "task 1.2: DRIFTED"]
    substantive, meta = partition_drift_findings(findings)
    assert substantive == findings
    assert meta == []


def test_partition_empty_input() -> None:
    substantive, meta = partition_drift_findings([])
    assert substantive == []
    assert meta == []
