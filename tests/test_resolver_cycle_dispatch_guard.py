"""Tier-2B (gate R5): resolver cycle-guard + dispatch-swallow.

Closes WS3-cycle-guard-ephemeral-dict and WS3-resolver-dispatch-swallowed.

The chokepoint ``execute_phase._maybe_resolve_blocker`` has two latent
inertness bugs that these tests pin:

1. **cycle-guard-ephemeral-dict** — the in-memory per-blocker loop-safety
   counter was stored via ``setattr(orch, "_resolver_cycle_counts", {})``
   with the failure swallowed (``except: pass``). When ``setattr`` raises
   (e.g. a ``__slots__`` orch, or any orch whose attribute set is guarded),
   the guard dict became *ephemeral* (a fresh ``{}`` each call) so the cap
   never bound and the resolver could re-loop forever. The fix keys the
   counter off a module-level ``WeakKeyDictionary`` so it survives even when
   ``setattr`` fails.

2. **resolver-dispatch-swallowed** — a dispatch failure inside the
   chokepoint was caught and only ``logger.warning``'d, so resolver
   inertness was completely invisible in the ledger. The fix emits exactly
   one ``blocker_escalated`` op carrying a ``dispatch_failed`` marker before
   returning ``None``.

These are ``resolver_enabled`` so the conftest autouse fixture unsets
``AUTODEV_RESOLVER_DISABLED`` (resolver ON).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.defaults import default_config
from orchestrator import blocker_resolver as br
from orchestrator import execute_phase as ep
from orchestrator import failure_classes as fc
from state import ledger as ledger_mod
from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task

pytestmark = pytest.mark.resolver_enabled


def _iso() -> str:
    return "2026-06-15T00:00:00+00:00"


def _t(tid: str) -> Task:
    return Task(id=tid, phase_id="1", title=f"task {tid}", description=f"do {tid}")


def _mk_plan(tasks: list[Task]) -> Plan:
    return Plan(
        plan_id="p-cycle-dispatch",
        spec_hash="cafe",
        phases=[Phase(id="1", title="p", tasks=tasks)],
        created_at=_iso(),
        updated_at=_iso(),
    )


class _FakeGuard:
    def start_task(self, *a: Any, **k: Any) -> None: ...
    def end_task(self, *a: Any, **k: Any) -> None: ...
    def pre_invocation(self, *a: Any, **k: Any) -> None: ...
    def post_invocation(self, *a: Any, **k: Any) -> None: ...


class _SetattrHostileOrch:
    """An orch whose attribute set raises for the cycle-counter attribute.

    Mirrors the real failure mode: an orch with ``__slots__`` (or any
    attribute guard) for which ``setattr(orch, "_resolver_cycle_counts", {})``
    raises. Every other attribute behaves normally.

    ``cwd`` deliberately points at a SEPARATE empty directory while the
    PlanManager writes to its own dir. This reproduces the exact scenario the
    in-memory guard exists for (per the chokepoint's own comment): the
    *ledger-based* budget (``count_prior_cycles``) and ladder-advancement read
    ``orch.cwd`` and therefore see 0 forever, so the deterministic resolver
    keeps recovering and ONLY the in-memory guard can bound the loop.
    """

    def __init__(self, cwd: Path, pm: PlanManager) -> None:
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "session_id", "test-cycle-dispatch")
        object.__setattr__(self, "plan_manager", pm)
        object.__setattr__(self, "cfg", default_config())
        object.__setattr__(self, "guardrails", _FakeGuard())
        object.__setattr__(self, "adapter", None)
        object.__setattr__(self, "registry", None)
        object.__setattr__(self, "knowledge", None)
        object.__setattr__(self, "loop_detector", None)
        object.__setattr__(self, "plugin_registry", None)
        object.__setattr__(self, "disable_impl_tournament", True)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_resolver_cycle_counts":
            raise AttributeError(
                "this orch cannot accept _resolver_cycle_counts via setattr"
            )
        object.__setattr__(self, name, value)


def _make_orch(tmp_path: Path, pm: PlanManager) -> Any:
    cfg = default_config()
    return type(
        "OrchStub",
        (),
        {
            "cwd": tmp_path,
            "session_id": "test-cycle-dispatch",
            "plan_manager": pm,
            "cfg": cfg,
            "guardrails": _FakeGuard(),
            "adapter": None,
            "registry": None,
            "knowledge": None,
            "loop_detector": None,
            "plugin_registry": None,
            "disable_impl_tournament": True,
        },
    )()


async def _pm(tmp_path: Path) -> PlanManager:
    pm = PlanManager(tmp_path, session_id="s-cd")
    await pm.init_plan(_mk_plan([_t("1.1")]))
    return pm


async def _hostile_orch(tmp_path: Path) -> tuple[Any, Task]:
    """Build a setattr-hostile orch whose ``cwd`` is an empty dir disjoint from
    the PlanManager's, isolating the in-memory cycle-guard as the sole backstop.
    """
    pm_dir = tmp_path / "pm"
    cwd = tmp_path / "cwd"
    pm_dir.mkdir()
    cwd.mkdir()
    pm = PlanManager(pm_dir, session_id="s-cd")
    await pm.init_plan(_mk_plan([_t("1.1")]))
    plan = await pm.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    orch = _SetattrHostileOrch(cwd, pm)
    return orch, task


def _ops(cwd: Path) -> list[str]:
    return [e.op for e in ledger_mod.read_entries(cwd)]


def _entries(cwd: Path) -> list[Any]:
    return list(ledger_mod.read_entries(cwd))


# ---------------------------------------------------------------------------
# 1) cycle-guard survives a setattr failure (WeakKeyDictionary backstop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cycle_guard_bounds_resolver_when_setattr_fails(tmp_path: Path) -> None:
    """RED-on-HEAD: when ``setattr(orch, "_resolver_cycle_counts", ...)`` raises,
    the guard dict is lost (ephemeral fresh ``{}`` each call) so the resolver
    re-loops without bound — more than ``max_cycles`` recoveries.

    GREEN: the module-level WeakKeyDictionary keeps the counter alive across
    calls even though setattr fails, so the cap binds.

    The orch's ``cwd`` is an empty dir disjoint from the PlanManager, so the
    ledger-based budget reads 0 forever and only the in-memory guard can bound
    the loop (this is precisely the scenario the in-memory guard exists for).
    """
    orch, task = await _hostile_orch(tmp_path)

    max_cycles = orch.cfg.resolver.max_cycles_per_blocker
    results = []
    for _ in range(max_cycles + 3):
        results.append(
            await ep._maybe_resolve_blocker(
                orch, task, failure_class=fc.SOFT_BLOCKER, raw_error="no improvement"
            )
        )

    # The guard MUST bound recoveries even though setattr failed.
    recovered = [r for r in results if r is not None]
    assert len(recovered) <= max_cycles, (
        f"guard lost across setattr failure: {len(recovered)} recoveries "
        f"> cap {max_cycles}"
    )
    # After the cap, fall through to legacy block (None).
    assert results[-1] is None
    assert results[-2] is None


@pytest.mark.asyncio
async def test_cycle_guard_uses_weakkeydict_path(tmp_path: Path) -> None:
    """A module-level WeakKeyDictionary holds the counter keyed on the orch
    instance — proving the backstop is the WeakKeyDictionary (not setattr).
    """
    import weakref

    orch, task = await _hostile_orch(tmp_path)

    counts = ep._RESOLVER_CYCLE_COUNTS
    assert isinstance(counts, weakref.WeakKeyDictionary)
    assert orch not in counts

    await ep._maybe_resolve_blocker(
        orch, task, failure_class=fc.SOFT_BLOCKER, raw_error="no improvement"
    )

    # The setattr-hostile orch never got the attribute set...
    assert getattr(orch, "_resolver_cycle_counts", None) is None
    # ...but the WeakKeyDictionary recorded the cycle count for it.
    assert orch in counts
    guard_key = f"{task.id}:{fc.SOFT_BLOCKER}"
    assert counts[orch].get(guard_key, 0) >= 1


# ---------------------------------------------------------------------------
# 2) dispatch failure is no longer swallowed silently
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_failure_emits_visible_op_and_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED-on-HEAD: a dispatch failure inside the chokepoint is swallowed —
    only a ``logger.warning`` fires, NO ledger op — so resolver inertness is
    invisible.

    GREEN: exactly one ``blocker_escalated`` op carrying a ``dispatch_failed``
    marker is emitted before returning ``None``.
    """
    pm = await _pm(tmp_path)
    plan = await pm.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    orch = _make_orch(tmp_path, pm)

    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("dispatch exploded")

    # The chokepoint imports ``blocker_resolver`` lazily; patch the module attr.
    monkeypatch.setattr(br, "resolve_blocker", _boom)

    out = await ep._maybe_resolve_blocker(
        orch, task, failure_class=fc.SOFT_BLOCKER, raw_error="x"
    )
    assert out is None  # graceful fall-through (never raises)

    entries = _entries(tmp_path)
    escalated = [e for e in entries if e.op == "blocker_escalated"]
    assert len(escalated) == 1, (
        "dispatch failure must emit exactly one blocker_escalated op "
        f"(got {len(escalated)})"
    )
    payload = escalated[0].payload
    assert payload.get("source") == "dispatch_failed"
    assert payload.get("task_id") == task.id


@pytest.mark.asyncio
async def test_dispatch_success_emits_no_dispatch_failed_op(tmp_path: Path) -> None:
    """Broken-control guard: when dispatch SUCCEEDS, NO ``dispatch_failed``
    marker is emitted (the visible-inertness op is specific to the failure
    path, not always-on noise).
    """
    pm = await _pm(tmp_path)
    plan = await pm.load()
    assert plan is not None
    task = plan.phases[0].tasks[0]
    orch = _make_orch(tmp_path, pm)

    out = await ep._maybe_resolve_blocker(
        orch, task, failure_class=fc.SOFT_BLOCKER, raw_error="no improvement"
    )
    # Deterministic resolver path recovers SOFT_BLOCKER -> non-None.
    assert out is not None

    entries = _entries(tmp_path)
    dispatch_failed = [
        e
        for e in entries
        if e.op == "blocker_escalated"
        and e.payload.get("source") == "dispatch_failed"
    ]
    assert dispatch_failed == [], (
        "no dispatch_failed marker on the success path "
        f"(got {len(dispatch_failed)})"
    )
