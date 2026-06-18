"""ADR-0047 (B8): the resolver *wiring* in execute_phase.

These exercise the single chokepoint ``execute_phase._maybe_resolve_blocker``
with the resolver ENABLED (the module is marked ``resolver_enabled`` so the
autouse fixture in conftest unsets ``AUTODEV_RESOLVER_DISABLED``). The resolver's
own decision logic is covered by ``test_blocker_resolver.py``; here we prove the
glue: the gate, the in-memory loop-safety guard, the fail-safe-on-error
behaviour, ledger recording, active recovery at task-local sites, the novel-
failure LLM path, and the phase-degrade observability conversion.
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

from stub_adapter import StubAdapter, ok

pytestmark = pytest.mark.resolver_enabled


def _iso() -> str:
    return "2026-06-15T00:00:00+00:00"


def _t(tid: str) -> Task:
    return Task(
        id=tid,
        phase_id="1",
        title=f"task {tid}",
        description=f"do {tid}",
    )


def _mk_plan(tasks: list[Task]) -> Plan:
    return Plan(
        plan_id="p-resolver-wiring",
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


def _make_orch(tmp_path: Path, pm: PlanManager, adapter: Any = None) -> Any:
    cfg = default_config()
    return type(
        "OrchStub",
        (),
        {
            "cwd": tmp_path,
            "session_id": "test-resolver-wiring",
            "plan_manager": pm,
            "cfg": cfg,
            "guardrails": _FakeGuard(),
            "adapter": adapter,
            "registry": None,
            "knowledge": None,
            "loop_detector": None,
            "plugin_registry": None,
            "disable_impl_tournament": True,
        },
    )()


async def _orch_with_task(tmp_path: Path, adapter: Any = None) -> tuple[Any, Task]:
    pm = PlanManager(tmp_path, session_id="s-wire")
    await pm.init_plan(_mk_plan([_t("1.1")]))
    orch = _make_orch(tmp_path, pm, adapter=adapter)
    plan = await pm.load()
    assert plan is not None
    return orch, plan.phases[0].tasks[0]


def _ops(cwd: Path) -> list[str]:
    return [e.op for e in ledger_mod.read_entries(cwd)]


# ---------------------------------------------------------------------------
# Gate: disabled / kill-switch -> exact legacy behaviour (None, no ledger ops)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chokepoint_disabled_returns_none(tmp_path: Path) -> None:
    orch, task = await _orch_with_task(tmp_path)
    orch.cfg.resolver.enabled = False
    out = await ep._maybe_resolve_blocker(
        orch, task, failure_class=fc.SOFT_BLOCKER, raw_error="x"
    )
    assert out is None
    assert "blocker_escalated" not in _ops(tmp_path)


@pytest.mark.asyncio
async def test_chokepoint_env_kill_switch_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch, task = await _orch_with_task(tmp_path)
    monkeypatch.setenv("AUTODEV_RESOLVER_DISABLED", "1")
    out = await ep._maybe_resolve_blocker(
        orch, task, failure_class=fc.SOFT_BLOCKER, raw_error="x"
    )
    assert out is None
    assert "blocker_escalated" not in _ops(tmp_path)


# ---------------------------------------------------------------------------
# Active recovery + ledger recording at a task-local site
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chokepoint_recovers_task_and_records_ledger(tmp_path: Path) -> None:
    orch, task = await _orch_with_task(tmp_path)
    out = await ep._maybe_resolve_blocker(
        orch, task, failure_class=fc.SOFT_BLOCKER, raw_error="no improvement"
    )
    # Recovered -> a non-None task, re-enabled (in_progress, not blocked).
    assert out is not None
    assert out.status == "in_progress"
    ops = _ops(tmp_path)
    assert "blocker_escalated" in ops
    assert "resolution_chosen" in ops
    assert "resolution_outcome" in ops


@pytest.mark.asyncio
async def test_guardrail_site_recovers(tmp_path: Path) -> None:
    orch, task = await _orch_with_task(tmp_path)
    out = await ep._maybe_resolve_blocker(
        orch, task, failure_class=fc.GUARDRAIL_EXCEEDED, raw_error="duration cap"
    )
    assert out is not None and out.status == "in_progress"


# ---------------------------------------------------------------------------
# Loop-safety: the in-memory guard caps recoveries (ledger-independent)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_guard_caps_recoveries(tmp_path: Path) -> None:
    orch, task = await _orch_with_task(tmp_path)
    max_cycles = orch.cfg.resolver.max_cycles_per_blocker
    results = []
    for _ in range(max_cycles + 2):
        results.append(
            await ep._maybe_resolve_blocker(
                orch, task, failure_class=fc.WORKER_EXCEPTION, raw_error="boom"
            )
        )
    # First ``max_cycles`` calls may recover; everything after the cap is None
    # (fall through to the legacy block) — no unbounded recovery loop.
    assert results[-1] is None
    assert results[-2] is None
    recovered = [r for r in results if r is not None]
    assert len(recovered) <= max_cycles


# ---------------------------------------------------------------------------
# Fail-safe: a resolver error never breaks the loop (returns None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failsafe_on_resolver_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch, task = await _orch_with_task(tmp_path)

    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("resolver exploded")

    # The chokepoint does ``from orchestrator import blocker_resolver as _br``
    # lazily, so patching the module attribute reaches it.
    monkeypatch.setattr(br, "resolve_blocker", _boom)
    out = await ep._maybe_resolve_blocker(
        orch, task, failure_class=fc.SOFT_BLOCKER, raw_error="x"
    )
    assert out is None  # never raised; fell through to legacy block


# ---------------------------------------------------------------------------
# Novel failure -> LLM resolver path through the chokepoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_novel_failure_routes_through_llm(tmp_path: Path) -> None:
    adapter = StubAdapter(
        {
            "resolver": ok(
                '{"action": "retry_with_changes", "params": {}, '
                '"rationale": "novel error, retry once"}'
            )
        }
    )
    orch, task = await _orch_with_task(tmp_path, adapter=adapter)
    out = await ep._maybe_resolve_blocker(
        orch, task, failure_class="totally_novel_unseen_error", raw_error="???"
    )
    assert out is not None and out.status == "in_progress"
    assert adapter.count("resolver") == 1  # the LLM resolver was consulted
    chosen = [
        e.payload
        for e in ledger_mod.read_entries(tmp_path)
        if e.op == "resolution_chosen"
    ]
    assert chosen and chosen[-1]["action"] == "retry_with_changes"


# ---------------------------------------------------------------------------
# Phase-degrade -> explicit resolver decision (observability conversion)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_phase_degrade_writes_ledger(tmp_path: Path) -> None:
    pm = PlanManager(tmp_path, session_id="s-degrade")
    await pm.init_plan(_mk_plan([_t("1.1")]))
    orch = _make_orch(tmp_path, pm)
    await br.record_phase_degrade(orch, "intake", RuntimeError("'intake_enricher'"))
    ops = _ops(tmp_path)
    assert "blocker_escalated" in ops
    assert "resolution_chosen" in ops
    outcomes = [
        e.payload
        for e in ledger_mod.read_entries(tmp_path)
        if e.op == "resolution_outcome"
    ]
    assert outcomes and outcomes[-1]["outcome"] == "phase_degraded"


@pytest.mark.asyncio
async def test_record_phase_degrade_noop_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pm = PlanManager(tmp_path, session_id="s-degrade2")
    await pm.init_plan(_mk_plan([_t("1.1")]))
    orch = _make_orch(tmp_path, pm)
    monkeypatch.setenv("AUTODEV_RESOLVER_DISABLED", "1")
    await br.record_phase_degrade(orch, "intake", RuntimeError("x"))
    assert "blocker_escalated" not in _ops(tmp_path)


# ---------------------------------------------------------------------------
# B8 structural: every failure class the wiring passes is a known class
# ---------------------------------------------------------------------------


def test_wired_failure_classes_are_known() -> None:
    """Every failure class the orchestrator *wires* into the resolver must be a
    member of the taxonomy — proven by an **AST walk**, not a literal-string grep.

    The previous version did ``re.findall(r'failure_class="([a-z_]+)"', src)``,
    which only caught the two ``failure_class="…"`` literal call sites and *missed*
    every attribute reference (``_fcls.SOFT_BLOCKER``, ``fc.GUARDRAIL_EXCEEDED``,
    the function-local ``_fc.classify`` …) — ~21 of the ~23 real sites. A typo or
    a planted ``_fcls.bogus_planted_class`` anywhere in the wiring would have
    sailed past the old gate.

    This version is engagement-by-construction:

    1. **Alias discovery (AST).** For every src module that imports
       ``orchestrator.failure_classes`` (module-level *or* function-local), we
       parse it and record every name the module bound the package to (``_fcls``,
       ``fc``, the lazy ``_fc`` at execute_phase.py:1854, …).
    2. **Attribute sites (AST).** We ``ast.walk`` each such module for
       ``ast.Attribute`` nodes whose ``.value`` is an ``ast.Name`` matching a
       discovered alias, and collect the attribute name (``SOFT_BLOCKER`` …).
    3. **Literal sites (AST).** We also collect every ``failure_class="…"``
       string-keyword argument (the two literal call sites), so both styles are
       covered.
    4. **Assert.** Each attribute name (other than the two non-class helpers
       ``classify``/``is_known``) must be a real taxonomy constant:
       ``hasattr(fc, name) and getattr(fc, name) in fc.ALL_FAILURE_CLASSES``.
       Each literal string must be in ``fc.ALL_FAILURE_CLASSES``. (``unknown`` is
       allowed — it is itself a member.)

    Because the check is hasattr/getattr-on-the-real-module, planting
    ``_fcls.bogus_planted_class`` (or fat-fingering ``_fcls.SOFT_BLOKER``)
    *anywhere* in the wiring fails CI: the name is not an attribute of
    ``failure_classes``, so the assertion fires. ANTI-VACUITY: we require
    ``>= 15`` discovered sites, so a regression that walks nothing (e.g. an alias
    rename that silently stops matching) fails instead of trivially passing.
    """
    import ast

    # Non-class members of the failure_classes public API that may be reached via
    # an alias attribute access (functions, not taxonomy constants). These are the
    # ONLY attribute names exempt from the "must be a constant" rule.
    api_members = {"classify", "classify_max_turns_failure", "is_known"}

    src_root = Path(fc.__file__).resolve().parent.parent  # .../src

    attr_sites: list[tuple[str, str]] = []  # (module_path, attr_name)
    literal_sites: list[tuple[str, str]] = []  # (module_path, string_value)

    for py in sorted(src_root.rglob("*.py")):
        source = py.read_text(encoding="utf-8")
        # Cheap pre-filter; AST is the source of truth below.
        if "failure_classes" not in source:
            continue
        tree = ast.parse(source, filename=str(py))

        # (1) Discover every alias this module binds failure_classes to. Covers
        # both ``from orchestrator import failure_classes as X`` and
        # ``import orchestrator.failure_classes as X`` / ``import …failure_classes``
        # at module scope or inside a function body (ast.walk is exhaustive).
        aliases: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # from orchestrator import failure_classes [as X]
                mod = node.module or ""
                for alias in node.names:
                    if alias.name == "failure_classes" and (
                        mod == "orchestrator" or mod.endswith(".orchestrator")
                    ):
                        aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                # import orchestrator.failure_classes [as X]
                for alias in node.names:
                    if alias.name.endswith("orchestrator.failure_classes"):
                        # ``import a.b.failure_classes`` binds ``a`` w/o asname;
                        # only the ``as X`` form yields a usable attribute alias.
                        if alias.asname:
                            aliases.add(alias.asname)

        if not aliases:
            continue

        # (2) Attribute references on a discovered alias: alias.ATTR
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in aliases
            ):
                attr_sites.append((str(py), node.attr))

        # (3) Literal ``failure_class="…"`` keyword arguments (AST, not regex).
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if (
                        kw.arg == "failure_class"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)
                    ):
                        literal_sites.append((str(py), kw.value.value))

    total = len(attr_sites) + len(literal_sites)
    # ANTI-VACUITY: today there are ~21 alias-attr refs + 2 literals. A walk that
    # finds nothing (alias rename, import-shape change) MUST fail, not pass.
    assert total >= 15, (
        f"only discovered {total} wired failure-class sites "
        f"({len(attr_sites)} attr + {len(literal_sites)} literal) — expected >=15; "
        "the AST walk likely stopped matching (alias/import-shape regression)"
    )

    # (4a) Every alias-attribute name must be a real taxonomy constant (the two
    # helper functions excepted).
    for module_path, name in attr_sites:
        if name in api_members:
            continue
        assert hasattr(fc, name), (
            f"{module_path}: failure_classes has no attribute {name!r} "
            "(typo or planted bogus constant in the resolver wiring?)"
        )
        value = getattr(fc, name)
        assert value in fc.ALL_FAILURE_CLASSES, (
            f"{module_path}: failure_classes.{name} == {value!r} "
            "is not in ALL_FAILURE_CLASSES"
        )

    # (4b) Every literal failure_class="…" must be a known class.
    for module_path, value in literal_sites:
        assert value in fc.ALL_FAILURE_CLASSES, (
            f"{module_path}: literal failure_class={value!r} not in taxonomy"
        )
