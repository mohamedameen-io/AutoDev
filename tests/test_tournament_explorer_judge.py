"""v0.17.0 S3: ``extract_explorer_findings`` + Explorer judge config.

The Explorer specialist judge produces an additional ``FINDINGS:`` block
beyond the standard RANKING. This module exposes a pure parser that
walks judge raw responses and pulls out structured findings, plus
helpers to convert them into ``TournamentEvent`` lessons (discard with
confidence 0.6).

These tests pin the parsing surface so :func:`tournament.core._run_judges`
can call into them without coupling to the orchestrator.
"""

from __future__ import annotations


def test_extract_findings_none() -> None:
    from tournament.core import extract_explorer_findings

    findings = extract_explorer_findings("RANKING: 1 2 3\n\nFINDINGS: NONE")
    assert findings == []


def test_extract_findings_single_line() -> None:
    from tournament.core import extract_explorer_findings

    raw = """
RANKING: 2 1 3

FINDINGS:
- [1] slop_pattern: three near-identical helpers
"""
    findings = extract_explorer_findings(raw)
    assert len(findings) == 1
    assert findings[0].candidate == "1"
    assert findings[0].category == "slop_pattern"
    assert "three near-identical helpers" in findings[0].description


def test_extract_findings_multiple() -> None:
    from tournament.core import extract_explorer_findings

    raw = """RANKING: 1 2 3

FINDINGS:
- [1] slop_pattern: dup helpers
- [2] hallucinated_api: db.execute_batch missing
- [3] cargo_cult: asyncio import unused
"""
    findings = extract_explorer_findings(raw)
    assert len(findings) == 3
    cats = {f.category for f in findings}
    assert cats == {"slop_pattern", "hallucinated_api", "cargo_cult"}


def test_extract_findings_unknown_category_skipped() -> None:
    """Unrecognized categories are silently dropped — graceful degradation."""
    from tournament.core import extract_explorer_findings

    raw = """RANKING: 1 2 3

FINDINGS:
- [1] slop_pattern: ok
- [2] not_a_real_category: noise
"""
    findings = extract_explorer_findings(raw)
    assert len(findings) == 1
    assert findings[0].category == "slop_pattern"


def test_extract_findings_no_block_returns_empty() -> None:
    from tournament.core import extract_explorer_findings

    raw = "RANKING: 1 2 3\nNo findings block at all."
    findings = extract_explorer_findings(raw)
    assert findings == []


def test_extract_findings_handles_extra_whitespace() -> None:
    from tournament.core import extract_explorer_findings

    raw = """RANKING: 1 2 3

FINDINGS:
   - [1]   spec_drift:   solved different problem
"""
    findings = extract_explorer_findings(raw)
    assert len(findings) == 1
    assert findings[0].category == "spec_drift"


def test_explorer_finding_has_expected_fields() -> None:
    from tournament.core import ExplorerFinding

    finding = ExplorerFinding(
        candidate="1",
        category="slop_pattern",
        description="x",
    )
    assert finding.candidate == "1"
    assert finding.category == "slop_pattern"
    assert finding.description == "x"


def test_config_explorer_enabled_default_false() -> None:
    from config.defaults import default_config

    cfg = default_config()
    # Default is opt-in (False) per privacy/cost guardrail.
    assert cfg.tournaments.plan.explorer_enabled is False
    assert cfg.tournaments.impl.explorer_enabled is False
    assert cfg.tournaments.phase_review.explorer_enabled is False
