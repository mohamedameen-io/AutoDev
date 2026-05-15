"""Tests for v0.31.0 (Phase 5.3) codebase language profile."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from runtime.language_profile import (
    DOMINANT_THRESHOLD,
    compute_language_profile,
    get_dominant_language,
)


def _touch(path: Path, content: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_single_language(tmp_path: Path) -> None:
    """Pure-Python repo profiles to ``{python: 1.0}``."""
    for i in range(3):
        _touch(tmp_path / f"src/mod_{i}.py")

    profile = compute_language_profile(tmp_path)
    assert profile == {"python": 1.0}
    assert get_dominant_language(profile) == "python"


def test_mixed(tmp_path: Path) -> None:
    """A mixed Python+TS repo reports both languages summing to 1.0."""
    for i in range(2):
        _touch(tmp_path / f"src/mod_{i}.py")
    for i in range(3):
        _touch(tmp_path / f"web/comp_{i}.ts")

    profile = compute_language_profile(tmp_path)
    assert set(profile.keys()) == {"python", "typescript"}
    assert abs(sum(profile.values()) - 1.0) < 1e-9
    # Python weight (100) per file vs typescript (50) per file:
    # py = 2*100 = 200, ts = 3*50 = 150, total 350.
    assert abs(profile["python"] - 200 / 350) < 1e-9
    assert abs(profile["typescript"] - 150 / 350) < 1e-9


def test_empty(tmp_path: Path) -> None:
    """Empty / language-free repo falls back to ``{other: 1.0}``."""
    # Make a non-source file so iter_repo_files has something to walk.
    _touch(tmp_path / "README.md", "# hi")
    profile = compute_language_profile(tmp_path)
    assert profile == {"other": 1.0}
    assert get_dominant_language(profile) == "other"


def test_dominant_language_threshold() -> None:
    """``get_dominant_language`` returns ``mixed`` when no language hits 40%."""
    # 35% / 35% / 30% -- nothing crosses 40%.
    profile = {"python": 0.35, "typescript": 0.35, "go": 0.30}
    assert get_dominant_language(profile) == "mixed"

    # 40% just barely qualifies.
    on_threshold = {"python": DOMINANT_THRESHOLD, "javascript": 0.60}
    # Tie-break inside max(): the language with the highest share wins.
    assert get_dominant_language(on_threshold) == "javascript"

    # Single-winner case.
    clear_winner = {"python": 0.80, "javascript": 0.20}
    assert get_dominant_language(clear_winner) == "python"


def test_cache_hit_skips_scan(tmp_path: Path) -> None:
    """Second call reads the cached profile rather than rescanning."""
    _touch(tmp_path / "src/main.py")
    p1 = compute_language_profile(tmp_path)

    cache_file = tmp_path / ".autodev" / "language_profile.json"
    assert cache_file.exists()

    # Manually corrupt the cache to a sentinel value -- if the second
    # call rescanned, it would overwrite the sentinel; if it cache-hit,
    # the sentinel comes back.
    sentinel = {"profile": {"python": 0.5, "go": 0.5}}
    cache_file.write_text(json.dumps(sentinel), encoding="utf-8")
    # Bump the cache mtime forwards so the cache is newer than every
    # source file (defends against filesystems with coarse mtime res).
    future = time.time() + 60
    os.utime(cache_file, (future, future))

    p2 = compute_language_profile(tmp_path)
    assert p2 == {"python": 0.5, "go": 0.5}
    # Sanity: original profile was different.
    assert p1 != p2


def test_cache_invalidation_on_source_change(tmp_path: Path) -> None:
    """A source file newer than the cache forces a recompute."""
    _touch(tmp_path / "src/main.py")
    p1 = compute_language_profile(tmp_path)
    assert p1 == {"python": 1.0}

    # Backdate the cache so a new source file is unambiguously newer.
    cache_file = tmp_path / ".autodev" / "language_profile.json"
    past = time.time() - 3600
    os.utime(cache_file, (past, past))

    # Add a TS file (must end up newer than the cache).
    ts_file = tmp_path / "src/app.ts"
    _touch(ts_file)
    now = time.time()
    os.utime(ts_file, (now, now))

    p2 = compute_language_profile(tmp_path)
    assert "typescript" in p2
    # Recomputed cache should reflect the new mix.
    assert set(p2.keys()) == {"python", "typescript"}
