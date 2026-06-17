"""Phase 3 / N5 determinism gate.

Closes the critic ``no-reproducibility-gate`` finding.

GATE N5 (from the stabilization gate-doc): *same input does not silently
produce a materially different fix.* AutoDev drives all LLM calls through a
deterministic :class:`StubAdapter` in tests, so given **fixed** agent outputs
the ORCHESTRATION layer must itself be deterministic — no hidden
``time.time``/``random``/dict-or-set-ordering may leak into the realised fix.

Strategy
--------
Drive a REAL multi-step task (a 2-task single-phase plan, executed via
``Orchestrator.execute`` → ``run_execute_phase``) **twice** with two
independently-constructed StubAdapters that return identical responses, and
assert run A and run B agree on the gate-doc N5 scope tuple::

    (n_tasks, sorted(files_changed))

``files_changed`` is read back from the developer evidence bundles that the
orchestrator actually wrote to ``.autodev/evidence/`` — i.e. what the system
*produced*, not what the test wished for.

The two tasks touch **distinct** files, so they are parallel-eligible in the
DAG dispatcher (``asyncio.wait(FIRST_COMPLETED)``). The raw completion order
of that dispatcher is legitimately non-deterministic; the N5 criterion sorts
``files_changed`` precisely so that scheduling jitter does not count as a
*materially different fix*. A set/dict-ordering bug that leaked into the
realised scope, by contrast, WOULD flip the sorted tuple and be caught.

ANTI-VACUITY
------------
A gate that passes on the empty / found-nothing case is the bug. Every run
asserts ``n_tasks >= 1`` (in fact 2 complete tasks) and that
``files_changed`` is NON-empty before the cross-run comparison, so the gate
cannot pass on a no-op / empty plan.

BROKEN-CONTROL
--------------
``test_n5_scope_tuple_broken_control_detects_divergence`` perturbs the second
run's stub responses (a different file set) and asserts the SAME equality
check the gate uses now FAILS — proving the assertion has teeth and is not
trivially satisfied.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from state.evidence import read_evidence
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    Task,
)

from stub_adapter import StubAdapter, ok


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# --- A real, non-trivial multi-step plan --------------------------------

# Two tasks that each touch a DISTINCT file. Distinct files => the DAG
# dispatcher may run them concurrently, exercising the parallel-completion
# path whose raw ordering is non-deterministic by design. The N5 tuple sorts
# files_changed so legitimate scheduling jitter is normalised away.
_TASK_FILE = {"1.1": "math.py", "1.2": "util.py"}


def _mk_multistep_plan() -> Plan:
    """A genuinely multi-step plan: 2 tasks, 2 distinct files.

    NOT a no-op/empty plan — the anti-vacuity precondition requires the
    realised fix to touch >= 1 file and >= 1 task to complete.
    """
    tasks = [
        Task(
            id="1.1",
            phase_id="1",
            title="Add subtract",
            description="Implement subtract(a, b) in math.py",
            files=[_TASK_FILE["1.1"]],
            acceptance=[AcceptanceCriterion(id="ac-1", description="tests pass")],
        ),
        Task(
            id="1.2",
            phase_id="1",
            title="Add slugify",
            description="Implement slugify(s) in util.py",
            files=[_TASK_FILE["1.2"]],
            acceptance=[AcceptanceCriterion(id="ac-1", description="tests pass")],
        ),
    ]
    return Plan(
        plan_id="p-n5-determinism",
        spec_hash="d",
        phases=[Phase(id="1", title="Work", tasks=tasks)],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _coder_for(file_name: str) -> AgentResult:
    """A successful developer result whose diff/files_changed name ``file_name``."""
    return AgentResult(
        success=True,
        text=f"wrote {file_name}",
        diff=(
            f"diff --git a/{file_name} b/{file_name}\n"
            f"--- a/{file_name}\n"
            f"+++ b/{file_name}\n"
            "@@ -0,0 +1 @@\n"
            f"+# generated edit for {file_name}\n"
        ),
        files_changed=[Path(file_name)],
        duration_s=0.1,
    )


def _developer_dispatch(inv: object) -> AgentResult:
    """Route the developer call to the right per-task file by prompt content.

    The developer prompt carries the task description, which names the target
    file. This lets a SINGLE stub serve both tasks with file-specific output
    (a real multi-step fix), independent of completion order.
    """
    prompt = getattr(inv, "prompt", "") or ""
    if _TASK_FILE["1.2"] in prompt:
        return _coder_for(_TASK_FILE["1.2"])
    # Default to the 1.1 file (math.py) — its description is the other branch.
    return _coder_for(_TASK_FILE["1.1"])


def _make_adapter() -> StubAdapter:
    """Fresh adapter with the SAME response mapping for every run.

    Building a new adapter per run guarantees the two runs share no mutable
    state (call counters, recorded calls) — any agreement is the orchestration
    being deterministic, not a shared cache.
    """
    return StubAdapter(
        {
            "developer": _developer_dispatch,
            "reviewer": ok("APPROVED\n- clean"),
            "test_engineer": ok("ran pytest\nRESULTS: passed=3 failed=0 total=3"),
        }
    )


async def _run_once(cwd: Path, adapter: StubAdapter) -> tuple[int, list[str]]:
    """Drive a full multi-step execute phase and return the N5 scope tuple.

    Returns ``(n_complete_tasks, sorted(files_changed))`` where
    ``files_changed`` is read back from the developer evidence bundles the
    orchestrator actually wrote — observed output, not assumed output.
    """
    cfg = default_config()
    # Pin the orchestration shape: disable the LLM tournaments so the only
    # variance under test is the execute-phase scheduling/recording layer.
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-n5",
    )
    await orch.plan_manager.init_plan(_mk_multistep_plan())

    tasks = await orch.execute()

    complete = [t for t in tasks if t.status == "complete"]
    files: set[str] = set()
    for t in complete:
        ev = await read_evidence(cwd, t.id, "developer")
        # CoderEvidence.files_changed: what the developer actually changed.
        for f in getattr(ev, "files_changed", []) or []:
            files.add(f)
    return len(complete), sorted(files)


@pytest.mark.asyncio
async def test_n5_orchestration_is_deterministic_under_stub(tmp_path: Path) -> None:
    """N5: same StubAdapter responses => identical (n_tasks, sorted(files)).

    Run A and run B use independently-constructed adapters with identical
    response maps, in separate working dirs. The realised fix scope must be
    byte-identical on the gate tuple.
    """
    cwd_a = tmp_path / "run_a"
    cwd_b = tmp_path / "run_b"
    cwd_a.mkdir()
    cwd_b.mkdir()

    n_a, files_a = await _run_once(cwd_a, _make_adapter())
    n_b, files_b = await _run_once(cwd_b, _make_adapter())

    # --- ANTI-VACUITY: the fixture must be a REAL multi-step task ---
    # If either run did nothing, the equality below would pass trivially.
    assert n_a >= 1, "run A completed no tasks — gate would be vacuous"
    assert files_a, "run A changed no files — gate would be vacuous"
    # Stronger than the doc minimum: this fixture is a genuine 2-task fix.
    assert n_a == 2, f"expected 2 completed tasks in run A, got {n_a}"
    assert files_a == [_TASK_FILE["1.1"], _TASK_FILE["1.2"]], files_a

    # --- N5 GATE: same input => same realised fix scope ---
    assert (n_a, files_a) == (n_b, files_b), (
        "N5 determinism gate FAILED: identical StubAdapter responses produced "
        f"a materially different fix.\n  run A = {(n_a, files_a)}\n"
        f"  run B = {(n_b, files_b)}\n"
        "Investigate hidden non-determinism (time.time / random / set or dict "
        "ordering) in the execute-phase orchestration."
    )


@pytest.mark.asyncio
async def test_n5_scope_tuple_broken_control_detects_divergence(
    tmp_path: Path,
) -> None:
    """BROKEN-CONTROL: a deliberately divergent run must FAIL the N5 check.

    Proves the equality assertion in the gate has teeth: when run B's stub is
    perturbed to realise a DIFFERENT file set, the same comparison the gate
    uses flips to non-equal. A gate that could not detect this divergence
    would be the bug.
    """
    cwd_a = tmp_path / "ctl_a"
    cwd_b = tmp_path / "ctl_b"
    cwd_a.mkdir()
    cwd_b.mkdir()

    # Baseline run: the real fix (math.py + util.py).
    n_a, files_a = await _run_once(cwd_a, _make_adapter())

    # Perturbed adapter: the developer always edits a DIFFERENT file, so the
    # realised scope diverges from the baseline.
    def _perturbed_dispatch(inv: object) -> AgentResult:
        return _coder_for("DIVERGENT.py")

    perturbed = StubAdapter(
        {
            "developer": _perturbed_dispatch,
            "reviewer": ok("APPROVED\n- clean"),
            "test_engineer": ok("ran pytest\nRESULTS: passed=3 failed=0 total=3"),
        }
    )
    n_b, files_b = await _run_once(cwd_b, perturbed)

    # The baseline is the genuine multi-file fix (anti-vacuity for the control).
    assert files_a == [_TASK_FILE["1.1"], _TASK_FILE["1.2"]], files_a
    # The perturbed run realised a different scope...
    assert files_b == ["DIVERGENT.py"], files_b
    # ...so the SAME equality check the N5 gate relies on must report divergence.
    assert (n_a, files_a) != (n_b, files_b), (
        "broken-control failed: the N5 scope-tuple comparison did NOT detect a "
        "deliberately divergent fix — the gate has no teeth"
    )
