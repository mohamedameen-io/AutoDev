"""v0.38.0 I1 (HK11): TTL-bounded sibling of :func:`is_huge_repo`.

Default :func:`is_huge_repo` uses ``functools.lru_cache`` keyed on the
resolved cwd path; that's correct for single-dispatch orchestrator
runs. Long-lived sessions (multi-hour resume loops, daemon processes)
opt in to :func:`is_huge_repo_with_ttl` so a repo crossing the
threshold mid-session is re-detected after the TTL expires.

These tests exercise three invariants:

- First call probes (counter increments).
- Second call within TTL returns the cached value (counter unchanged).
- Call after TTL expiry re-probes (counter increments again).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import orchestrator.repo_size as size_mod
from orchestrator.repo_size import (
    DEFAULT_HUGE_REPO_THRESHOLD,
    clear_cache,
    clear_ttl_cache,
    is_huge_repo_with_ttl,
)


@pytest.fixture(autouse=True)
def _drain_caches() -> None:
    clear_cache()
    clear_ttl_cache()
    yield
    clear_cache()
    clear_ttl_cache()


def _patch_counters(
    monkeypatch: pytest.MonkeyPatch, file_count: int
) -> dict[str, int]:
    """Replace subprocess.run + os.walk with stubs that bump a counter.

    Returns a mutable ``{"probes": n}`` dict the tests can inspect to
    verify whether the underlying probe re-ran.
    """
    stats: dict[str, int] = {"probes": 0}

    def _fake_run(*_a: object, **_kw: object) -> object:
        # Force the git fast-path to fail so the os.walk branch fires.
        class _CP:
            returncode = 1
            stdout = ""

        return _CP()

    def _fake_walk(_root: object) -> object:
        stats["probes"] += 1
        # Yield (root, dirs, files) once with synthetic file_count entries.
        yield ("", [], [f"f{i}" for i in range(file_count)])

    monkeypatch.setattr(size_mod.subprocess, "run", _fake_run)
    monkeypatch.setattr(size_mod.os, "walk", _fake_walk)
    return stats


def test_ttl_cache_first_call_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh cache → the first call materialises a probe."""
    stats = _patch_counters(monkeypatch, file_count=DEFAULT_HUGE_REPO_THRESHOLD + 1)
    assert is_huge_repo_with_ttl(tmp_path, ttl_s=3600) is True
    assert stats["probes"] == 1


def test_ttl_cache_second_call_within_ttl_uses_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Within the TTL window, repeated calls do NOT re-probe."""
    stats = _patch_counters(monkeypatch, file_count=DEFAULT_HUGE_REPO_THRESHOLD + 1)
    assert is_huge_repo_with_ttl(tmp_path, ttl_s=3600) is True
    assert is_huge_repo_with_ttl(tmp_path, ttl_s=3600) is True
    assert is_huge_repo_with_ttl(tmp_path, ttl_s=3600) is True
    assert stats["probes"] == 1


def test_ttl_cache_after_expiry_reprobes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After TTL elapses, the next call materialises a fresh probe.

    Drives time deterministically via a monkeypatched ``time.time``
    rather than sleeping in tests.
    """
    stats = _patch_counters(monkeypatch, file_count=DEFAULT_HUGE_REPO_THRESHOLD + 1)

    fake_now = {"t": 1_000.0}

    def _now() -> float:
        return fake_now["t"]

    monkeypatch.setattr(size_mod.time, "time", _now)

    # First probe at t=1000.
    assert is_huge_repo_with_ttl(tmp_path, ttl_s=60) is True
    assert stats["probes"] == 1

    # Within TTL (+30s) → still cached.
    fake_now["t"] = 1_030.0
    assert is_huge_repo_with_ttl(tmp_path, ttl_s=60) is True
    assert stats["probes"] == 1

    # After TTL (+120s) → fresh probe.
    fake_now["t"] = 1_120.0
    assert is_huge_repo_with_ttl(tmp_path, ttl_s=60) is True
    assert stats["probes"] == 2


def test_ttl_cache_escape_hatch_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cfg.huge_repo_overrides_disabled=True`` short-circuits to False
    BEFORE any probe — the underlying counter must not increment."""
    stats = _patch_counters(monkeypatch, file_count=DEFAULT_HUGE_REPO_THRESHOLD + 1)

    class _Cfg:
        huge_repo_overrides_disabled = True
        index_full_rebuild_threshold_files = DEFAULT_HUGE_REPO_THRESHOLD

    assert is_huge_repo_with_ttl(tmp_path, ttl_s=3600, cfg=_Cfg()) is False
    assert stats["probes"] == 0


def test_ttl_cache_clear_ttl_cache_drops_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``clear_ttl_cache`` invalidates entries even within the TTL window."""
    stats = _patch_counters(monkeypatch, file_count=DEFAULT_HUGE_REPO_THRESHOLD + 1)

    assert is_huge_repo_with_ttl(tmp_path, ttl_s=3600) is True
    assert stats["probes"] == 1

    clear_ttl_cache()

    assert is_huge_repo_with_ttl(tmp_path, ttl_s=3600) is True
    assert stats["probes"] == 2
