"""Tests for the abstract `PlatformAdapter` base class."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from adapters.base import PlatformAdapter
from adapters.types import AgentInvocation, AgentResult, AgentSpec


class _DummyAdapter(PlatformAdapter):
    """Record call order and enforce measurable concurrency."""

    name = "dummy"

    def __init__(self, delay_s: float = 0.05) -> None:
        self.delay_s = delay_s
        self.in_flight = 0
        self.max_in_flight = 0
        self.call_order: list[str] = []
        self._lock = asyncio.Lock()

    async def init_workspace(self, cwd: Path, agents: list[AgentSpec]) -> None:
        return None

    async def execute(self, inv: AgentInvocation) -> AgentResult:
        async with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.call_order.append(inv.role)
        try:
            await asyncio.sleep(self.delay_s)
            return AgentResult(
                success=True,
                text=f"ran:{inv.role}",
                duration_s=self.delay_s,
            )
        finally:
            async with self._lock:
                self.in_flight -= 1

    async def healthcheck(self) -> tuple[bool, str]:
        return True, "dummy"


class _RaisingAdapter(PlatformAdapter):
    name = "raise"

    async def init_workspace(self, cwd: Path, agents: list[AgentSpec]) -> None:
        return None

    async def execute(self, inv: AgentInvocation) -> AgentResult:
        if inv.role == "fail":
            raise RuntimeError("boom")
        return AgentResult(success=True, text="ok", duration_s=0.0)

    async def healthcheck(self) -> tuple[bool, str]:
        return True, "raise"


def _invs(n: int) -> list[AgentInvocation]:
    return [
        AgentInvocation(role=f"r{i}", prompt=f"p{i}", cwd=Path("/tmp"))
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_parallel_enforces_max_concurrent() -> None:
    adapter = _DummyAdapter(delay_s=0.05)
    results = await adapter.parallel(_invs(10), max_concurrent=3)
    assert len(results) == 10
    assert all(r.success for r in results)
    assert adapter.max_in_flight <= 3
    # We expect concurrency to actually hit 3 with delay * 10 / 3 spread.
    assert adapter.max_in_flight >= 2  # at least some concurrency observed


@pytest.mark.asyncio
async def test_parallel_preserves_order() -> None:
    adapter = _DummyAdapter(delay_s=0.01)
    invs = _invs(6)
    results = await adapter.parallel(invs, max_concurrent=4)
    for i, r in enumerate(results):
        assert r.text == f"ran:r{i}"


@pytest.mark.asyncio
async def test_parallel_propagates_exceptions() -> None:
    adapter = _RaisingAdapter()
    invs = [
        AgentInvocation(role="ok", prompt="p", cwd=Path("/tmp")),
        AgentInvocation(role="fail", prompt="p", cwd=Path("/tmp")),
        AgentInvocation(role="ok2", prompt="p", cwd=Path("/tmp")),
    ]
    with pytest.raises(RuntimeError, match="boom"):
        await adapter.parallel(invs, max_concurrent=3)


@pytest.mark.asyncio
async def test_parallel_rejects_zero_concurrency() -> None:
    adapter = _DummyAdapter()
    with pytest.raises(ValueError):
        await adapter.parallel(_invs(1), max_concurrent=0)


@pytest.mark.asyncio
async def test_parallel_serial_mode() -> None:
    adapter = _DummyAdapter(delay_s=0.01)
    await adapter.parallel(_invs(5), max_concurrent=1)
    assert adapter.max_in_flight == 1


@pytest.mark.asyncio
async def test_parallel_empty_list() -> None:
    adapter = _DummyAdapter()
    results = await adapter.parallel([], max_concurrent=3)
    assert results == []


def test_abstract_cannot_instantiate() -> None:
    with pytest.raises(TypeError):
        PlatformAdapter()  # type: ignore[abstract]
