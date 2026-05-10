"""Phase 2 (anti-bloat): closed-vocabulary contract for seed-pack smell tags.

Every entry in ``seeds/anti_bloat_v1.jsonl`` MUST carry
``metadata.smell_name`` drawn from the closed vocabulary used by reviewer
prompt enhancements (Phase 3) and the minimality_judge specialist
(Phase 4). Drift in this set silently breaks downstream consumers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PACK_PATH = REPO_ROOT / "seeds" / "anti_bloat_v1.jsonl"

CLOSED_SMELL_VOCAB = {
    "long_method",
    "duplicate_code",
    "dead_code",
    "feature_envy",
    "speculative_generality",
    "shotgun_surgery",
    "primitive_obsession",
    "complex_conditional",
    "large_class",
}

ALLOWED_RULE_SOURCES = {
    "karpathy",
    "austin",
    "bloatware-detector",
    "pyexamine",
}


def _load_entries() -> list[dict]:
    text = PACK_PATH.read_text()
    out: list[dict] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        out.append(json.loads(s))
    return out


def test_pack_file_exists() -> None:
    assert PACK_PATH.exists(), f"missing seed pack: {PACK_PATH}"


def test_pack_has_18_entries() -> None:
    entries = _load_entries()
    assert len(entries) == 18, f"expected 18 entries, got {len(entries)}"


@pytest.mark.parametrize("entry_idx", range(18))
def test_each_entry_smell_name_in_vocab(entry_idx: int) -> None:
    entries = _load_entries()
    entry = entries[entry_idx]
    smell = entry.get("metadata", {}).get("smell_name")
    assert smell is not None, f"entry {entry_idx} missing metadata.smell_name"
    assert smell in CLOSED_SMELL_VOCAB, (
        f"entry {entry_idx} smell_name={smell!r} not in closed vocab "
        f"{sorted(CLOSED_SMELL_VOCAB)}"
    )


@pytest.mark.parametrize("entry_idx", range(18))
def test_each_entry_rule_source_in_vocab(entry_idx: int) -> None:
    entries = _load_entries()
    entry = entries[entry_idx]
    src = entry.get("metadata", {}).get("rule_source")
    assert src in ALLOWED_RULE_SOURCES, (
        f"entry {entry_idx} rule_source={src!r} not in {sorted(ALLOWED_RULE_SOURCES)}"
    )


def test_pack_distribution_matches_plan() -> None:
    """Plan: 5 karpathy, 5 austin, 5 bloatware-detector, 3 pyexamine."""
    entries = _load_entries()
    counts: dict[str, int] = {}
    for e in entries:
        src = e["metadata"]["rule_source"]
        counts[src] = counts.get(src, 0) + 1
    assert counts == {
        "karpathy": 5,
        "austin": 5,
        "bloatware-detector": 5,
        "pyexamine": 3,
    }, f"unexpected distribution: {counts}"


def test_each_entry_has_required_fields() -> None:
    entries = _load_entries()
    required = {
        "id",
        "timestamp",
        "role_source",
        "tier",
        "text",
        "confidence",
        "applied_count",
        "succeeded_after_count",
        "confirmations",
        "metadata",
    }
    for i, entry in enumerate(entries):
        missing = required - set(entry.keys())
        assert not missing, f"entry {i} missing fields: {missing}"
        assert entry["tier"] == "hive", f"entry {i} tier must be hive"
        assert entry["role_source"] == "seed_pack:anti_bloat_v1"
        assert entry["confidence"] == 0.85
        assert entry["metadata"].get("lane") == "anti_bloat"
        assert entry["metadata"].get("source_pack") == "anti_bloat_v1"
        assert entry["id"] == f"ab_v1_{i + 1:03d}"


def test_text_lengths_reasonable() -> None:
    """Texts should be short and actionable (<= 250 chars)."""
    entries = _load_entries()
    for i, entry in enumerate(entries):
        text = entry["text"]
        assert len(text) <= 250, f"entry {i} text too long ({len(text)} chars): {text[:60]}..."
        assert len(text.strip()) > 0
