"""v0.22.4 B4: NFC unicode normalization in path validator."""

from __future__ import annotations

import unicodedata

from orchestrator.path_validator import normalize_path


def test_decomposed_normalizes_to_canonical_composed() -> None:
    """An NFD-decomposed input normalizes to its NFC-composed canonical form."""
    decomposed = unicodedata.normalize("NFD", "foó.py")
    composed = unicodedata.normalize("NFC", "foó.py")
    assert decomposed != composed  # codepoints differ
    assert normalize_path(decomposed) == composed
    assert normalize_path(composed) == composed
