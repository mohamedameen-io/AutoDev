"""Plugin discovery and protocol definitions.

Public API (re-exported from :mod:`plugins.registry`):

- :class:`PluginRegistry` — buckets discovered plugins by kind.
- :class:`QAGatePlugin`, :class:`JudgeProviderPlugin`, :class:`AgentExtensionPlugin`
  — runtime-checkable ``Protocol`` classes third-party plugins must satisfy.
- :class:`QAContext`, :class:`GateResult` — minimal dataclass carriers used by
  QA-gate plugins. Phase 8 may extend these; the current shapes are intentionally
  narrow so plugins written against them remain forward-compatible.
- :func:`discover_plugins` — walk ``importlib.metadata.entry_points`` and
  instantiate each plugin into the appropriate :class:`PluginRegistry` bucket.

Third-party packages publish plugins by declaring an entry point, e.g.::

    [project.entry-points."autodev.plugins"]
    my_qa_gate = "mypkg.plugins:MyQAGate"
"""

from __future__ import annotations

from plugins.registry import (
    AgentExtensionPlugin,
    GateResult,
    JudgeProviderPlugin,
    PluginRegistry,
    QAContext,
    QAGatePlugin,
    discover_plugins,
)


__all__ = [
    "AgentExtensionPlugin",
    "GateResult",
    "JudgeProviderPlugin",
    "PluginRegistry",
    "QAContext",
    "QAGatePlugin",
    "discover_plugins",
]
