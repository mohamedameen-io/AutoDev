"""Host-capacity probing + parallelism resolver.

v0.10.0 introduces dynamic subprocess parallelism: rather than a fixed
``max_parallel_subprocesses=3``, the tournament runners ask this module
for a resource-aware value at startup and (optionally) ratchet it down
between passes if observed subprocess RSS exceeds a budget.

The module is intentionally tiny — pure functions over psutil + a single
dataclass. Keeping the surface narrow means downstream callers can mock
psutil at module scope (``monkeypatch.setattr(resource_probe.psutil, ...)``)
in tests without having to construct fake HostCapacity objects everywhere.

Exported surface:

* :class:`HostCapacity` — snapshot of host resources at probe time.
* :func:`probe_host` — reads psutil and returns a populated HostCapacity.
* :func:`resolve_parallelism` — maps (configured, capacity, role_mix,
  num_judges) → safe parallelism count.
* :func:`measure_subprocess_rss` — given a list of PIDs, returns the
  mean resident-set-size in MB across living children (used by
  per-pass adaptive ratcheting in :class:`tournament.core.Tournament`).
"""

from __future__ import annotations

from dataclasses import dataclass

import psutil

from autologging import get_logger


logger = get_logger(component="runtime.resource_probe")


@dataclass
class HostCapacity:
    """Snapshot of host resources at probe time.

    Attributes:
        cpu_count: Logical CPU count (from ``psutil.cpu_count()``). Falls
            back to ``4`` when psutil returns None (rare, but possible on
            exotic hosts / containers without ``/proc``).
        available_mem_gb: Free + reusable memory in GB at probe time
            (``psutil.virtual_memory().available`` divided by 1024**3).
        mean_subprocess_rss_mb: Mean resident-set-size in MB across the
            most recent batch of subprocess children. ``None`` until the
            first per-pass probe lands. Currently advisory — written by
            :class:`tournament.core.Tournament` but not consumed inside
            this module.
    """

    cpu_count: int
    available_mem_gb: float
    mean_subprocess_rss_mb: float | None = None


def probe_host() -> HostCapacity:
    """Read psutil and return a populated :class:`HostCapacity`.

    Cheap (~microseconds) — safe to call once per tournament startup.

    Returns:
        A fresh :class:`HostCapacity` snapshot. ``cpu_count`` falls back
        to ``4`` when psutil returns None; ``available_mem_gb`` is always
        a positive float on any host with a working ``virtual_memory()``.
    """
    cpu_count = psutil.cpu_count() or 4
    available_mem_gb = psutil.virtual_memory().available / 1024**3
    return HostCapacity(
        cpu_count=cpu_count,
        available_mem_gb=available_mem_gb,
    )
