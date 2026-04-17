"""Integration tests for plan_phase.py with the tournament wired in.

Walks explorer → domain_expert → architect → **PlanTournament** → init_plan with a
:class:`StubAdapter` that replies per-role. Verifies:

  - the plan tournament is invoked and refines the markdown,
  - the refined plan is used to init the plan,
  - a ``plan_tournament_complete`` ledger entry is appended.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.base import PlatformAdapter
from adapters.types import AgentInvocation, AgentResult, AgentSpec
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from state.schemas import Plan


INITIAL_PLAN_MD = """# Plan: Add foo(x)

## Phase 1: Implement

### Task 1.1: Write foo
  - Description: Add a function foo.
  - Files: foo.py
  - Acceptance:
    - [ ] function exists

## Phase 2: Test

### Task 2.1: Add pytest
  - Description: Add a pytest for foo.
  - Files: test_foo.py
  - Acceptance:
    - [ ] pytest passes
"""


REFINED_PLAN_MD = """# Plan: Add foo(x) (refined)

## Phase 1: Implement foo

### Task 1.1: Write foo(x) with docstring
  - Description: Add foo(x) returning x+1 with a short docstring.
  - Files: foo.py
  - Acceptance:
    - [ ] function exists
    - [ ] docstring describes the return value

## Phase 2: Test

### Task 2.1: Add pytest covering edge cases
  - Description: pytest for foo(0), foo(-1), foo(1).
  - Files: test_foo.py
  - Acceptance:
    - [ ] pytest passes
"""


class PlanTournamentStubAdapter(PlatformAdapter):
    """Adapter that returns role-specific canned text for a plan-tournament run.

    Unlike :class:`tests.stub_adapter.StubAdapter` this one encodes the
    tournament's expected judge RANKING so the tournament converges quickly.
    """

    name = "plan-tournament-stub"

    def __init__(
        self,
        *,
        refined_md: str = REFINED_PLAN_MD,
        judge_favor: str = "AB",
    ) -> None:
        self.calls: list[AgentInvocation] = []
        self._refined_md = refined_md
        self._judge_favor = judge_favor
        # role -> count for round-robin / counting
        self._n: dict[str, int] = {}

    async def init_workspace(self, cwd: Path, agents: list[AgentSpec]) -> None:
        return

    async def healthcheck(self) -> tuple[bool, str]:
        return True, "plan-tournament-stub"

    async def execute(self, inv: AgentInvocation) -> AgentResult:
        self.calls.append(inv)
        self._n[inv.role] = self._n.get(inv.role, 0) + 1
        n = self._n[inv.role]

        if inv.role == "explorer":
            return _ok("explorer findings: repo has foo.py and tests/")
        if inv.role == "domain_expert":
            return _ok("domain_expert: no unusual constraints")
        if inv.role == "architect":
            return _ok(INITIAL_PLAN_MD)
        if inv.role == "critic_t":
            return _ok("pass{n}: minor structural issues".format(n=n))
        if inv.role == "architect_b":
            # Author_b returns the "refined" plan on pass 1; on later passes
            # return the same refined text (idempotent).
            return _ok(self._refined_md)
        if inv.role == "synthesizer":
            return _ok(self._refined_md)
        if inv.role == "judge":
            return _ok(_judge_rank_favoring(inv.prompt, self._judge_favor))

        return _ok(f"[{inv.role}] default-ok")

    def count(self, role: str) -> int:
        """Return how many times ``role`` has been invoked on this adapter."""
        return self._n.get(role, 0)


def _ok(text: str) -> AgentResult:
    return AgentResult(success=True, text=text, duration_s=0.01)


def _judge_rank_favoring(prompt: str, label: str) -> str:
    """Emit a ``RANKING:`` that places ``label`` first.

    The judge prompt carries three ``PROPOSAL N:`` blocks. We locate the slot
    whose body contains a distinctive marker matching the desired canonical
    label, then emit a RANKING placing that slot at position 1.

    For this stub:
        - A = initial plan ⇒ contains "Add foo(x)" (canonical title).
        - B = author_b = refined plan ⇒ contains "(refined)".
        - AB = synthesizer = refined plan ⇒ contains "(refined)".

    A and B/AB are distinguishable by the parenthesized "(refined)" marker.
    When the tournament re-enters with A = refined (after AB wins), A/B/AB
    all contain "(refined)" — we always pick the first such slot, which
    with the conservative A-tiebreak still maps to A. This makes subsequent
    passes converge (A wins twice after initial AB).
    """
    offsets: dict[int, int] = {}
    for slot in (1, 2, 3):
        idx = prompt.find(f"PROPOSAL {slot}:")
        if idx >= 0:
            offsets[slot] = idx
    ordered = sorted(offsets.items(), key=lambda kv: kv[1])
    slot_end: dict[int, int] = {}
    for i, (slot, start) in enumerate(ordered):
        slot_end[slot] = ordered[i + 1][1] if i + 1 < len(ordered) else len(prompt)

    def _body(slot: int) -> str:
        return prompt[offsets[slot] : slot_end[slot]]

    # When favoring AB on the first pass: pick the slot whose body has
    # "(refined)" AND is not the smallest-offset (since A is the initial).
    # Simpler: pick the FIRST slot containing "(refined)".
    # When favoring A: pick the FIRST slot (whatever that is) OR a slot
    # without "(refined)" on pass 1.
    if label == "A":
        # Prefer a slot WITHOUT "(refined)". If none exists (later passes),
        # just pick slot 1.
        for slot in (1, 2, 3):
            if slot in offsets and "(refined)" not in _body(slot):
                preferred = slot
                break
        else:
            preferred = 1
    elif label in ("B", "AB"):
        for slot in (1, 2, 3):
            if slot in offsets and "(refined)" in _body(slot):
                preferred = slot
                break
        else:
            preferred = 1
    else:
        preferred = 1

    others = [s for s in (1, 2, 3) if s != preferred and s in offsets]
    return f"RANKING: {preferred}, {others[0]}, {others[1]}"


# ── Tests ────────────────────────────────────────────────────────────────


def _make_orch(cwd: Path, adapter: PlatformAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.plan.num_judges = 1
    cfg.tournaments.plan.convergence_k = 1  # converge after first non-A pass
    cfg.tournaments.plan.max_rounds = 3
    # Disable auto-disable for this test (default includes "opus" and
    # registry judge model defaults to "sonnet" so it wouldn't trigger;
    # explicitly clear to be safe).
    cfg.tournaments.auto_disable_for_models = []
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-plan-t",
    )


@pytest.mark.asyncio
async def test_plan_phase_runs_tournament_and_uses_refined_plan(
    tmp_path: Path,
) -> None:
    """Plan phase runs tournament; the refined plan is used for init_plan."""
    adapter = PlanTournamentStubAdapter(judge_favor="AB")
    orch = _make_orch(tmp_path, adapter)

    plan = await orch.plan("Add foo(x)")
    assert isinstance(plan, Plan)
    assert "(refined)" in plan.metadata.get("title", "")

    assert adapter.count("architect_b") >= 1


@pytest.mark.asyncio
async def test_plan_phase_tournament_ledger_entry(tmp_path: Path) -> None:
    """A ``plan_tournament_complete`` ledger entry must be appended."""
    adapter = PlanTournamentStubAdapter(judge_favor="AB")
    orch = _make_orch(tmp_path, adapter)

    await orch.plan("Add foo(x)")

    ledger = await orch.plan_manager.read_ledger()
    ops = [e.op for e in ledger]
    assert "plan_tournament_complete" in ops

    entry = next(e for e in ledger if e.op == "plan_tournament_complete")
    assert "tournament_id" in entry.payload
    assert entry.payload["tournament_id"].startswith("plan-")
    assert entry.payload["passes"] >= 1


@pytest.mark.asyncio
async def test_plan_phase_tournament_artifacts_on_disk(tmp_path: Path) -> None:
    """The tournament writes artifacts under ``.autodev/tournaments/plan-*/``."""
    adapter = PlanTournamentStubAdapter(judge_favor="AB")
    orch = _make_orch(tmp_path, adapter)
    await orch.plan("Add foo(x)")

    tournaments_root = tmp_path / ".autodev" / "tournaments"
    assert tournaments_root.exists()
    plan_dirs = [d for d in tournaments_root.iterdir() if d.name.startswith("plan-")]
    assert len(plan_dirs) == 1
    adir = plan_dirs[0]
    assert (adir / "initial_a.md").exists()
    assert (adir / "final_output.md").exists()
    assert (adir / "history.json").exists()


@pytest.mark.asyncio
async def test_plan_phase_tournament_disabled_flag_skips_tournament(
    tmp_path: Path,
) -> None:
    """With ``plan.enabled=False`` the tournament is not invoked at all."""
    from stub_adapter import StubAdapter, ok

    adapter = StubAdapter(
        {
            "explorer": ok("ok"),
            "domain_expert": ok("ok"),
            "architect": ok(INITIAL_PLAN_MD),
        }
    )
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-disabled",
    )
    await orch.plan("Add foo(x)")

    # No tournament roles should have been invoked.
    assert adapter.count("critic_t") == 0
    assert adapter.count("architect_b") == 0
    assert adapter.count("synthesizer") == 0
    assert adapter.count("judge") == 0
    # No ledger entry either.
    ledger = await orch.plan_manager.read_ledger()
    assert all(e.op != "plan_tournament_complete" for e in ledger)
