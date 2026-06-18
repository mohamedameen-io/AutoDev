"""Unmocked end-to-end pipeline test: intake -> diagnosis -> framing -> execute.

Closes WS3-no-e2e-pipeline-test (Phase 0 / B4; gate N3).

WHY THIS EXISTS
---------------
The v0.41 "dead-on-arrival" failure was a *phase silently not running*: intake
and diagnosis were wired in but a role-dispatch ``KeyError`` made them no-op, and
the isolation unit tests (which ``monkeypatch.setattr`` the ``run_*_phase``
functions) could not catch it because they never executed the real orchestration.

This test exercises the REAL pipeline orchestration with NO
``monkeypatch.setattr`` on any ``run_*_phase`` function. The four phases are
driven through the real top-level entrypoint (``Orchestrator.plan`` →
``run_plan_phase``, which internally calls ``run_intake_phase``,
``run_diagnosis_phase`` and ``run_framing_phase`` in sequence, then
``Orchestrator.execute`` → ``run_execute_phase``). The LLM roles are the ONLY
thing stubbed — each role returns a minimal VALID-SHAPED output so every phase
produces its real artifact.

ENGAGEMENT-FIRST
----------------
Each assertion proves a phase *actually ran* by reading a LEDGER OP it emits
(plus the persisted evidence), not just "no crash":

  * intake     -> ``spec_locked`` op + non-empty ``.autodev/spec.md``
  * diagnosis  -> ``seam_finding`` / ``hypotheses_ranked`` ops + >=1 parsed
                  hypothesis in the persisted ``DiagnosisEvidence``
  * framing    -> ``framing_classified`` op + persisted ``FramingEvidence``
  * execute    -> a terminal ``update_task_status`` op (complete/blocked/skipped)

NON-VACUITY
-----------
``test_e2e_diagnosis_finding_assertion_is_non_vacuous`` proves the diagnosis
gate FIRES on the broken case: when the diagnostician role returns an empty
body (the v0.41 silent-no-op shape), the persisted evidence has ZERO hypotheses,
so the ``len(hypotheses) >= 1`` assertion the green test relies on would FAIL.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Iterable

import pytest

from adapters.types import AgentResult
from agents import build_registry
from config.defaults import default_config
from config.loader import save_config
from config.schema import AutodevConfig, QAGatesConfig
from orchestrator import Orchestrator
from state.evidence import read_evidence
from state.plan_manager import PlanManager
from state.schemas import DiagnosisEvidence, FramingEvidence, IntakeEvidence

from stub_adapter import StubAdapter, ok

# Terminal task statuses an ``update_task_status`` ledger op may carry when a
# task reaches the end of the execute FSM (mirrors execute_phase._TERMINAL_*).
_TERMINAL_STATUSES = frozenset({"complete", "blocked", "skipped"})

# A bug-shaped intent. It MUST contain a bug marker (``bug``/``fix``) so the
# diagnosis phase's ``is_bug_fix`` gate fires and the phase actually dispatches
# the diagnostician (rather than skipping as feature work).
_BUG_INTENT = "Fix the off-by-one bug in math_utils.add returning the wrong sum"


# ---------------------------------------------------------------------------
# Architect plan markdown — deterministic output from the architect role.
# Two tasks so the execute phase has real work to drive to a terminal.
# ---------------------------------------------------------------------------

_PLAN_MD = """
# Plan: Fix off-by-one in add

## Phase 1: Fix and test

### Task 1.1: Correct add in math_utils.py
  - Description: Return a + b (drop the stray +1)
  - Files: math_utils.py
  - Acceptance:
    - [ ] add returns the correct sum

### Task 1.2: Add a regression pytest
  - Description: Add a pytest pinning add(1, 2) == 3
  - Files: test_math_utils.py
  - Acceptance:
    - [ ] tests pass
"""


# A diagnostician response in the exact structured shape the parser expects:
# a loop block, REPRODUCED/SYMPTOM/CONFIRMED_CAUSE/SEAM lines, and at least one
# falsifiable HYPOTHESIS (``statement || prediction``) so the persisted evidence
# carries >=1 finding for the bug.
_DIAGNOSTICIAN_MD = """
LOOP_METHOD: failing_test
LOOP_COMMAND: pytest test_math_utils.py
LOOP_FIDELITY: synthetic
LOOP_DETERMINISTIC: true
REPRODUCED: true
SYMPTOM: add(1, 2) returns 4 instead of 3
CONFIRMED_CAUSE: add() adds a stray +1 to the result
SEAM: correct
RECURRENCE_AT_SEAM: false

HYPOTHESIS 1: add has a stray +1 || removing +1 makes add(1,2)==3
HYPOTHESIS 2: caller passes wrong args || fixing args alone leaves the bug
"""


# A framing classifier response in the ```framing fenced shape. A conservative
# local_defect classification is the realistic default for a one-line bug and
# still emits the ``framing_classified`` ledger op the test asserts on.
_FRAMING_MD = """
```framing
CLASSIFICATION: local_defect
CONFIDENCE: 0.2
HYPOTHESIS_CHALLENGED: is this a one-line patch or a design smell?
SIGNALS_FIRED: none
```
"""


# An intake_enricher response that doubles as (a) the gather facts block and
# (b) the enriched spec. The ```facts block lets the repo gather source parse a
# provenance-carrying fact; the prose below it becomes the enriched spec draft.
_ENRICHER_MD = """
The bug is a stray +1 in math_utils.add. Scope: fix add to return a + b.
Acceptance: add(1, 2) == 3. Constraints: keep the public signature stable.
Touchpoints: math_utils.py:add.

```facts
repo | math_utils.py:1-2 | add currently returns a + b + 1 (off-by-one)
```
"""


_CLARIFIER_MD = """
```questions
```
"""


def _developer_result() -> AgentResult:
    return AgentResult(
        success=True,
        text="fixed off-by-one",
        diff=(
            "diff --git a/math_utils.py b/math_utils.py\n"
            "--- a/math_utils.py\n"
            "+++ b/math_utils.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-def add(a, b):\n"
            "-    return a + b + 1\n"
            "+def add(a, b):\n"
            "+    return a + b\n"
        ),
        files_changed=[Path("math_utils.py")],
        duration_s=0.01,
    )


def _stub(extras: Iterable[tuple[str, object]] | None = None) -> StubAdapter:
    """Valid-shaped stub responses for EVERY role the real pipeline invokes.

    Covers the plan roles (explorer/domain_expert/architect/developer/reviewer/
    test_engineer) AND the specialist phase roles wired into ``run_plan_phase``
    (intake_enricher — gather + enrich, intake_clarifier, diagnostician, framing).
    """
    responses: dict[str, object] = {
        # Plan-phase delegated roles.
        "explorer": ok("math_utils.py has add() returning a + b + 1 (off-by-one)"),
        "domain_expert": ok("simple arithmetic; the off-by-one is the only defect"),
        "architect": ok(_PLAN_MD),
        "developer": _developer_result(),
        "reviewer": ok("APPROVED\n- correct one-line fix"),
        "test_engineer": ok("RESULTS: passed=2 failed=0 total=2"),
        # Specialist phase roles (specialist load_prompt dispatch path).
        "intake_enricher": ok(_ENRICHER_MD),
        "intake_clarifier": ok(_CLARIFIER_MD),
        "diagnostician": ok(_DIAGNOSTICIAN_MD),
        "framing": ok(_FRAMING_MD),
    }
    if extras:
        responses.update(dict(extras))
    return StubAdapter(responses)


def _git_init_repo(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(repo), check=True)


def _git_commit_all(repo: Path) -> None:
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=str(repo), check=True)


def _make_buggy_repo(tmp_path: Path) -> Path:
    """A tiny real git repo whose ``add`` has an off-by-one bug."""
    repo = tmp_path / "buggy_repo"
    repo.mkdir()
    _git_init_repo(repo)
    (repo / "math_utils.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b + 1\n"
    )
    (repo / "test_math_utils.py").write_text(
        "from math_utils import add\n\n"
        "def test_add() -> None:\n    assert add(1, 2) == 3\n"
    )
    (repo / "README.md").write_text("# Buggy Repo\n")
    _git_commit_all(repo)
    return repo


def _make_full_pipeline_config(repo: Path) -> AutodevConfig:
    """Config with ALL four phases ON but tournaments + QA gates OFF.

    Tournaments are disabled to keep the run deterministic and fast; QA gates are
    disabled so execute is not blocked by environment tooling (eslint, etc.).
    Crucially, intake / diagnosis / framing are LEFT ENABLED (their defaults) so
    the real ``run_plan_phase`` actually dispatches them — that is the whole
    point of this test.
    """
    cfg = default_config()
    cfg.platform = "claude_code"
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.qa_gates = QAGatesConfig(
        syntax_check=False,
        lint=False,
        build_check=False,
        test_runner=False,
        secretscan=False,
        sast_scan=False,
        mutation_test=False,
    )
    assert cfg.intake.enabled, "intake must be ON for this e2e test"
    assert cfg.diagnosis.enabled, "diagnosis must be ON for this e2e test"
    assert cfg.framing.enabled, "framing must be ON for this e2e test"
    save_config(cfg, repo / ".autodev" / "config.json")
    return cfg


def _ledger_ops(entries: list[object]) -> list[str]:
    return [getattr(e, "op", "") for e in entries]


# ---------------------------------------------------------------------------
# The unmocked e2e pipeline test.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_e2e_pipeline_full_unmocked(tmp_path: Path) -> None:
    """intake -> diagnosis -> framing -> execute through the REAL orchestration.

    NO ``monkeypatch.setattr`` on any ``run_*_phase``: the phases run for real
    via ``Orchestrator.plan`` (→ ``run_plan_phase`` which wires intake/diagnosis/
    framing) and ``Orchestrator.execute`` (→ ``run_execute_phase``). Only the LLM
    roles are stubbed. Every assertion checks a LEDGER OP the phase emits.
    """
    repo = _make_buggy_repo(tmp_path)
    cfg = _make_full_pipeline_config(repo)
    adapter = _stub()
    registry = build_registry(cfg)

    # --- PLAN: drives intake -> diagnosis -> framing -> architect (all real) ---
    orch = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="e2e-full-plan",
    )
    plan = await orch.plan(_BUG_INTENT)
    assert len(plan.phases) >= 1
    assert len(plan.phases[0].tasks) >= 1

    # Read the ledger ONCE for the plan-phase assertions.
    pm = PlanManager(repo, session_id="e2e-reader")
    plan_entries = await pm.read_ledger()
    plan_ops = _ledger_ops(plan_entries)

    # 1) INTAKE actually ran: it locked a spec (op + non-empty artifact).
    assert "spec_locked" in plan_ops, (
        f"intake did not emit spec_locked; ledger ops: {sorted(set(plan_ops))}"
    )
    spec_md = repo / ".autodev" / "spec.md"
    assert spec_md.exists() and spec_md.read_text().strip(), (
        "intake must produce a non-empty .autodev/spec.md"
    )
    intake_ev = await read_evidence(repo, "plan-intake", "intake")
    assert isinstance(intake_ev, IntakeEvidence)
    assert intake_ev.locked_spec_hash, "intake evidence must carry a locked spec hash"

    # 2) DIAGNOSIS actually ran: seam finding emitted AND >=1 parsed hypothesis.
    #    The hypotheses list is THE non-vacuity hook — it is 0 if diagnosis was
    #    skipped or returned an empty body (see the non-vacuity test below).
    assert "seam_finding" in plan_ops, (
        f"diagnosis did not emit seam_finding; ops: {sorted(set(plan_ops))}"
    )
    assert "hypotheses_ranked" in plan_ops, (
        f"diagnosis did not emit hypotheses_ranked; ops: {sorted(set(plan_ops))}"
    )
    diag_ev = await read_evidence(repo, "plan-diagnosis", "diagnosis")
    assert isinstance(diag_ev, DiagnosisEvidence)
    assert len(diag_ev.hypotheses) >= 1, (
        "diagnosis must produce >=1 finding (hypothesis) for the bug; "
        f"got {len(diag_ev.hypotheses)}"
    )

    # 3) FRAMING actually ran: classification op + persisted classification.
    assert "framing_classified" in plan_ops, (
        f"framing did not emit framing_classified; ops: {sorted(set(plan_ops))}"
    )
    framing_ev = await read_evidence(repo, "plan-framing", "framing")
    assert isinstance(framing_ev, FramingEvidence)
    assert framing_ev.classification in ("local_defect", "realized_design_failure")

    # Sanity: the specialist phase roles were genuinely invoked through the
    # adapter (the real dispatch path), not skipped.
    assert adapter.count("diagnostician") >= 1
    assert adapter.count("framing") >= 1
    assert adapter.count("intake_enricher") >= 1

    # --- EXECUTE: drives run_execute_phase to a real terminal ---
    orch2 = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=_stub(),
        registry=registry,
        session_id="e2e-full-exec",
    )
    tasks = await orch2.execute()
    assert len(tasks) >= 1

    # 4) EXECUTE reached a terminal: an update_task_status op carrying a terminal
    #    status (complete/blocked/skipped) — a REAL terminal op, not "no crash".
    final_entries = await pm.read_ledger()
    terminal_ops = [
        e
        for e in final_entries
        if getattr(e, "op", "") == "update_task_status"
        and getattr(e, "payload", {}).get("status") in _TERMINAL_STATUSES
    ]
    assert terminal_ops, (
        "execute reached no terminal update_task_status op; "
        f"ops seen: {sorted(set(_ledger_ops(final_entries)))}"
    )
    # And the in-memory tasks themselves are terminal (defensive cross-check).
    assert all(t.status in _TERMINAL_STATUSES for t in tasks), (
        f"tasks not terminal: {[(t.id, t.status) for t in tasks]}"
    )

    # The plan persisted as real JSON (proves the architect output was applied).
    plan_json = repo / ".autodev" / "plan.json"
    assert plan_json.exists()
    raw = json.loads(plan_json.read_text())
    assert raw.get("phases"), "plan.json must contain at least one phase"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_e2e_diagnosis_finding_assertion_is_non_vacuous(tmp_path: Path) -> None:
    """PROVE the green test's diagnosis assertion FIRES on the broken case.

    This is the engagement-gate guarantee: if the diagnostician role returns an
    EMPTY body (the v0.41 silent-no-op shape — the phase "runs" but yields
    nothing), the persisted ``DiagnosisEvidence`` carries ZERO hypotheses. The
    green test asserts ``len(hypotheses) >= 1``, so it would FAIL here. We assert
    the inverse (== 0) to lock in that the gate is non-vacuous.
    """
    repo = _make_buggy_repo(tmp_path)
    cfg = _make_full_pipeline_config(repo)
    # Diagnostician returns an empty body — no HYPOTHESIS lines to parse.
    adapter = _stub(extras=[("diagnostician", ok(""))])
    registry = build_registry(cfg)

    orch = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="e2e-nonvacuous",
    )
    await orch.plan(_BUG_INTENT)

    # The phase still RAN (the role was dispatched) ...
    assert adapter.count("diagnostician") >= 1
    diag_ev = await read_evidence(repo, "plan-diagnosis", "diagnosis")
    assert isinstance(diag_ev, DiagnosisEvidence)
    # ... but produced NO findings. The green test's ``>= 1`` assertion would
    # fail here — proving it is not a vacuous always-true check.
    assert len(diag_ev.hypotheses) == 0, (
        "expected ZERO hypotheses from an empty diagnostician body; the green "
        f"test's >=1 assertion must fire on this case (got {len(diag_ev.hypotheses)})"
    )
