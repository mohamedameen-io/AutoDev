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
from typing import Literal

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


# Memory constants used by ``resolve_parallelism``. Kept module-private so
# tests can monkeypatch them if a future hardware regime ships heavier
# subprocess footprints. Values were chosen empirically: a ``claude -p``
# subprocess in the QNX run averaged ~800-1100 MB resident; 1.5 GB
# per-subprocess + 4.0 GB headroom held in v0.7.0 / v0.8.0 / v0.9.0
# field testing without OOM'ing.
_MEM_HEADROOM_GB = 4.0
_MEM_PER_SUBPROCESS_GB = 1.5

# Ceiling chosen to defend against pathological configurations (e.g. a
# user setting ``num_judges=64`` on a beefy host). 16 is high enough that
# realistic 7-judge ensembles are never throttled and low enough that
# misconfiguration won't fork-bomb the laptop.
_PARALLELISM_CEILING = 16

# CPU reservation: leave 2 cores for the OS scheduler + the parent
# orchestrator process. Empirical: with cpu_count - 2, the laptop fan
# stays in normal range during a tournament.
_CPU_RESERVE = 2


def resolve_parallelism(
    configured: int | None,
    capacity: HostCapacity,
    role_mix: Literal["plan", "impl", "phase_review"],
    num_judges: int,
) -> int:
    """Resolve a safe ``max_parallel_subprocesses`` for the next tournament.

    The resolution algorithm:

    * If ``configured`` is not None, return ``max(1, configured)``. This is
      the backward-compat path: explicit user ints bypass the probe.
    * Otherwise, take the lesser of:

      * memory cap: ``(available_mem_gb - 4.0 GB) / 1.5 GB-per-subprocess``
      * CPU cap: ``cpu_count - 2``
      * cohort cap: ``num_judges``
      * absolute ceiling: ``16``

      then floor at ``1``.

    Logs the resolution as ``tournament.parallelism_resolved`` with the
    structured fields ``{chosen, cpus, memory_gb, num_judges, role_mix}``
    for forensic inspection in tournament artifacts.

    Args:
        configured: Operator-supplied int from
            ``cfg.tournaments.max_parallel_subprocesses``. ``None`` means
            "auto-resolve from capacity"; an int means "use this value".
        capacity: Snapshot from :func:`probe_host`.
        role_mix: Tournament role identifier (``plan``, ``impl``, or
            ``phase_review``). Currently logged for forensics; reserved
            for v0.10.1's per-role weighted parallelism (DEFERRED in
            this release).
        num_judges: Effective judge cohort size for this tournament. The
            resolver never returns more workers than there are judges
            because excess workers would idle.

    Returns:
        A positive int suitable as ``cfg.max_parallel_subprocesses`` for
        :class:`tournament.core.TournamentConfig`. Always >= 1.
    """
    if configured is not None:
        chosen = max(1, configured)
        logger.info(
            "tournament.parallelism_resolved",
            chosen=chosen,
            cpus=capacity.cpu_count,
            memory_gb=round(capacity.available_mem_gb, 2),
            num_judges=num_judges,
            role_mix=role_mix,
            source="configured",
        )
        return chosen

    mem_cap = max(
        1, int((capacity.available_mem_gb - _MEM_HEADROOM_GB) / _MEM_PER_SUBPROCESS_GB)
    )
    cpu_cap = max(1, capacity.cpu_count - _CPU_RESERVE)
    chosen = min(num_judges, mem_cap, cpu_cap, _PARALLELISM_CEILING)
    chosen = max(1, chosen)

    logger.info(
        "tournament.parallelism_resolved",
        chosen=chosen,
        cpus=capacity.cpu_count,
        memory_gb=round(capacity.available_mem_gb, 2),
        num_judges=num_judges,
        role_mix=role_mix,
        source="auto",
        mem_cap=mem_cap,
        cpu_cap=cpu_cap,
    )
    return chosen
