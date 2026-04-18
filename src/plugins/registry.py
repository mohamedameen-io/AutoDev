"""Plugin discovery via :mod:`importlib.metadata` ``entry_points``.

Third-party packages publish plugins by declaring an entry point in their
``pyproject.toml``::

    [project.entry-points."autodev.plugins"]
    my_qa_gate = "mypkg.plugins:MyQAGate"
    my_judge   = "mypkg.plugins:MyJudge"
    my_agent   = "mypkg.plugins:MyAgentExtension"

``autodev`` inspects the ``autodev.plugins`` entry-point group at runtime,
loads each target, instantiates it (if it is a class), and buckets the instance
into the appropriate slot on :class:`PluginRegistry` based on which
``Protocol`` it satisfies.

The discovery pipeline must never crash the host process: a malformed plugin
(``ImportError``, missing dependency, raises in ``__init__``, or simply fails
the runtime Protocol check) is logged at ``WARNING`` level and skipped.

Contract summary
----------------

A plugin class MUST:

1. Be importable via its entry-point target.
2. Be instantiable with zero arguments (``cls()``). Pre-instantiated values
   are also accepted (``cls`` may already be an instance, not a class).
3. Expose a ``name: str`` attribute.
4. Implement exactly one of :class:`QAGatePlugin`, :class:`JudgeProviderPlugin`,
   or :class:`AgentExtensionPlugin` (structural ``Protocol`` check).

Why ``Protocol`` + ``runtime_checkable``? We want third-party plugins to have
no hard dependency on ``autodev`` types at import time — they can simply
implement the right shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from autologging import get_logger


log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Plugin protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class QAGatePlugin(Protocol):
    """A custom QA gate. Runs against a checked-out diff.

    Implementations live outside autodev and are surfaced via entry-points.
    Phase 8 will define the built-in gates; this Protocol is the minimum
    contract third-party gates must satisfy.
    """

    name: str

    async def run(self, ctx: "QAContext") -> "GateResult":
        """Evaluate the gate and return a :class:`GateResult`.

        Must be async so long-running subprocess gates don't block the
        orchestrator event loop.
        """
        ...


@runtime_checkable
class JudgeProviderPlugin(Protocol):
    """A custom tournament judge that returns a ranking of version indices.

    The return value is a permutation of ``[0, 1, ..., len(versions)-1]``
    ordered best-to-worst. Index 0 refers to the first element of ``versions``,
    index 1 to the second, etc. For the standard three-way tournament call
    ``rank(task, [v_a, v_b, v_ab])``, a valid return is e.g. ``[2, 0, 1]``
    meaning ``v_ab`` is best, ``v_a`` is second, and ``v_b`` is worst.
    """

    name: str

    async def rank(self, task: str, versions: list[Any]) -> list[int]:
        ...


@runtime_checkable
class AgentExtensionPlugin(Protocol):
    """A plugin that contributes a new agent definition or overrides one.

    ``get_spec`` returns an ``AgentSpec``-compatible object (duck-typed — the
    import is lazy to avoid a hard cycle with :mod:`adapters.types`).
    ``render_platform`` lets the plugin emit per-platform artifacts (e.g. an
    entry in ``.claude/agents/my_agent.md``).
    """

    name: str

    def get_spec(self) -> Any:
        ...

    def render_platform(self, platform: str) -> str:
        ...


# ---------------------------------------------------------------------------
# Carrier dataclasses
# ---------------------------------------------------------------------------


@dataclass
class QAContext:
    """Inputs handed to :meth:`QAGatePlugin.run`.

    Kept deliberately narrow: the current working directory (a repo worktree),
    a task id for correlation, and the optional raw diff text so gates can
    short-circuit without re-reading from git.
    """

    cwd: Path
    task_id: str
    diff: str | None = None


@dataclass
class GateResult:
    """Verdict emitted by a :class:`QAGatePlugin`."""

    passed: bool
    details: str = ""


# ---------------------------------------------------------------------------
# Registry + discovery
# ---------------------------------------------------------------------------


@dataclass
class PluginRegistry:
    """Buckets discovered plugin instances by kind.

    Dict keys are the plugin's ``name`` attribute so each kind is a
    name-indexed map. Later registrations overwrite earlier ones for the same
    name (entry-point iteration order is typically alphabetical by package
    name, so this is deterministic-enough for our purposes).
    """

    qa_gates: dict[str, QAGatePlugin] = field(default_factory=dict)
    judges: dict[str, JudgeProviderPlugin] = field(default_factory=dict)
    agents: dict[str, AgentExtensionPlugin] = field(default_factory=dict)

    @property
    def total(self) -> int:
        """Total count of discovered plugins across all buckets."""
        return len(self.qa_gates) + len(self.judges) + len(self.agents)

    def is_empty(self) -> bool:
        return self.total == 0


def discover_plugins(group: str = "autodev.plugins") -> PluginRegistry:
    """Load all plugins declared under the ``autodev.plugins`` entry-point group.

    Plugins that fail to load or don't implement any supported protocol are
    skipped with a warning log — discovery must never crash the host process.

    Parameters
    ----------
    group:
        Entry-point group name. Override only in tests or to support
        alternative group names for parallel installs.
    """
    reg = PluginRegistry()
    try:
        eps = entry_points(group=group)
    except Exception as exc:  # pragma: no cover — defensive: stdlib should not raise
        log.warning("plugins.discover.failed", error=str(exc))
        return reg

    for ep in eps:
        try:
            target = ep.load()
        except Exception as exc:
            log.warning(
                "plugins.load_failed", name=ep.name, error=str(exc)
            )
            continue

        try:
            instance = target() if isinstance(target, type) else target
        except Exception as exc:
            log.warning(
                "plugins.instantiate_failed", name=ep.name, error=str(exc)
            )
            continue

        # Validate via Protocol isinstance. ``runtime_checkable`` Protocols
        # only validate attribute presence — they do not inspect method
        # signatures. Good enough for this loose contract.
        matched = False
        if isinstance(instance, QAGatePlugin):
            reg.qa_gates[_plugin_name(instance, ep.name)] = instance
            matched = True
        elif isinstance(instance, JudgeProviderPlugin):
            reg.judges[_plugin_name(instance, ep.name)] = instance
            matched = True
        elif isinstance(instance, AgentExtensionPlugin):
            reg.agents[_plugin_name(instance, ep.name)] = instance
            matched = True

        if not matched:
            log.warning(
                "plugins.protocol_mismatch",
                name=ep.name,
                type=type(instance).__name__,
            )

    log.info(
        "plugins.discover.done",
        qa_gates=len(reg.qa_gates),
        judges=len(reg.judges),
        agents=len(reg.agents),
    )
    return reg


def _plugin_name(instance: Any, fallback: str) -> str:
    """Return ``instance.name`` if non-empty else the entry-point name."""
    declared = getattr(instance, "name", None)
    if isinstance(declared, str) and declared:
        return declared
    return fallback


__all__ = [
    "AgentExtensionPlugin",
    "GateResult",
    "JudgeProviderPlugin",
    "PluginRegistry",
    "QAContext",
    "QAGatePlugin",
    "discover_plugins",
]
