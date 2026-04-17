"""Tests for :func:`src.plugins.registry.discover_plugins`."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch


from plugins.registry import (
    GateResult,
    PluginRegistry,
    QAContext,
    discover_plugins,
)


class _FakeQAGate:
    name = "fake_qa_gate"

    async def run(self, ctx: QAContext) -> GateResult:
        return GateResult(passed=True)


class _FakeJudge:
    name = "fake_judge"

    async def rank(self, task: str, versions: list[Any]) -> list[str]:
        return versions


class _FakeAgentExt:
    name = "fake_agent"

    def get_spec(self) -> Any:
        return None

    def render_platform(self, platform: str) -> str:
        return ""


class _BadPlugin:
    """Does not implement any protocol."""
    name = "bad"


def _make_ep(name: str, target: Any) -> MagicMock:
    ep = MagicMock()
    ep.name = name
    ep.load.return_value = target
    return ep


def test_discover_empty_group() -> None:
    with patch("plugins.registry.entry_points", return_value=[]):
        reg = discover_plugins()
    assert reg.is_empty()
    assert reg.total == 0


def test_discover_qa_gate() -> None:
    ep = _make_ep("my_gate", _FakeQAGate)
    with patch("plugins.registry.entry_points", return_value=[ep]):
        reg = discover_plugins()
    assert "fake_qa_gate" in reg.qa_gates
    assert reg.total == 1


def test_discover_judge() -> None:
    ep = _make_ep("my_judge", _FakeJudge)
    with patch("plugins.registry.entry_points", return_value=[ep]):
        reg = discover_plugins()
    assert "fake_judge" in reg.judges
    assert reg.total == 1


def test_discover_agent_extension() -> None:
    ep = _make_ep("my_agent", _FakeAgentExt)
    with patch("plugins.registry.entry_points", return_value=[ep]):
        reg = discover_plugins()
    assert "fake_agent" in reg.agents
    assert reg.total == 1


def test_discover_protocol_mismatch_skipped() -> None:
    ep = _make_ep("bad_plugin", _BadPlugin)
    with patch("plugins.registry.entry_points", return_value=[ep]):
        reg = discover_plugins()
    assert reg.is_empty()


def test_discover_load_error_skipped() -> None:
    ep = MagicMock()
    ep.name = "broken"
    ep.load.side_effect = ImportError("missing dep")
    with patch("plugins.registry.entry_points", return_value=[ep]):
        reg = discover_plugins()
    assert reg.is_empty()


def test_discover_instantiate_error_skipped() -> None:
    class _Crasher:
        name = "crasher"

        def __init__(self) -> None:
            raise RuntimeError("boom")

        async def run(self, ctx: QAContext) -> GateResult:
            return GateResult(passed=True)

    ep = _make_ep("crasher", _Crasher)
    with patch("plugins.registry.entry_points", return_value=[ep]):
        reg = discover_plugins()
    assert reg.is_empty()


def test_discover_pre_instantiated_plugin() -> None:
    """entry_points may return an already-instantiated object."""
    instance = _FakeQAGate()
    ep = _make_ep("pre_inst", instance)  # not a class
    with patch("plugins.registry.entry_points", return_value=[ep]):
        reg = discover_plugins()
    assert "fake_qa_gate" in reg.qa_gates


def test_plugin_registry_total() -> None:
    reg = PluginRegistry()
    assert reg.total == 0
    reg.qa_gates["g1"] = _FakeQAGate()  # type: ignore[assignment]
    reg.judges["j1"] = _FakeJudge()  # type: ignore[assignment]
    assert reg.total == 2
    assert not reg.is_empty()


def test_discover_multiple_plugins() -> None:
    eps = [
        _make_ep("gate", _FakeQAGate),
        _make_ep("judge", _FakeJudge),
        _make_ep("agent", _FakeAgentExt),
    ]
    with patch("plugins.registry.entry_points", return_value=eps):
        reg = discover_plugins()
    assert reg.total == 3
