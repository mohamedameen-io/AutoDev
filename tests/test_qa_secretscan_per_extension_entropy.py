"""Tests for v0.19.0 per-extension entropy thresholds."""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.secretscan import _shannon_entropy, _high_entropy_strings, run_secretscan


def test_default_threshold_unchanged() -> None:
    """Default 4.5 threshold preserved when no extension-specific override."""
    # Mid-entropy (~4.7 bits) — passes default 4.5.
    s = "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr"
    assert _shannon_entropy(s) > 4.5
    out = _high_entropy_strings(f'x = "{s}"', extension=".py")
    assert s in out


def test_cpp_threshold_higher_than_default() -> None:
    """``.cpp/.h`` files use a 5.0 threshold (Unity-class C++ identifiers)."""
    s = "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr"
    e = _shannon_entropy(s)
    assert 4.5 <= e < 5.0
    out_default = _high_entropy_strings(f'x = "{s}"', extension=".py")
    out_cpp = _high_entropy_strings(f'x = "{s}";', extension=".cpp")
    assert s in out_default
    assert s not in out_cpp


def test_yaml_threshold_higher_than_default() -> None:
    """``.yaml/.yml`` files use a 5.5 threshold (build manifests)."""
    s = "aBc1dEf2gHi3jKl4mNo5pQr6sTu"  # ~4.75 bits
    e = _shannon_entropy(s)
    assert 4.5 <= e < 5.5
    out_yaml = _high_entropy_strings(f'key: "{s}"', extension=".yaml")
    assert s not in out_yaml


@pytest.mark.asyncio
async def test_cpp_file_high_entropy_skipped(tmp_path: Path) -> None:
    """A medium-entropy string in a ``.cpp`` file does not fire."""
    s = "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr"  # ~4.7 bits
    (tmp_path / "code.cpp").write_text(f'const char* k = "{s}";\n')
    result = await run_secretscan(tmp_path)
    # Default threshold would catch this at 4.5; C++ threshold of 5.0 lets it pass.
    assert result.passed, result.details


@pytest.mark.asyncio
async def test_per_extension_thresholds_override(tmp_path: Path) -> None:
    """Programmatic override via *per_extension_thresholds* arg works."""
    s = "Ab1Cd2Ef3Gh4Ij5Kl6Mn7Op8Qr"  # ~4.7 bits
    (tmp_path / "code.cpp").write_text(f'const char* k = "{s}";\n')
    # Override C++ threshold back to 4.5: entropy now triggers.
    result = await run_secretscan(
        tmp_path, per_extension_thresholds={".cpp": 4.5}
    )
    assert not result.passed
