"""F-7: plan-phase fail-loud wall-clock ceiling (deterministic, NO live claude).

Root cause (read-only investigation): the plan tournament's pass loop
(:meth:`tournament.core.Tournament.run`) has NO cumulative wall-clock
deadline anywhere. A slow OR wedged plan phase runs to
``max_rounds × judges × branches`` or until an EXTERNAL SIGKILL — surfacing
as an opaque "timed out after 2400s" with no autodev-emitted reason.

The fix (analog of F-2's ``corrective_nonconvergent_ceiling``): a
configurable cumulative wall-clock budget checked BETWEEN passes (cheap,
never mid-call). On breach the tournament STOPS LOUD with the best
on-disk incumbent (``final_output.md`` written) by raising
:class:`~errors.TournamentError` — which the existing
``plan_phase`` salvage path already catches.

These tests pin:
  * RED→GREEN: with a small ``wall_budget_s`` and a fake clock that
    advances per pass, the loop stops EARLY (does NOT run all
    ``max_rounds``) and raises ``TournamentError`` carrying the
    ``plan_phase_wall_budget_exceeded`` marker — AND ``final_output.md``
    is on disk (the salvage incumbent).
  * Default ``wall_budget_s=None`` preserves the OLD unbounded behavior
    (no early stop, no raise) — legacy byte-identical.

A FAKE clock (injected via ``TournamentConfig.clock``) is used rather
than real sleeps for determinism + speed.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Callable

import pytest

from errors import TournamentError
from tournament import StubLLMClient, Tournament, TournamentConfig
from tournament.plan_tournament import PlanContentHandler


# The greppable marker the loud failure carries — also the ledger op name.
_WALL_BUDGET_MARKER = "plan_phase_wall_budget_exceeded"


def _judge_prefer(prompt_text: str, prefer_marker: str) -> str:
    """Return ``RANKING: …`` placing the slot containing ``prefer_marker`` first.

    Mirrors :func:`tests.test_tournament_runaway_repro._judge_prefer`.
    """
    offsets: dict[int, int] = {}
    for slot in (1, 2, 3):
        idx = prompt_text.find(f"PROPOSAL {slot}:")
        if idx >= 0:
            offsets[slot] = idx
    ordered = sorted(offsets.items(), key=lambda kv: kv[1])
    slot_end: dict[int, int] = {}
    for i, (slot, start) in enumerate(ordered):
        end = ordered[i + 1][1] if i + 1 < len(ordered) else len(prompt_text)
        slot_end[slot] = end

    preferred: int | None = None
    for slot, start in offsets.items():
        if prefer_marker in prompt_text[start : slot_end[slot]]:
            preferred = slot
            break
    assert preferred is not None
    others = [s for s in (1, 2, 3) if s != preferred]
    return f"RANKING: {preferred}, {others[0]}, {others[1]}"


def _never_converging_cb() -> Callable[[str, str, str], str]:
    """Role callback where the synthesizer emits fresh content every pass.

    AB wins by Borda every pass (hash always differs → no hash short-circuit;
    convergence_k never reached). With the runaway detectors OFF, the loop
    would run to ``max_rounds`` — so an early stop is unambiguously the
    wall-budget ceiling firing, not convergence/runaway.
    """
    state: dict[str, int] = {"synth_n": 0}

    def _cb(role: str, system: str, user: str) -> str:
        if role == "critic_t":
            return "- nit"
        if role == "architect_b":
            return "# Plan: B_VARIANT\n## Phase B\n"
        if role == "synthesizer":
            state["synth_n"] += 1
            return (
                f"# Plan: AB_{state['synth_n']}\n"
                f"## Phase AB_{state['synth_n']}\n"
            )
        return _judge_prefer(user, f"# Plan: AB_{state['synth_n']}")

    return _cb


class _FakeClock:
    """Monotonic-shaped fake clock that advances a fixed step per read.

    The Tournament reads the clock once at run-entry (t0) and once per
    between-pass check. With ``step=10.0`` the elapsed grows 10s per read,
    so a ``wall_budget_s`` of e.g. 25 is breached after ~3 reads.
    """

    def __init__(self, step: float = 10.0, start: float = 1000.0) -> None:
        self._t = start
        self._step = step
        self.reads = 0

    def __call__(self) -> float:
        v = self._t
        self.reads += 1
        self._t += self._step
        return v


# ── RED→GREEN: the ceiling fires, stops loud, salvages the incumbent ────────


@pytest.mark.asyncio
async def test_wall_budget_stops_loud_before_max_rounds(tmp_path: Path) -> None:
    """A small ``wall_budget_s`` halts the loop EARLY with a loud raise.

    Pre-fix: ``wall_budget_s`` is unknown to ``TournamentConfig`` /
    ``Tournament.run`` (the loop has no cumulative deadline) so this test
    cannot even construct the config — it fails RED.

    Post-fix:
      * the loop stops well before ``max_rounds=50`` (the wedged-run shape);
      * it raises ``TournamentError`` carrying the
        ``plan_phase_wall_budget_exceeded`` marker (the greppable,
        attributable reason — NOT an opaque external timeout);
      * ``final_output.md`` is on disk so the salvage path can recover the
        best incumbent.
    """
    clock = _FakeClock(step=10.0)
    cfg = TournamentConfig(
        num_judges=3,
        convergence_k=10,  # never converge via streak within the window
        max_rounds=50,  # large: an early stop must be the budget, not the cap
        # Runaway detectors OFF so the ONLY early-stop cause is the budget.
        score_stability_window=None,
        score_stability_max_delta=None,
        winner_stability_window=None,
        wall_budget_s=25.0,
        clock=clock,
    )

    client = StubLLMClient(fn=_never_converging_cb())
    artifact = tmp_path / "tournaments" / "wall-budget"
    t = Tournament(
        handler=PlanContentHandler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0xF00D),
    )

    with pytest.raises(TournamentError) as exc_info:
        await t.run("Repro task.", "# Plan: foo\n")

    # Loud + attributable: the error names the budget ceiling.
    assert _WALL_BUDGET_MARKER in str(exc_info.value)

    # Salvage incumbent is on disk (best-effort recovery target).
    final_path = artifact / "final_output.md"
    assert final_path.exists(), "final_output.md must be written for salvage"
    assert final_path.read_text().startswith("# Plan:")

    # We stopped EARLY — nowhere near max_rounds. With step=10 and budget=25
    # the breach lands within the first handful of passes.
    history_path = artifact / "history.json"
    assert history_path.exists()
    import json

    history = json.loads(history_path.read_text())
    assert 1 <= len(history) < 50, (
        f"expected an early stop (<50 passes), got {len(history)}"
    )


# ── Default None preserves the OLD unbounded behavior (legacy-identical) ─────


@pytest.mark.asyncio
async def test_wall_budget_none_is_unbounded_legacy(tmp_path: Path) -> None:
    """``wall_budget_s=None`` (default) imposes NO deadline.

    Even with a fast-advancing clock, a converging run completes normally
    (no early stop, no raise). This pins the byte-identical-legacy default:
    the ceiling is strictly opt-in.
    """
    # A converging stub: synthesizer returns a STABLE body so the hash
    # short-circuit advances the streak → converges at convergence_k=2.
    stable_body = "# Plan: STABLE\n## Phase X\n"

    def _cb(role: str, system: str, user: str) -> str:
        if role == "critic_t":
            return "- nit"
        if role == "architect_b":
            return "# Plan: B_BODY\n## Phase B\n"
        if role == "synthesizer":
            return (
                "Looking at both versions, X is stronger.\n\n" + stable_body
            )
        return _judge_prefer(user, "STABLE")

    clock = _FakeClock(step=10_000.0)  # huge step — would breach ANY finite budget
    cfg = TournamentConfig(
        num_judges=3,
        convergence_k=2,
        max_rounds=10,
        wall_budget_s=None,  # default — OFF
        clock=clock,
    )

    client = StubLLMClient(fn=_cb)
    artifact = tmp_path / "tournaments" / "wall-budget-off"
    t = Tournament(
        handler=PlanContentHandler(),
        client=client,
        cfg=cfg,
        artifact_dir=artifact,
        rng=random.Random(0xDEAD),
    )

    # No raise — the run converges normally despite the fast clock.
    final, history = await t.run("Repro task.", "# Plan: foo\n")

    assert final.startswith("# Plan:")
    # Converged via the hash short-circuit at convergence_k=2 (3 passes,
    # matching the runaway-repro convergence shape) — NOT an early budget stop.
    assert len(history) == 3
    assert history[-1].meta["effective_winner"] == "A"


@pytest.mark.asyncio
async def test_wall_budget_unset_field_defaults_to_none(tmp_path: Path) -> None:
    """A ``TournamentConfig`` built WITHOUT the new field behaves as None.

    Guards the default: legacy call sites that never mention
    ``wall_budget_s`` / ``clock`` get the unbounded path for free.
    """
    cfg = TournamentConfig(num_judges=1, convergence_k=2, max_rounds=5)
    assert cfg.wall_budget_s is None


# ── Runner-level wiring: the LOUD, attributable ledger op + re-raise ────────
#
# These pin that ``run_plan_tournament`` (the layer that owns ``orch`` and so
# can emit ledger ops) routes a wall-budget breach to the
# ``plan_phase_wall_budget_exceeded`` op AND re-raises so the existing
# plan-phase salvage path (which catches ``TournamentError``) fires.

from agents import build_registry  # noqa: E402
from config.defaults import default_config  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402
from orchestrator import plan_tournament_runner as ptr  # noqa: E402
from stub_adapter import StubAdapter, ok  # noqa: E402


_SPEC_HASH = "0123456789abcdef"


def _plan_md() -> str:
    return (
        "# Plan: Add foo(x)\n\n"
        "## Phase 1: Implement\n\n"
        "### Task 1.1: Write foo\n"
        "  - Description: Add a function foo.\n"
        "  - Files: foo.py\n"
        "  - Acceptance:\n"
        "    - [ ] function exists\n"
    )


def _make_orch(cwd: Path, *, wall_budget_s: float | None) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.plan.num_judges = 3
    cfg.tournaments.plan.convergence_k = 1
    cfg.tournaments.plan.max_rounds = 3
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.impl.enabled = False
    cfg.guardrails.plan_phase_wall_budget_s = wall_budget_s
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=StubAdapter({"explorer": ok("ok")}),
        registry=registry,
        session_id="sess-f7-wall-budget",
    )


class _BudgetBreachTournament:
    """Stand-in ``Tournament`` whose ``run`` raises the budget-breach error.

    Mirrors the real loop's behavior at the breach: it raises a
    ``TournamentError`` carrying the ``plan_phase_wall_budget_exceeded``
    marker (the real loop also writes ``final_output.md`` first; that's
    covered by the core-level test above).
    """

    captured_cfg = None

    def __init__(self, *, cfg=None, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        type(self).captured_cfg = cfg

    async def run(self, *, task_prompt: str, initial: str):  # type: ignore[no-untyped-def]
        raise TournamentError(
            "plan_phase_wall_budget_exceeded: tournament wall-clock budget of "
            "5.0s exceeded after 6.0s (2 pass(es) completed); stopping LOUD."
        )


class _SurvivorFloorTournament:
    """Stand-in ``Tournament`` raising a NON-budget ``TournamentError``."""

    def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
        pass

    async def run(self, *, task_prompt: str, initial: str):  # type: ignore[no-untyped-def]
        raise TournamentError("only 1 of 3 branches succeeded; survivor floor is 2")


@pytest.mark.asyncio
async def test_runner_threads_budget_into_tournament_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_plan_tournament`` threads ``cfg.guardrails.plan_phase_wall_budget_s``
    into the constructed ``TournamentConfig``."""

    class _Capture:
        captured_cfg = None

        def __init__(self, *, cfg=None, **_kw) -> None:  # type: ignore[no-untyped-def]
            type(self).captured_cfg = cfg

        async def run(self, *, task_prompt: str, initial: str):  # type: ignore[no-untyped-def]
            return initial, []

    monkeypatch.setattr(ptr, "Tournament", _Capture)
    orch = _make_orch(tmp_path, wall_budget_s=123.0)
    await ptr.run_plan_tournament(orch, _plan_md(), "spec text", spec_hash=_SPEC_HASH)
    assert _Capture.captured_cfg is not None
    assert _Capture.captured_cfg.wall_budget_s == 123.0


@pytest.mark.asyncio
async def test_runner_default_none_threads_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default (unset) budget threads ``None`` into the config — OFF."""

    class _Capture:
        captured_cfg = None

        def __init__(self, *, cfg=None, **_kw) -> None:  # type: ignore[no-untyped-def]
            type(self).captured_cfg = cfg

        async def run(self, *, task_prompt: str, initial: str):  # type: ignore[no-untyped-def]
            return initial, []

    monkeypatch.setattr(ptr, "Tournament", _Capture)
    orch = _make_orch(tmp_path, wall_budget_s=None)
    await ptr.run_plan_tournament(orch, _plan_md(), "spec text", spec_hash=_SPEC_HASH)
    assert _Capture.captured_cfg is not None
    assert _Capture.captured_cfg.wall_budget_s is None


@pytest.mark.asyncio
async def test_runner_emits_ledger_op_and_reraises_on_breach(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A budget-breach ``TournamentError`` → LOUD ledger op + re-raise.

    The re-raise is essential: ``plan_phase`` catches ``TournamentError`` to
    drive salvage. The ledger op is the greppable, attributable reason that
    replaces the opaque external timeout.
    """
    monkeypatch.setattr(ptr, "Tournament", _BudgetBreachTournament)
    orch = _make_orch(tmp_path, wall_budget_s=5.0)

    with pytest.raises(TournamentError) as exc_info:
        await ptr.run_plan_tournament(
            orch, _plan_md(), "spec text", spec_hash=_SPEC_HASH
        )
    assert _WALL_BUDGET_MARKER in str(exc_info.value)

    entries = await orch.plan_manager.read_ledger()
    ops = [e.op for e in entries]
    assert _WALL_BUDGET_MARKER in ops, (
        f"expected the loud ledger op to be appended; got ops={ops}"
    )
    # The emitted op's payload carries the attribution figures.
    breach = next(e for e in entries if e.op == _WALL_BUDGET_MARKER)
    assert breach.payload["budget_s"] == 5.0
    assert breach.payload["spec_hash"] == _SPEC_HASH
    assert _WALL_BUDGET_MARKER in breach.payload["reason"]


@pytest.mark.asyncio
async def test_runner_does_not_emit_op_for_non_budget_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A NON-budget ``TournamentError`` (survivor floor) re-raises WITHOUT
    the wall-budget op — we only annotate the specific breach."""
    monkeypatch.setattr(ptr, "Tournament", _SurvivorFloorTournament)
    orch = _make_orch(tmp_path, wall_budget_s=5.0)

    with pytest.raises(TournamentError) as exc_info:
        await ptr.run_plan_tournament(
            orch, _plan_md(), "spec text", spec_hash=_SPEC_HASH
        )
    assert _WALL_BUDGET_MARKER not in str(exc_info.value)

    entries = await orch.plan_manager.read_ledger()
    assert _WALL_BUDGET_MARKER not in [e.op for e in entries]
