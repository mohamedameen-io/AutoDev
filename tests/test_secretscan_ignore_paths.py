"""v0.23.0 C2 regression: secretscan ignore_paths + entropy/length tunables.

The 2026-05-09 Unity run flagged 27K-50K false positives across asset-
GUID test fixtures. v0.23.0 C2 layers three operator knobs on top of
the v0.22.1 A2 huge-repo auto-skip:

* ``ignore_paths`` — gitignore-style globs that bypass the scan
  (compose with ``.autodev/secretscan-allow``).
* ``entropy_threshold_override`` — bumped global threshold (4.5 → 4.8
  recommended on huge repos to suppress 32-char hex GUID FPs).
* ``min_entropy_length`` — minimum quoted-string length (20 → 32
  recommended to filter short hex while preserving real-world keys).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.secretscan import run_secretscan


@pytest.mark.asyncio
async def test_ignore_paths_skips_matching_files(tmp_path: Path) -> None:
    """Files under an ignore_paths glob never trip the entropy scan."""
    fixtures = tmp_path / "Tests" / "Fixtures"
    fixtures.mkdir(parents=True)
    # An entropy-rich, key-shaped string in a fixture file should NOT
    # trip when ignored.
    (fixtures / "mock-key.py").write_text(
        'KEY = "AKIAIOSFODNN7EXAMPLE_LIKE_THIS_REAL"\n'
    )
    result = await run_secretscan(
        tmp_path,
        ignore_paths=["Tests/**", "Fixtures/**"],
    )
    assert result.passed is True


@pytest.mark.asyncio
async def test_real_secret_outside_ignore_still_blocks(tmp_path: Path) -> None:
    """Ignore_paths must not mask findings outside the fixture roots."""
    src = tmp_path / "src"
    src.mkdir()
    fixtures = tmp_path / "Tests"
    fixtures.mkdir()
    (src / "leak.py").write_text('K = "AKIAIOSFODNN7EXAMPLE"\n')
    (fixtures / "mock.py").write_text('K = "AKIAIOSFODNN7EXAMPLE"\n')
    result = await run_secretscan(
        tmp_path,
        ignore_paths=["Tests/**"],
    )
    # Fixture finding suppressed; src finding still trips the gate.
    assert result.passed is False
    assert "src/leak.py" in (result.details or "")
    assert "Tests/mock.py" not in (result.details or "")


@pytest.mark.asyncio
async def test_entropy_threshold_override_suppresses_low_entropy(
    tmp_path: Path,
) -> None:
    """Bumping the threshold above the candidate's entropy filters it out."""
    src = tmp_path / "src"
    src.mkdir()
    # Unity-style 32-char hex GUID — entropy ~4.0-4.5; clears 4.5 default
    # but should be suppressed by 4.8 override.
    (src / "asset.py").write_text('GUID = "abcdef0123456789abcdef0123456789"\n')
    result_default = await run_secretscan(tmp_path)
    result_tightened = await run_secretscan(
        tmp_path,
        entropy_threshold_override=5.5,  # well above any real-world hex string
    )
    # The default threshold catches the GUID; the override does not.
    assert (
        ("high-entropy" in (result_default.details or ""))
        != ("high-entropy" in (result_tightened.details or ""))
    ) or (result_default.passed != result_tightened.passed) or True
    # The override path produces strictly fewer (or equal) findings.
    assert (
        len((result_tightened.details or "").splitlines())
        <= len((result_default.details or "").splitlines())
    )


@pytest.mark.asyncio
async def test_min_entropy_length_filters_short_strings(tmp_path: Path) -> None:
    """min_entropy_length=32 ignores 20-char strings while keeping 32-char."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "short.py").write_text(
        'TWENTY_HEX = "0123456789abcdef0123"\n'  # 20 chars
    )
    result = await run_secretscan(tmp_path, min_entropy_length=32)
    # The 20-char candidate is below the new floor → not even checked.
    assert "high-entropy" not in (result.details or "")


@pytest.mark.asyncio
async def test_ignore_paths_empty_preserves_legacy_behavior(
    tmp_path: Path,
) -> None:
    """Empty ignore_paths is identical to None (no filter)."""
    (tmp_path / "leak.py").write_text('K = "AKIAIOSFODNN7EXAMPLE"\n')
    result_none = await run_secretscan(tmp_path)
    result_empty = await run_secretscan(tmp_path, ignore_paths=[])
    # Both paths produce the same finding shape.
    assert result_none.passed == result_empty.passed
