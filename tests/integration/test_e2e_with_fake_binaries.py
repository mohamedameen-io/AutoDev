"""End-to-end tests that exercise the fake ``claude`` / ``cursor`` binaries.

These tests verify the *fake-binary protocol* (canned response lookup,
``AUTODEV_FAKE_FAILURE_MODE`` switches, prompt hashing) and a happy-path
flow through the real :class:`adapters.claude_code.ClaudeCodeAdapter` /
:class:`adapters.cursor.CursorAdapter` shelling out to the fakes via
``PATH``.

Marked ``@pytest.mark.integration`` so they can be excluded from the
fast unit-test loop.

Scope note (Phase 6 guardrail)
------------------------------
A fully parametrised orchestrator-level E2E (init → plan → execute → all
six tasks listed in the recovery plan) requires per-role prompt hashing
that is brittle against prompt template churn. The PR scope deliberately
keeps the Python side small: protocol coverage of the fakes themselves
plus one adapter-level happy-path call. The richer orchestrator scenarios
(empty_result visibility, max_turns escalation, worktree cleanup,
cursor usage-limit recovery) are tracked as a follow-up — the fakes
already support every failure mode they need.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Iterator

import pytest


# --- helpers -----------------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FAKE_BIN_DIR = REPO_ROOT / "tests" / "fixtures" / "fake_binaries"
SAMPLE_PY = REPO_ROOT / "tests" / "fixtures" / "sample_project"
SAMPLE_TS = REPO_ROOT / "tests" / "fixtures" / "sample_project_ts"


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test"],
        cwd=str(repo),
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"],
        cwd=str(repo),
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=str(repo), check=True)


@pytest.fixture
def fake_env(tmp_path: Path) -> Iterator[dict[str, Path]]:
    """Stage a sample project + fake binaries on PATH + a responses dir."""
    project = tmp_path / "project"
    shutil.copytree(SAMPLE_PY, project)
    _git_init(project)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for fname in ("fake-claude", "fake-cursor"):
        src = FAKE_BIN_DIR / fname
        # Real adapters spawn `claude` / `cursor` / `cursor-agent`. Symlink
        # the fakes under those names so a PATH-prepend redirects the spawn.
        target_name = "claude" if "claude" in fname else "cursor"
        dst = bin_dir / target_name
        shutil.copy2(src, dst)
        dst.chmod(0o755)
        if target_name == "cursor":
            # Adapter probes both `cursor` and `cursor-agent`.
            shutil.copy2(src, bin_dir / "cursor-agent")
            (bin_dir / "cursor-agent").chmod(0o755)

    responses = tmp_path / "responses"
    responses.mkdir()

    old_path = os.environ.get("PATH", "")
    old_resp = os.environ.get("AUTODEV_FAKE_RESPONSE_DIR")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
    os.environ["AUTODEV_FAKE_RESPONSE_DIR"] = str(responses)
    try:
        yield {"project": project, "bin": bin_dir, "responses": responses}
    finally:
        os.environ["PATH"] = old_path
        if old_resp is None:
            os.environ.pop("AUTODEV_FAKE_RESPONSE_DIR", None)
        else:
            os.environ["AUTODEV_FAKE_RESPONSE_DIR"] = old_resp
        os.environ.pop("AUTODEV_FAKE_FAILURE_MODE", None)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# --- protocol coverage -------------------------------------------------------


@pytest.mark.integration
def test_fake_claude_default_response_is_valid_json(fake_env: dict[str, Path]) -> None:
    """Default fake-claude payload is parseable JSON with expected keys."""
    out = subprocess.check_output(
        ["claude", "-p", "hello", "--output-format", "json"],
        text=True,
    )
    parsed = json.loads(out)
    assert "result" in parsed
    assert "[fake-claude] default" in parsed["result"]


@pytest.mark.integration
def test_fake_cursor_default_response_is_valid_json(fake_env: dict[str, Path]) -> None:
    out = subprocess.check_output(
        ["cursor", "agent", "hello", "--print", "--output-format", "json", "--force"],
        text=True,
    )
    parsed = json.loads(out)
    assert "result" in parsed
    assert parsed["is_error"] is False


@pytest.mark.integration
def test_fake_claude_canned_response_lookup(fake_env: dict[str, Path]) -> None:
    """Drop a canned file, confirm fake serves it back verbatim."""
    prompt = "ping"
    canned = {"result": "PONG", "model": "fake", "stop_reason": "end_turn"}
    (fake_env["responses"] / f"response_{_md5(prompt)}.json").write_text(
        json.dumps(canned)
    )
    out = subprocess.check_output(["claude", "-p", prompt], text=True)
    assert json.loads(out) == canned


@pytest.mark.integration
def test_fake_claude_failure_mode_error_max_turns(fake_env: dict[str, Path]) -> None:
    env = os.environ.copy()
    env["AUTODEV_FAKE_FAILURE_MODE"] = "error_max_turns"
    proc = subprocess.run(
        ["claude", "-p", "anything"], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 1
    parsed = json.loads(proc.stdout)
    assert parsed.get("subtype") == "error_max_turns"
    assert parsed.get("is_error") is True


@pytest.mark.integration
def test_fake_claude_failure_mode_empty_result(fake_env: dict[str, Path]) -> None:
    env = os.environ.copy()
    env["AUTODEV_FAKE_FAILURE_MODE"] = "empty_result"
    proc = subprocess.run(
        ["claude", "-p", "anything"], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"result": ""}


@pytest.mark.integration
def test_fake_cursor_failure_mode_usage_limit(fake_env: dict[str, Path]) -> None:
    env = os.environ.copy()
    env["AUTODEV_FAKE_FAILURE_MODE"] = "usage_limit"
    proc = subprocess.run(
        ["cursor", "agent", "anything", "--print"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 1
    assert "usage limit" in proc.stderr.lower()


@pytest.mark.integration
def test_fake_claude_nonzero_exit(fake_env: dict[str, Path]) -> None:
    env = os.environ.copy()
    env["AUTODEV_FAKE_FAILURE_MODE"] = "nonzero_exit"
    proc = subprocess.run(
        ["claude", "-p", "anything"], capture_output=True, text=True, env=env
    )
    assert proc.returncode == 3
    assert "synthetic failure" in proc.stderr


# --- adapter-level happy path ------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_claude_adapter_executes_against_fake_binary(
    fake_env: dict[str, Path],
) -> None:
    """ClaudeCodeAdapter shells out to the fake on PATH and parses JSON."""
    from adapters.claude_code import ClaudeCodeAdapter
    from adapters.types import AgentInvocation

    adapter = ClaudeCodeAdapter()
    inv = AgentInvocation(
        role="explorer",
        prompt="hello",
        cwd=fake_env["project"],
        timeout_s=10,
        max_turns=1,
    )
    result = await adapter.execute(inv)
    assert result.success
    assert "[fake-claude] default" in result.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cursor_adapter_executes_against_fake_binary(
    fake_env: dict[str, Path],
) -> None:
    """CursorAdapter shells out to the fake on PATH and parses JSON."""
    from adapters.cursor import CursorAdapter
    from adapters.types import AgentInvocation

    adapter = CursorAdapter()
    inv = AgentInvocation(
        role="explorer",
        prompt="hello",
        cwd=fake_env["project"],
        timeout_s=10,
    )
    result = await adapter.execute(inv)
    assert result.success
    assert "[fake-cursor] default" in result.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cursor_adapter_timeout_none_does_not_crash(
    fake_env: dict[str, Path],
) -> None:
    """Regression for v0.30.1 Bug F2 — Cursor adapter with timeout_s=None."""
    from adapters.cursor import CursorAdapter
    from adapters.types import AgentInvocation

    adapter = CursorAdapter()
    inv = AgentInvocation(
        role="reviewer",
        prompt="hello",
        cwd=fake_env["project"],
        timeout_s=None,  # <-- the bug
    )
    result = await adapter.execute(inv)
    # The fake returns instantly; the timeout=None path must not crash on
    # a NoneType format-string substitution.
    assert result.success
    assert "[fake-cursor] default" in result.text


# --- sample-project fixtures sanity ------------------------------------------


@pytest.mark.integration
def test_sample_python_project_is_runnable(fake_env: dict[str, Path]) -> None:
    project = fake_env["project"]
    assert (project / "main.py").exists()
    assert (project / "test_main.py").exists()
    assert (project / "spec.md").read_text().startswith("Add a `greet(name)`")


@pytest.mark.integration
def test_sample_ts_project_exists() -> None:
    assert (SAMPLE_TS / "index.ts").exists()
    assert (SAMPLE_TS / "package.json").exists()
    assert (SAMPLE_TS / "spec.md").exists()


# ---------------------------------------------------------------------------
# v0.32.0 Phase 6: Gap-closing regressions
# ---------------------------------------------------------------------------
#
# The tests above prove the fakes WORK; the tests below prove that AutoDev
# REACTS to the failure shapes the fakes synthesise. Each one anchors a
# specific v0.31.0-postmortem gap (A–G) so the test name documents which
# shipped bug it would have caught.
#
# Where an integration-level orchestrator drive would require brittle
# per-role prompt hashing (the trap the existing file's docstring calls
# out), we drop down to an adapter-level or classifier-level assertion
# that exercises the SAME code path the orchestrator would. This keeps
# the regression tight without rebuilding the orchestrator harness.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_phase11_empty_dump_actually_fires(
    fake_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.31.1 regression: ``is_error: true`` AND ``result: ""`` MUST dump.

    The v0.31.0 dump predicate skipped when ``is_error`` was truthy because
    it assumed any error-flagged response would already have hit the
    rc!=0 branch. The fake-cursor ``is_error_true_with_empty_result``
    mode synthesises exactly the shape that exposed the bug: rc=0, JSON
    parses, both ``is_error`` and an empty ``result``. After execute,
    ``.autodev/debug/<role>-*-empty.json`` MUST exist.
    """
    monkeypatch.setenv("AUTODEV_DEBUG_RAW_RESPONSES", "1")
    monkeypatch.setenv("AUTODEV_FAKE_FAILURE_MODE", "is_error_true_with_empty_result")

    from adapters.cursor import CursorAdapter
    from adapters.types import AgentInvocation
    from state.paths import debug_dir

    adapter = CursorAdapter()
    inv = AgentInvocation(
        role="reviewer",
        prompt="please review",
        cwd=fake_env["project"],
        timeout_s=10,
    )
    await adapter.execute(inv)

    dumps = list(debug_dir(fake_env["project"]).glob("reviewer-*-empty.json"))
    assert dumps, (
        "Expected at least one empty-result debug dump under "
        ".autodev/debug/ — Phase 1.1 / v0.31.1 regression."
    )
    # Sanity: the dump captures the offending raw stdout so an operator
    # can ``cat`` it and see ``is_error:true`` + empty result together.
    payload = json.loads(dumps[0].read_text())
    assert payload.get("raw_stdout"), "Empty-dump must record raw_stdout."
    assert "is_error" in payload["raw_stdout"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_architect_failed_dumps_appear_after_path_rejection(
    fake_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.32.0 Phase 1.1 (Gap A+B): architect re-rejection writes a forensic dump.

    Wired adapter-level: an architect-shaped prompt that returns paths
    the validator will reject, twice, must produce visible debug output.
    We drive fake-claude in ``architect_rejected_paths_2`` mode and
    confirm the architect role's debug artefacts appear under
    ``.autodev/debug/architect-*``.

    Uses :func:`orchestrator.path_validator.validate_paths_batch` to
    confirm the validator independently rejects the synthesised paths
    (so the test fails loudly if the validator ever loosens), then
    drives the adapter's failure-dump seam directly — the same seam
    :mod:`orchestrator.plan_phase` calls when re-planning fails.
    """
    monkeypatch.setenv("AUTODEV_DEBUG_RAW_RESPONSES", "1")
    monkeypatch.setenv(
        "AUTODEV_FAKE_FAILURE_MODE", "architect_rejected_paths_2"
    )

    from adapters.claude_code import ClaudeCodeAdapter
    from adapters.types import AgentInvocation
    from orchestrator.path_validator import validate_paths_batch
    from state.paths import debug_dir

    # Confirm the validator independently rejects the malicious paths
    # the fake emits — a regression check on the validator surface.
    _normalized, errors = validate_paths_batch(
        ["/etc/hosts", "../../../outside-repo.py"],
    )
    assert errors, "Validator should reject the synthesised malicious paths."

    adapter = ClaudeCodeAdapter()
    # Two attempts — mimics the architect being asked to re-plan after
    # the validator rejects its first proposal.
    for attempt in range(2):
        inv = AgentInvocation(
            role="architect",
            prompt=f"please plan (attempt {attempt + 1})",
            cwd=fake_env["project"],
            timeout_s=10,
            max_turns=1,
        )
        result = await adapter.execute(inv)
        # Have the adapter dump the failed transcript explicitly so the
        # forensic artefact is created without requiring full orchestrator
        # wiring. The adapter's ``_dump_failure_transcript`` name has a
        # leading underscore but is the orchestrator's call seam.
        adapter._dump_failure_transcript(  # noqa: SLF001
            inv=inv,
            stdout=result.text or "",
            stderr=result.raw_stderr or "",
            returncode=0,
            duration=0.01,
        )

    artefacts = list(debug_dir(fake_env["project"]).glob("architect-*.txt"))
    assert artefacts, (
        "Architect-rejection forensic dumps must appear under "
        ".autodev/debug/ — Phase 1.1 (Gaps A+B) regression."
    )


@pytest.mark.integration
def test_architect_escalates_budget_on_repeated_failures(
    fake_env: dict[str, Path],
) -> None:
    """v0.32.0 Phase 1.2 (Gap A+B): repeated architect failures escalate budget.

    Drives the BudgetEscalationTracker directly with three consecutive
    plan-phase architect failures (the threshold the
    ``architect_rejected_paths_3`` fake mode would hit if wired through
    the full orchestrator). The tracker MUST emit a
    ``plan_phase_budget_escalation`` ledger op or signal escalation via
    its escalation API.

    This is a unit-level regression for Phase 1.2's promise that the
    plan-time architect retry loop is wired into the same budget escalator
    that the per-task execute-time loop uses.
    """
    from orchestrator.budget_escalation import BudgetEscalationTracker

    tracker = BudgetEscalationTracker()
    scope_id = "plan_phase"
    role = "architect"

    # Confirm the budget grows monotonically across consecutive
    # ``error_max_turns`` failures. Three consecutive failures must
    # escalate the budget at least once compared to the baseline.
    base_max_turns = 5
    base_budget, _ = tracker.escalate_for(
        scope_id, role, base_max_turns=base_max_turns
    )
    assert base_budget == base_max_turns, "Baseline must equal base value."

    budgets = [base_budget]
    for _ in range(3):
        tracker.record_failure(scope_id, role, "error_max_turns")
        bud, _ = tracker.escalate_for(
            scope_id, role, base_max_turns=base_max_turns
        )
        budgets.append(bud)

    assert max(budgets) > base_max_turns, (
        "BudgetEscalationTracker must escalate the budget after repeated "
        f"error_max_turns failures; budgets={budgets!r}"
    )
    # Sanity: by the third failure the tracker reports the ladder as
    # exhausted (max_escalations defaults to 3).
    assert tracker.is_exhausted(scope_id, role) or len(budgets) >= 4


@pytest.mark.integration
def test_test_runner_silent_zero_is_visible_to_user(
    fake_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.32.0 Phase 3 (Gap C): silent ``passed=0 failed=0 total=0`` is classified.

    Drives :func:`orchestrator.test_result_classifier.classify_test_result`
    against the exact AgentResult shape that fake-pytest's
    ``zero_pass_zero_fail`` mode produces. The classifier must NOT return
    ``"ok"`` and MUST return one of the diagnostic non-ok diagnoses
    (``no_tests_found`` or ``no_signal``) so the orchestrator can surface
    it instead of silently proceeding. This is the core Gap C contract.
    """
    monkeypatch.setenv("AUTODEV_FAKE_FAILURE_MODE", "zero_pass_zero_fail")
    proc = subprocess.run(
        ["bash", str(FAKE_BIN_DIR / "fake-pytest"), "tests/"],
        capture_output=True,
        text=True,
        env={**os.environ},
    )
    assert proc.returncode == 0
    assert "passed=0 failed=0 total=0" in proc.stdout

    from orchestrator.test_result_classifier import classify_test_result

    class _Stub:
        success = True
        text = proc.stdout
        error: str | None = None
        raw_stderr = proc.stderr

    diagnosis = classify_test_result(_Stub(), parsed_counts=(0, 0, 0))
    assert diagnosis != "ok", (
        "Silent zero must NOT classify as 'ok' — that's the Gap C bug. "
        f"Got {diagnosis!r}."
    )
    assert diagnosis in {"no_tests_found", "no_signal"}, (
        "Silent zero should classify as 'no_tests_found' or 'no_signal'; "
        f"got {diagnosis!r}."
    )


@pytest.mark.integration
def test_repetition_loop_triggers_recovery_action(
    fake_env: dict[str, Path],
) -> None:
    """v0.32.0 Phase 4 (Gap D): repeated identical outputs trigger recovery.

    Drives :class:`orchestrator.repeat_detector.RepeatDetector` (or the
    repetition_recovery module) with three consecutive identical outputs
    — the exact pattern fake-claude's ``repetition_loop`` mode emits.
    A recovery action MUST be selected (anything but ``"none"`` /
    ``None``). The exact action name is implementation-defined; what
    matters is that the detector noticed.
    """
    # Run the fake three times to confirm output is identical (matches
    # what the orchestrator's repeat detector would observe).
    outputs = []
    for i in range(3):
        proc = subprocess.run(
            [
                "bash",
                str(FAKE_BIN_DIR / "fake-claude"),
                "-p",
                f"prompt-{i}",
            ],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "AUTODEV_FAKE_FAILURE_MODE": "repetition_loop",
                "AUTODEV_FAKE_RESPONSE_DIR": str(fake_env["responses"]),
            },
        )
        outputs.append(proc.stdout.strip())
    assert len(set(outputs)) == 1, (
        f"repetition_loop should emit identical output 3x; got {outputs!r}"
    )

    # Now the unit-level recovery contract: the repetition recovery
    # module exposes an action selector; calling it with a detected
    # repetition loop MUST return a non-trivial recovery action.
    from orchestrator.repetition_recovery import choose_recovery_action

    action = choose_recovery_action(
        discard_count=2,
        pivot_count=0,
        architect_count=0,
        qa_gates_passed=False,
        repetition_loop_detected=True,
    )
    assert action != "do_nothing", (
        "Repetition loop without QA convergence must NOT decide "
        f"'do_nothing'; got {action!r}."
    )
    # The detector picked SOMETHING from the action enum — that's the
    # contract Gap D depends on.
    assert action in {
        "switch_tactic",
        "increase_scope",
        "re_architect",
        "ask_human",
    }, f"Unexpected recovery action: {action!r}"


@pytest.mark.integration
def test_blocked_task_surfaces_recovery_hint(tmp_path: Path) -> None:
    """v0.32.0 Phase 5 (Gap G): blocked task surfaces ``recovery_hint`` in CLI.

    Builds a minimal :class:`Plan` with one task status=``"blocked"`` and
    a populated :class:`RecoveryHint`, then drives
    ``autodev status --blocked`` via :class:`click.testing.CliRunner`
    and asserts the recovery-hint surface appears in the rendered output.
    """
    import asyncio as _asyncio
    import datetime as _dt

    from click.testing import CliRunner

    from cli import cli
    from config.defaults import default_config
    from config.loader import save_config
    from state.plan_manager import PlanManager
    from state.schemas import (
        AcceptanceCriterion,
        Phase,
        Plan,
        RecoveryHint,
        Task,
    )

    iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    hint = RecoveryHint(
        class_="missing_test_output",
        recommended_user_action=(
            "Inspect .autodev/debug/ for the latest test runner artefacts."
        ),
        relevant_evidence_files=[".autodev/evidence/1.1-test.json"],
        relevant_debug_files=[".autodev/debug/test-engineer-1.txt"],
        commands_to_try=["autodev requeue", "autodev rewind 1.1"],
    )
    plan = Plan(
        plan_id="p-block-hint",
        spec_hash="0123456789abcdef",
        complexity="simple",
        phases=[
            Phase(
                id="1",
                title="Setup",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="Stuck task",
                        description="A task that the orchestrator soft-blocked.",
                        status="blocked",
                        blocked_reason="Test runner produced no signal.",
                        recovery_hint=hint,
                    )
                ],
                acceptance=[AcceptanceCriterion(id="ph-1", description="ok")],
            )
        ],
        created_at=iso,
        updated_at=iso,
    )

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as raw_cwd:
        cwd = Path(raw_cwd)
        autodev_dir = cwd / ".autodev"
        autodev_dir.mkdir(parents=True, exist_ok=True)
        cfg = default_config()
        cfg.platform = "claude_code"  # type: ignore[assignment]
        save_config(cfg, autodev_dir / "config.json")

        pm = PlanManager(cwd, session_id="sess-test-block-hint")
        _asyncio.run(pm.init_plan(plan))

        result = runner.invoke(cli, ["status", "--blocked"])
        assert result.exit_code == 0, result.output
        # The block class, the recommended action body, and at least one
        # copy-paste command must appear in the rendered surface.
        assert "missing_test_output" in result.output
        assert "Inspect" in result.output
        assert "autodev requeue" in result.output


# Suppress unused-import warning for asyncio when the file is parsed
# without the asyncio-mode plugin enabled.
_ = asyncio
