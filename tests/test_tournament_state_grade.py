"""Tests for the v0.16.0 incumbent-grade sidecar JSON.

``TournamentArtifactStore.write_incumbent_after`` gains an optional
``grade`` parameter. When supplied, a sidecar
``incumbent_after_NN.grade.json`` is written next to the markdown so the
ladder can re-read the grade after a process restart.

Legacy callers that omit ``grade`` get the default ``"dev_best"`` and a
sidecar is still written — the file is the single source of truth and
older callers should not silently produce an ungraded artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tournament.state import TournamentArtifactStore


def test_write_incumbent_after_persists_grade_sidecar(tmp_path: Path) -> None:
    """An explicit ``grade`` writes a sidecar JSON with that value."""
    store = TournamentArtifactStore(tmp_path)
    md_path = store.write_incumbent_after(3, "incumbent text", grade="repeated")
    assert md_path == tmp_path / "incumbent_after_03.md"
    sidecar = tmp_path / "incumbent_after_03.grade.json"
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload == {"grade": "repeated", "pass_num": 3}


def test_legacy_callers_default_to_dev_best(tmp_path: Path) -> None:
    """Calling without ``grade`` emits a ``dev_best`` sidecar (default)."""
    store = TournamentArtifactStore(tmp_path)
    store.write_incumbent_after(1, "first incumbent")
    sidecar = tmp_path / "incumbent_after_01.grade.json"
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["grade"] == "dev_best"
    assert payload["pass_num"] == 1


def test_latest_incumbent_grade_reads_sidecar(tmp_path: Path) -> None:
    """``latest_incumbent_grade`` returns the grade of the highest pass."""
    store = TournamentArtifactStore(tmp_path)
    store.write_incumbent_after(1, "p1", grade="dev_best")
    store.write_incumbent_after(2, "p2", grade="pending_repeat")
    store.write_incumbent_after(3, "p3", grade="repeated")
    assert store.latest_incumbent_grade() == "repeated"


def test_latest_incumbent_grade_returns_none_when_no_incumbent(
    tmp_path: Path,
) -> None:
    """No on-disk incumbent → no grade to report."""
    store = TournamentArtifactStore(tmp_path)
    assert store.latest_incumbent_grade() is None


def test_latest_incumbent_grade_handles_missing_sidecar(tmp_path: Path) -> None:
    """A bare incumbent_after_NN.md with no sidecar (old-format artifact)
    must not crash — the helper returns None for that pass."""
    store = TournamentArtifactStore(tmp_path)
    # Simulate a legacy artifact: write the .md directly without sidecar.
    (tmp_path / "incumbent_after_05.md").write_text("legacy", encoding="utf-8")
    assert store.latest_incumbent_grade() is None


def test_write_incumbent_after_rejects_unknown_grade(tmp_path: Path) -> None:
    """Defensive guard: unknown grades raise rather than corrupt the sidecar."""
    store = TournamentArtifactStore(tmp_path)
    with pytest.raises(ValueError):
        store.write_incumbent_after(1, "bad", grade="totally-bogus")
