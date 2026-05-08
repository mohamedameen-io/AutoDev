"""Tests for :mod:`runtime.resource_probe`.

The module probes host capacity (CPU count, available memory) at tournament
start and provides a resolver that maps capacity + cohort size onto a safe
``max_parallel_subprocesses`` value. Tests mock psutil so they're hermetic
and deterministic across hosts.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest


# ---------------------------------------------------------------------------
# probe_host: returns a populated HostCapacity dataclass
# ---------------------------------------------------------------------------


def test_probe_host_returns_positive_values() -> None:
    """The unmocked probe returns sensible non-zero values on any modern host."""
    from runtime.resource_probe import HostCapacity, probe_host

    cap = probe_host()
    assert isinstance(cap, HostCapacity)
    assert cap.cpu_count >= 1
    assert cap.available_mem_gb > 0.0
    # Default: not yet measured.
    assert cap.mean_subprocess_rss_mb is None


def test_probe_host_uses_psutil_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """``probe_host`` reads psutil's cpu_count + virtual_memory.available."""
    from runtime import resource_probe

    monkeypatch.setattr(resource_probe.psutil, "cpu_count", lambda: 12)
    monkeypatch.setattr(
        resource_probe.psutil,
        "virtual_memory",
        lambda: Mock(available=8 * 1024**3),
    )

    cap = resource_probe.probe_host()
    assert cap.cpu_count == 12
    assert cap.available_mem_gb == pytest.approx(8.0, rel=1e-6)


def test_probe_host_handles_psutil_cpu_count_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``psutil.cpu_count()`` returns None, fall back to a safe default (4)."""
    from runtime import resource_probe

    monkeypatch.setattr(resource_probe.psutil, "cpu_count", lambda: None)
    monkeypatch.setattr(
        resource_probe.psutil,
        "virtual_memory",
        lambda: Mock(available=4 * 1024**3),
    )

    cap = resource_probe.probe_host()
    assert cap.cpu_count == 4
