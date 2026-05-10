"""Unit tests for ``scripts/calibrate_minimality_judge.py``.

The v1 stub has the mock judge return the gold rank verbatim. With
that stub:

  * Per-round Spearman ρ and Kendall τ must be 1.0.
  * Aggregate Spearman ρ across rounds must be 1.0.
  * All adversarial probes must return their no-fire defaults.
  * The Borda integer-cast diagnostic must detect the floor collapse
    at weight 0.5 (this is a *property of the aggregator*, not of the
    mock judge — so it fires even with a perfect judge).
  * The acceptance-criterion evaluator must pass all six checks.
  * The CLI entry point must exit 0.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_GOLD = (
    _REPO_ROOT
    / "tests"
    / "calibration"
    / "minimality_judge"
    / "gold_rankings.jsonl"
)


# Ensure the standalone script is importable as a module.
sys.path.insert(0, str(_SCRIPTS_DIR))

import calibrate_minimality_judge as cal  # noqa: E402  (path manip required)


@pytest.fixture
def synthetic_rounds() -> list[dict]:
    """Load the v1 synthetic gold corpus."""
    return cal.load_gold_corpus(_GOLD)


# ---------------------------------------------------------------------------
# Rank correlation helpers
# ---------------------------------------------------------------------------


def test_spearman_rho_identity_is_one() -> None:
    assert cal.spearman_rho([0, 1, 2], [0, 1, 2]) == pytest.approx(1.0)


def test_spearman_rho_reverse_is_minus_one() -> None:
    assert cal.spearman_rho([0, 1, 2], [2, 1, 0]) == pytest.approx(-1.0)


def test_kendall_tau_identity_is_one() -> None:
    assert cal.kendall_tau([0, 1, 2], [0, 1, 2]) == pytest.approx(1.0)


def test_kendall_tau_reverse_is_minus_one() -> None:
    assert cal.kendall_tau([0, 1, 2], [2, 1, 0]) == pytest.approx(-1.0)


def test_spearman_rho_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        cal.spearman_rho([0, 1], [0, 1, 2])


def test_kendall_tau_length_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        cal.kendall_tau([0, 1], [0, 1, 2])


# ---------------------------------------------------------------------------
# Borda integer-cast diagnostic — fires regardless of judge accuracy
# ---------------------------------------------------------------------------


def test_borda_diagnostic_detects_top_two_collapse_at_weight_half() -> None:
    diag = cal.borda_int_cast_diagnostic(weight=0.5, n=3)
    assert diag["int_collapse_top_two"] is True
    assert diag["int_collapse_bottom"] is True
    # Position 0: int(3 * 0.5) = 1; position 1: int(2 * 0.5) = 1.
    assert diag["contributions"][0]["int_floor"] == 1
    assert diag["contributions"][1]["int_floor"] == 1
    # Position 2: int(1 * 0.5) = 0.
    assert diag["contributions"][2]["int_floor"] == 0
    # Despite the collapse, the specialist must still be a meaningful
    # tiebreaker on near-tied peer scores.
    assert diag["meaningful_tiebreaker"] is True
    # Remediation guidance must be populated whenever collapse fires.
    assert diag["remediation"] is not None
    assert "1.0" in diag["remediation"]
    assert "voting.py" in diag["remediation"]


def test_borda_diagnostic_no_collapse_at_weight_one() -> None:
    diag = cal.borda_int_cast_diagnostic(weight=1.0, n=3)
    assert diag["int_collapse_top_two"] is False
    assert diag["int_collapse_bottom"] is False
    # No collapse → no remediation needed.
    assert diag["remediation"] is None
    # Contributions are integer-equal to raw at weight 1.0.
    for c in diag["contributions"]:
        assert c["int_floor"] == c["raw_contribution"]


# ---------------------------------------------------------------------------
# Corpus load + run pipeline (5 synthetic rounds, mock judge = gold)
# ---------------------------------------------------------------------------


def test_load_gold_corpus_returns_five_rounds(synthetic_rounds) -> None:
    assert len(synthetic_rounds) == 5
    task_ids = [r["task_id"] for r in synthetic_rounds]
    assert task_ids == [
        "synth_001",
        "synth_002",
        "synth_003",
        "synth_004",
        "synth_005",
    ]


def test_load_gold_corpus_validates_gold_rank_permutation(
    tmp_path: Path,
) -> None:
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        json.dumps(
            {
                "task_id": "bad",
                "task_spec": "x",
                "candidates": ["a", "b", "c"],
                "gold_rank": [0, 0, 1],
                "rater_notes": "",
            }
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="permutation"):
        cal.load_gold_corpus(bad)


def test_compute_calibration_metrics_perfect_judge(
    synthetic_rounds,
) -> None:
    judge = [r["gold_rank"] for r in synthetic_rounds]
    gold = [r["gold_rank"] for r in synthetic_rounds]
    rho, tau = cal.compute_calibration_metrics(judge, gold)
    assert rho == pytest.approx(1.0)
    assert tau == pytest.approx(1.0)


def test_run_calibration_passes_all_criteria_on_synthetic_corpus(
    synthetic_rounds,
) -> None:
    report = cal.run_calibration(synthetic_rounds)

    # Aggregate rank correlations are perfect under the identity mock.
    assert report.aggregate_spearman == pytest.approx(1.0)
    assert report.aggregate_kendall == pytest.approx(1.0)
    assert report.n_rounds == 5

    # Adversarial probes return their no-fire defaults.
    assert report.position_bias_changes == 0
    assert report.long_suffix_changes == 0
    assert report.fake_reasoning_improvements == 0

    # Self-preference is a placeholder (delta < 0.5 trivially).
    assert report.self_preference_avg_delta == pytest.approx(0.0)

    # Per-round metrics are populated and uniformly perfect.
    assert len(report.rounds) == 5
    for r in report.rounds:
        assert r.spearman == pytest.approx(1.0)
        assert r.kendall == pytest.approx(1.0)
        assert r.position_bias_changed is False
        assert r.long_suffix_rank_changed is False
        assert r.fake_reasoning_rank_improved is False

    # Borda diagnostic carries through the calibration report.
    assert report.borda_diagnostic["int_collapse_top_two"] is True
    assert report.borda_diagnostic["meaningful_tiebreaker"] is True

    # Acceptance: all six criteria pass.
    passed, criteria = cal.evaluate_acceptance(report)
    assert passed is True
    assert len(criteria) == 6
    for _name, ok, _detail in criteria:
        assert ok is True


# ---------------------------------------------------------------------------
# CLI entry point (in-process)
# ---------------------------------------------------------------------------


def test_cli_main_exits_zero_on_synthetic_corpus(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cal.main(["--gold", str(_GOLD), "--report", "markdown"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "minimality_judge calibration report" in out
    assert "All criteria pass: **YES**" in out


def test_cli_main_jsonl_report_is_valid_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cal.main(["--gold", str(_GOLD), "--report", "jsonl"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["n_rounds"] == 5
    assert payload["aggregate_spearman"] == pytest.approx(1.0)
    assert payload["borda_diagnostic"]["int_collapse_top_two"] is True


def test_cli_main_returns_2_on_missing_corpus(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "does-not-exist.jsonl"
    rc = cal.main(["--gold", str(missing)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "gold corpus not found" in err
