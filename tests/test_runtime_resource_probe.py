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


# ---------------------------------------------------------------------------
# resolve_parallelism: explicit-int passes through unchanged
# ---------------------------------------------------------------------------


def test_resolve_parallelism_explicit_int_passes_through() -> None:
    """An explicitly configured int bypasses the resource probe entirely.

    Backward-compat: pre-v0.10.0 configs with ``max_parallel_subprocesses=3``
    keep that exact value, no clamping based on host capacity.
    """
    from runtime.resource_probe import HostCapacity, resolve_parallelism

    cap = HostCapacity(cpu_count=2, available_mem_gb=2.0)
    out = resolve_parallelism(
        configured=8, capacity=cap, role_mix="plan", num_judges=5
    )
    assert out == 8


def test_resolve_parallelism_explicit_int_floors_at_1() -> None:
    """A configured value <= 0 is clamped up to 1 (avoid divide-by-zero
    on the semaphore + always-on parallelism contract)."""
    from runtime.resource_probe import HostCapacity, resolve_parallelism

    cap = HostCapacity(cpu_count=8, available_mem_gb=16.0)
    assert resolve_parallelism(0, cap, "plan", 5) == 1
    assert resolve_parallelism(-3, cap, "plan", 5) == 1


# ---------------------------------------------------------------------------
# resolve_parallelism: None auto-resolves via capacity probe
# ---------------------------------------------------------------------------


def test_resolve_parallelism_none_clamps_to_min_constraint() -> None:
    """Low-mem host: memory-headroom constraint dominates and resolves to a
    small int (here, 1).

    Memory model: ``(available - 4.0 GB headroom) / 1.5 GB-per-subprocess``.
    With 5GB free → ((5 - 4) / 1.5) = 0.66 → max(1, int(0.66)) = 1.
    """
    from runtime.resource_probe import HostCapacity, resolve_parallelism

    cap = HostCapacity(cpu_count=16, available_mem_gb=5.0)
    out = resolve_parallelism(None, cap, "plan", num_judges=7)
    assert out == 1


def test_resolve_parallelism_clamps_to_judge_cohort() -> None:
    """High-mem, high-CPU host with 5 judges → caps at 5, not at the 16
    absolute ceiling. No point spawning more workers than judges."""
    from runtime.resource_probe import HostCapacity, resolve_parallelism

    cap = HostCapacity(cpu_count=32, available_mem_gb=64.0)
    out = resolve_parallelism(None, cap, "plan", num_judges=5)
    assert out == 5


def test_resolve_parallelism_absolute_ceiling_16() -> None:
    """Massive-spec host with 64 judges still caps at the absolute 16
    ceiling (defends against pathological cohort sizes / runaway forks)."""
    from runtime.resource_probe import HostCapacity, resolve_parallelism

    cap = HostCapacity(cpu_count=128, available_mem_gb=512.0)
    out = resolve_parallelism(None, cap, "plan", num_judges=64)
    assert out == 16


def test_resolve_parallelism_cpu_constraint_dominates() -> None:
    """Sufficient memory but only 4 cores → clamps to ``cpu_count - 2 = 2``
    (leave 2 cores for the OS / parent process)."""
    from runtime.resource_probe import HostCapacity, resolve_parallelism

    cap = HostCapacity(cpu_count=4, available_mem_gb=64.0)
    out = resolve_parallelism(None, cap, "plan", num_judges=7)
    assert out == 2


def test_resolve_parallelism_logs_resolution(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The resolver emits ``tournament.parallelism_resolved`` with the
    structured fields {chosen, cpus, memory_gb, num_judges, role_mix}.

    Structlog uses ``PrintLoggerFactory`` so events land on stdout, not
    via stdlib logging — capture via ``capsys`` rather than ``caplog``.

    The emitted format depends on whether stdout-JSON or console-rendering
    is configured (autologging.configure swaps the renderer). Both
    representations contain the field name + value as substrings, so we
    accept either ``role_mix=impl`` (console) or ``"role_mix": "impl"``
    (JSON).
    """
    from runtime.resource_probe import HostCapacity, resolve_parallelism

    cap = HostCapacity(cpu_count=8, available_mem_gb=16.0)
    resolved = resolve_parallelism(
        configured=None, capacity=cap, role_mix="impl", num_judges=3
    )
    assert resolved >= 1
    out = capsys.readouterr().out
    assert "tournament.parallelism_resolved" in out
    # Accept either console-renderer or JSON-renderer formatting.
    assert ("role_mix=impl" in out) or ('"role_mix": "impl"' in out)
    assert ("num_judges=3" in out) or ('"num_judges": 3' in out)
    assert ("cpus=8" in out) or ('"cpus": 8' in out)


def test_resolve_parallelism_role_mix_phase_review_accepted() -> None:
    """All three role_mix literals (``plan``, ``impl``, ``phase_review``)
    are accepted by the resolver."""
    from runtime.resource_probe import HostCapacity, resolve_parallelism

    cap = HostCapacity(cpu_count=8, available_mem_gb=16.0)
    for role in ("plan", "impl", "phase_review"):
        out = resolve_parallelism(None, cap, role, 3)  # type: ignore[arg-type]
        assert out >= 1


# ---------------------------------------------------------------------------
# measure_subprocess_rss: mean RSS across PIDs, robust to dead children
# ---------------------------------------------------------------------------


def test_measure_subprocess_rss_returns_mean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given 3 reachable PIDs returning 100/200/300 MB → mean is 200 MB."""
    from runtime import resource_probe

    def fake_process(pid: int) -> Mock:
        rss_by_pid = {1: 100, 2: 200, 3: 300}
        proc = Mock()
        proc.memory_info.return_value = Mock(rss=rss_by_pid[pid] * 1024 * 1024)
        return proc

    monkeypatch.setattr(resource_probe.psutil, "Process", fake_process)
    out = resource_probe.measure_subprocess_rss([1, 2, 3])
    assert out == pytest.approx(200.0, rel=1e-6)


def test_measure_subprocess_rss_skips_dead_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``psutil.NoSuchProcess`` is silently skipped; mean is taken over
    living children only."""
    from runtime import resource_probe

    def fake_process(pid: int) -> Mock:
        if pid == 2:
            raise resource_probe.psutil.NoSuchProcess(pid)
        proc = Mock()
        proc.memory_info.return_value = Mock(rss=400 * 1024 * 1024)
        return proc

    monkeypatch.setattr(resource_probe.psutil, "Process", fake_process)
    out = resource_probe.measure_subprocess_rss([1, 2, 3])
    # Mean of 400, 400 (PID 2 skipped) = 400.
    assert out == pytest.approx(400.0, rel=1e-6)


def test_measure_subprocess_rss_skips_access_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``psutil.AccessDenied`` is silently skipped (e.g. in sandboxed CI)."""
    from runtime import resource_probe

    def fake_process(pid: int) -> Mock:
        raise resource_probe.psutil.AccessDenied(pid)

    monkeypatch.setattr(resource_probe.psutil, "Process", fake_process)
    out = resource_probe.measure_subprocess_rss([1, 2])
    # Both denied → no living children → return None.
    assert out is None


def test_measure_subprocess_rss_empty_pids_returns_none() -> None:
    """No PIDs supplied → no probe possible → return None."""
    from runtime.resource_probe import measure_subprocess_rss

    assert measure_subprocess_rss([]) is None


def test_measure_subprocess_rss_all_dead_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All PIDs unreachable → return None (no per-pass ratchet decision)."""
    from runtime import resource_probe

    def fake_process(pid: int) -> Mock:
        raise resource_probe.psutil.NoSuchProcess(pid)

    monkeypatch.setattr(resource_probe.psutil, "Process", fake_process)
    assert resource_probe.measure_subprocess_rss([1, 2, 3]) is None
