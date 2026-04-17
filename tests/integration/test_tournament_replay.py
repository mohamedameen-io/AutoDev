"""Tournament replay test against autoreason reference fixture.

Validates that the recorded ``history.json`` structure matches the expected
schema: a list of pass records each with ``pass``, ``winner``, and ``scores``
keys. Does NOT re-run the tournament — it replays the fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixture path
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "autoreason"
_SAMPLE_RUN = _FIXTURES_DIR / "sample_run"
_HISTORY_JSON = _SAMPLE_RUN / "history.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_history() -> list[dict[str, object]]:
    """Load the recorded history.json fixture."""
    assert _HISTORY_JSON.exists(), f"Fixture not found: {_HISTORY_JSON}"
    raw = json.loads(_HISTORY_JSON.read_text())
    assert isinstance(raw, list), "history.json must be a JSON array"
    return raw  # type: ignore[return-value]


def _load_pass_result(pass_num: int) -> dict[str, object]:
    """Load a per-pass result.json from the fixture directory."""
    result_path = _SAMPLE_RUN / f"pass_{pass_num:02d}" / "result.json"
    assert result_path.exists(), f"Pass result not found: {result_path}"
    return json.loads(result_path.read_text())  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_history_json_is_valid_list() -> None:
    """history.json must be a non-empty JSON array."""
    history = _load_history()
    assert len(history) > 0, "history.json must contain at least one pass record"


@pytest.mark.integration
def test_history_entries_have_required_keys() -> None:
    """Every history entry must have pass, winner, and scores keys."""
    history = _load_history()
    for entry in history:
        assert "pass" in entry, f"Missing 'pass' key in entry: {entry}"
        assert "winner" in entry, f"Missing 'winner' key in entry: {entry}"
        assert "scores" in entry, f"Missing 'scores' key in entry: {entry}"


@pytest.mark.integration
def test_history_pass_numbers_are_sequential() -> None:
    """Pass numbers must be sequential starting from 1."""
    history = _load_history()
    for idx, entry in enumerate(history, start=1):
        assert entry["pass"] == idx, (
            f"Expected pass={idx}, got pass={entry['pass']}"
        )


@pytest.mark.integration
def test_history_winner_values_are_valid() -> None:
    """Winner must be one of 'A', 'B', or 'AB'."""
    history = _load_history()
    valid_winners = {"A", "B", "AB"}
    for entry in history:
        assert entry["winner"] in valid_winners, (
            f"Invalid winner '{entry['winner']}' in pass {entry['pass']}"
        )


@pytest.mark.integration
def test_history_scores_have_abc_keys() -> None:
    """Each scores dict must have A, B, and AB keys with numeric values."""
    history = _load_history()
    for entry in history:
        scores = entry["scores"]
        assert isinstance(scores, dict), f"scores must be a dict in pass {entry['pass']}"
        for key in ("A", "B", "AB"):
            assert key in scores, (
                f"Missing scores key '{key}' in pass {entry['pass']}"
            )
            assert isinstance(scores[key], (int, float)), (
                f"scores['{key}'] must be numeric in pass {entry['pass']}"
            )


@pytest.mark.integration
def test_pass_01_result_matches_history() -> None:
    """pass_01/result.json must match the first entry in history.json."""
    history = _load_history()
    first_entry = history[0]
    assert first_entry["pass"] == 1

    pass_result = _load_pass_result(1)

    # Core fields must match
    assert pass_result["pass"] == first_entry["pass"], (
        "pass number mismatch between result.json and history.json"
    )
    assert pass_result["winner"] == first_entry["winner"], (
        "winner mismatch between result.json and history.json"
    )
    assert pass_result["scores"] == first_entry["scores"], (
        "scores mismatch between result.json and history.json"
    )


@pytest.mark.integration
def test_pass_01_result_has_judge_details() -> None:
    """pass_01/result.json must contain judge_details with ranking info."""
    pass_result = _load_pass_result(1)
    assert "judge_details" in pass_result, "result.json must have judge_details"
    judge_details = pass_result["judge_details"]
    assert isinstance(judge_details, list), "judge_details must be a list"
    assert len(judge_details) > 0, "judge_details must be non-empty"

    for judge in judge_details:
        assert "ranking" in judge, f"judge entry missing 'ranking': {judge}"
        assert isinstance(judge["ranking"], list), "ranking must be a list"
        assert len(judge["ranking"]) > 0, "ranking must be non-empty"


@pytest.mark.integration
def test_history_winner_consistent_with_scores() -> None:
    """The winner in each entry must have the highest score."""
    history = _load_history()
    for entry in history:
        scores: dict[str, int | float] = entry["scores"]  # type: ignore[assignment]
        declared_winner: str = entry["winner"]  # type: ignore[assignment]
        max_score = max(scores.values())
        winner_score = scores[declared_winner]
        assert winner_score == max_score, (
            f"Pass {entry['pass']}: declared winner '{declared_winner}' "
            f"has score {winner_score} but max is {max_score} "
            f"(scores={scores})"
        )
