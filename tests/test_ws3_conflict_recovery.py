"""WS3 — recover an already-VALIDATED patch on ANY terminal block.

Root cause (the discarded-work class): when a task's already-VALIDATED patch
(a genuine, non-soft-passed reviewer ``APPROVED`` + a converged, judge-ranked
tournament winner) fails to land on ``main``, ``block_task`` discards the whole
validated result. The original WS3 recovered ONLY when the terminal block was
one of the three merge-conflict-exhaustion classes; the forensic finding
(slice3, django-10914 / flask-4992) is that the SAME reviewer-APPROVED,
sometimes gold-identical patch is silently discarded when a task reaches a
terminal ``blocked`` state for a NON-conflict reason — ``test_diagnosis_hardfail``,
a turn-budget/``guardrail_exceeded`` overrun, a ``worker_exception``. The
discard is infrastructural, not a semantic verdict — exactly the "don't discard
a validated result over an unrelated mechanical failure" gap Tier J's
``_maybe_accept_approved_on_exhaustion`` already solves for turn-exhaustion.

Fix (two parts):
  * (3a) WIDEN the trigger — recovery is attempted on ANY terminal block,
    gated NOT by failure class but by the presence of a GENUINE
    (non-soft-passed) reviewer-``APPROVED`` verdict AND a non-empty validated
    diff resolvable from the source ladder (below). The genuine APPROVED review
    + the unforced clean-apply-vs-live-``main`` check ARE the safety; the failure
    class no longer restricts it. (A ``REVIEW_REJECTED`` / malformed block has no
    genuine APPROVED verdict, so it correctly won't recover.) A converged
    tournament winner is the PREFERRED diff source when present, but is NOT
    required — the real discarded cases (django-10914 / flask-4992) are
    single-candidate with NO tournament evidence at all, so requiring a converged
    winner would re-exclude exactly the cases this recovery exists for.
  * (3b) ADD an ``evidence/{task}.patch`` fallback diff source, used when
    ``tournament.final_diff`` / ``developer.json.diff`` are empty/absent — the
    compounding failure: ``error_max_turns`` truncates the developer return and
    empties the JSON diffs, so the APPROVED fix survives ONLY as the durable
    ``evidence/{task}.patch``.

The recovery helper ``_maybe_recover_validated_patch_on_conflict_exhaustion``
(name retained for continuity with its out-of-tree references; it is no longer
conflict-only) is hooked into ``block_task`` (the sole enforced ``blocked``
committer) immediately before its final blocked commit. On a genuinely-validated
task it attempts ONE clean, UNFORCED apply of the recovered patch against the
*current* ``main`` HEAD (the apply succeeding or failing IS the safety check —
nothing is forced). On success it walks the same FSM chain Tier J uses to reach
``complete`` (via the shared ``_walk_task_to_complete``), stamped with
distinguishing metadata (``resolver_action: "conflict_fallback_recovered"`` +
``needs_human_review``) and a distinctly-named ledger op carrying
``needs_verification=True`` so nothing downstream mistakes it for a normal
test-verified clean pass.

Multiple siblings with independently-validated patches are handled "for free":
each task's own ``block_task`` call re-checks against LIVE ``main`` at its own
decision point, so a genuinely-conflicting sibling correctly still ends up
``blocked`` while an independently-appliable one recovers.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path
from typing import Any

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator import execute_phase as ep
from orchestrator import failure_classes as fc
from orchestrator.blocker_guard import block_task
from state import ledger as ledger_mod
from state.evidence import write_evidence, write_patch
from state.schemas import (
    AcceptanceCriterion,
    CoderEvidence,
    Phase,
    Plan,
    ReviewEvidence,
    Task,
    TestEvidence,
    TournamentEvidence,
)

from stub_adapter import StubAdapter


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True
    )


def _git_init(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    for name, content in files.items():
        (repo / name).write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")


def _commit_count(repo: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    return int(out.stdout.strip())


def _head_file(repo: Path, path: str) -> str:
    out = subprocess.run(
        ["git", "show", f"HEAD:{path}"], cwd=str(repo), capture_output=True, text=True
    )
    return out.stdout


def _make_appliable_diff(repo: Path, path: str, new_content: str) -> str:
    """A REAL unified diff (relative to HEAD) that ``git apply`` accepts.

    Materialise the change in the working tree, capture ``git diff``, then
    restore the tree so the diff is a genuine, appliable patch against HEAD.
    """
    target = repo / path
    original = target.read_text()
    target.write_text(new_content)
    out = subprocess.run(
        ["git", "diff", "--no-color", "--", path],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )
    target.write_text(original)
    assert out.stdout.strip(), "expected a non-empty diff"
    return out.stdout


def _mk_task(tid: str, files: list[str], title: str = "task") -> Task:
    return Task(
        id=tid,
        phase_id="1",
        title=title,
        description="do the work",
        files=files,
        acceptance=[AcceptanceCriterion(id=f"ac-{tid}", description="works")],
    )


def _mk_plan(tasks: list[Task]) -> Plan:
    return Plan(
        plan_id="p-ws3",
        spec_hash="deadbeef",
        phases=[Phase(id="1", title="Build", tasks=tasks)],
        created_at=_iso(),
        updated_at=_iso(),
    )


async def _mk_orch(repo: Path, tasks: list[Task]) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.tournaments.review_tournament_enabled = False
    cfg.qa_retry_min_interval_s = 0.0
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=repo,
        cfg=cfg,
        adapter=StubAdapter({}),
        registry=registry,
        session_id="ws3-sess",
    )
    await orch.plan_manager.init_plan(_mk_plan(tasks))
    return orch


async def _advance_to_tournamented(orch: Orchestrator, tid: str) -> None:
    for st in ("in_progress", "coded", "auto_gated", "reviewed", "tested", "tournamented"):
        await orch.plan_manager.update_task_status(tid, st)


async def _seed_review(
    orch: Orchestrator,
    tid: str,
    *,
    verdict: str = "APPROVED",
    soft_passed: bool | None = None,
) -> None:
    await write_evidence(
        orch.cwd,
        tid,
        ReviewEvidence(task_id=tid, verdict=verdict, soft_passed=soft_passed),  # type: ignore[arg-type]
    )


async def _seed_tournament(
    orch: Orchestrator, tid: str, *, final_diff: str | None, converged: bool = True
) -> None:
    await write_evidence(
        orch.cwd,
        tid,
        TournamentEvidence(
            task_id=tid,
            tournament_id=f"t-{tid}",
            phase="impl",
            passes=1,
            winner="A",
            converged=converged,
            final_diff=final_diff,
        ),
    )


async def _seed_developer(orch: Orchestrator, tid: str, *, diff: str | None) -> None:
    await write_evidence(orch.cwd, tid, CoderEvidence(task_id=tid, diff=diff))


async def _seed_patch(orch: Orchestrator, tid: str, *, diff: str) -> None:
    """Persist the winning patch to ``evidence/{tid}.patch`` (the durable copy
    that survives when ``error_max_turns`` truncates the developer/tournament
    JSON diffs — the WS3 (3b) fallback diff source)."""
    await write_patch(orch.cwd, tid, diff)


async def _seed_test(orch: Orchestrator, tid: str, *, output_text: str) -> None:
    """Persist a ``TestEvidence`` carrying the test_engineer's report text (the
    ``BUGS FOUND:`` channel WS-4's ``reports_missing_change`` reads)."""
    await write_evidence(orch.cwd, tid, TestEvidence(task_id=tid, output_text=output_text))


async def _get(orch: Orchestrator, tid: str) -> Task:
    t = await orch.plan_manager.get_task(tid)
    assert t is not None
    return t


def _ops(repo: Path) -> list[str]:
    return [e.op for e in ledger_mod.read_entries(repo)]


_RECOVER_OP = "recovered_validated_patch_on_conflict_exhaustion"


async def _blocked_task_with_recoverable_evidence(
    repo: Path,
    *,
    tid: str = "1.1",
    fname: str = "widget.py",
    converged: bool = True,
    verdict: str = "APPROVED",
    soft_passed: bool | None = None,
    use_tournament: bool = True,
) -> tuple[Orchestrator, Task, str]:
    """Common setup: real git repo, one task advanced to ``tournamented`` with a
    genuine APPROVED review + a converged tournament winner diff that applies
    cleanly to ``main``. Returns (orch, task, appliable_diff)."""
    _git_init(repo, {fname: "x = 1\n"})
    orch = await _mk_orch(repo, [_mk_task(tid, [fname])])
    diff = _make_appliable_diff(repo, fname, "x = 1\nx = 2\n")
    await _seed_review(orch, tid, verdict=verdict, soft_passed=soft_passed)
    if use_tournament:
        await _seed_tournament(orch, tid, final_diff=diff, converged=converged)
    else:
        await _seed_developer(orch, tid, diff=diff)
    await _advance_to_tournamented(orch, tid)
    return orch, await _get(orch, tid), diff


# ===========================================================================
# 1. Failure-class scope: recovery fires on ANY terminal block EXCEPT
#    TESTS_FAILED (the one deny — a demonstrated ran-and-failed correctness
#    signal). The conflict-exhaustion classes are no longer the gate.
# ===========================================================================

CONFLICT_CLASSES = (
    fc.CONFLICT_3WAY_FAILED,
    fc.CONFLICT_ABANDON,
    fc.CONFLICT_REWRITE_CAP_EXCEEDED,
)


def test_conflict_exhaustion_failure_classes_are_exactly_the_three() -> None:
    """Anti-drift guard on the taxonomy grouping. As of WS3's widening this set
    is NO LONGER the recovery gate (recovery is gated on the validation signals,
    not the failure class); it is retained purely as the named grouping of the
    three merge-conflict-exhaustion classes that terminate the conflict cascade.
    Its membership must not silently drift."""
    assert fc.CONFLICT_EXHAUSTION_FAILURE_CLASSES == frozenset(CONFLICT_CLASSES)


async def test_recovers_for_each_conflict_class(tmp_path: Path) -> None:
    for i, cls in enumerate(CONFLICT_CLASSES):
        repo = tmp_path / f"repo{i}"
        orch, task, _ = await _blocked_task_with_recoverable_evidence(repo)
        recovered = await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
            orch, task, failure_class=cls
        )
        assert recovered is not None, cls
        assert recovered.status == "complete", cls


async def test_widened_trigger_recovers_on_non_conflict_classes(
    tmp_path: Path,
) -> None:
    """WS3 (3a) widening: a NON-conflict terminal block (with a genuinely
    recoverable, validated patch) now recovers — the trigger is gated on the
    validation signals, not the failure class. These are the exact forensic
    classes (``test_diagnosis_hardfail`` / turn-budget→``guardrail_exceeded`` /
    ``worker_exception``) that silently dropped reviewer-APPROVED, sometimes
    gold-identical fixes. (``TESTS_FAILED`` is deliberately NOT here — a
    demonstrated test failure is a correctness signal recovery must not
    override; see ``test_tests_failed_block_is_not_recovered``.)"""
    for i, cls in enumerate(
        (
            fc.TEST_DIAGNOSIS_HARDFAIL,
            fc.GUARDRAIL_EXCEEDED,
            fc.WORKER_EXCEPTION,
        )
    ):
        repo = tmp_path / f"repo{i}"
        orch, task, _ = await _blocked_task_with_recoverable_evidence(repo)
        recovered = await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
            orch, task, failure_class=cls
        )
        assert recovered is not None, cls
        assert recovered.status == "complete", cls


async def test_tests_failed_block_is_not_recovered(tmp_path: Path) -> None:
    """The ONE principled deny. A genuine reviewer-APPROVED, cleanly-appliable
    patch that blocked on ``TESTS_FAILED`` — the fix's OWN tests actually RAN and
    at least one FAILED (a demonstrated correctness signal, diagnosis=="ok") —
    must NOT recover. Tests are the arbiter: a static ``APPROVED`` cannot override
    a real test failure and ship failing work as "recovered" in an unattended
    run, not even behind a ``needs_verification`` flag. Every OTHER terminal block
    recovers; this one stays ``blocked``."""
    repo = tmp_path / "repo"
    orch, task, _ = await _blocked_task_with_recoverable_evidence(repo)
    # Hook-level: the exclusion denies recovery outright (no-op).
    recovered = await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
        orch, task, failure_class=fc.TESTS_FAILED
    )
    assert recovered is None
    assert (await _get(orch, "1.1")).status == "tournamented"
    # End-to-end through the real chokepoint: it commits the terminal block.
    result = await block_task(
        orch,
        await _get(orch, "1.1"),
        failure_class=fc.TESTS_FAILED,
        raw_error="tests_failed: 1 failed",
        meta={"blocked_reason": "tests_failed: 1 failed"},
    )
    assert result.status == "blocked"
    assert _RECOVER_OP not in _ops(repo)


async def test_relabeled_missing_change_block_is_not_recovered(
    tmp_path: Path,
) -> None:
    """Composition safety (WS-4 ↔ WS-3): the ``TESTS_FAILED`` label is NOT
    preserved on every escalation rung to ``block_task`` — a later critic
    ``SOFT_BLOCKER`` or a developer turn-exhaustion ``GUARDRAIL_EXCEEDED``
    RELABELS the class. Keying the deny only on the class label would let a task
    WS-4 meant to block for a MISSING CHANGE be WS-3-recovered after such a
    relabel — landing an incomplete/missing-change diff on ``main``. So the deny
    is ALSO tied to the SIGNAL: the persisted ``TestEvidence`` ``BUGS FOUND:``
    missing-change report makes recovery DECLINE regardless of the (relabeled)
    class."""
    repo = tmp_path / "repo"
    orch, task, _ = await _blocked_task_with_recoverable_evidence(repo)
    # WS-4 missing-change signal on the task's durable TestEvidence, but the
    # block arrives under a RELABELED class (NOT TESTS_FAILED).
    await _seed_test(
        orch,
        "1.1",
        output_text=(
            "RESULTS:\n1 passed\n\n"
            "BUGS FOUND:\nThe expected fix — the source change is missing from "
            "this diff."
        ),
    )
    # Hook-level: the signal-deny holds under either relabel class.
    for cls in (fc.GUARDRAIL_EXCEEDED, fc.SOFT_BLOCKER):
        recovered = await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
            orch, task, failure_class=cls
        )
        assert recovered is None, cls
    assert (await _get(orch, "1.1")).status == "tournamented"
    # End-to-end through the real chokepoint under the relabeled class: it
    # commits the terminal block (the missing-change signal is honored).
    result = await block_task(
        orch,
        await _get(orch, "1.1"),
        failure_class=fc.SOFT_BLOCKER,
        raw_error="soft_blocker (relabeled from a missing-change TESTS_FAILED)",
        meta={"blocked_reason": "soft_blocker"},
    )
    assert result.status == "blocked"
    assert _RECOVER_OP not in _ops(repo)


async def test_recovers_when_test_evidence_reports_no_missing_change(
    tmp_path: Path,
) -> None:
    """Guardrail on the signal-deny: a benign ``TestEvidence`` (no ``BUGS FOUND:``
    missing-change signal) on a normal recoverable block (``guardrail_exceeded``)
    must STILL recover — the deny keys on a demonstrated missing change, not on
    the mere presence of test evidence."""
    repo = tmp_path / "repo"
    orch, task, _ = await _blocked_task_with_recoverable_evidence(repo)
    await _seed_test(
        orch, "1.1", output_text="RESULTS:\n1 passed\n\nBUGS FOUND:\nnone"
    )
    recovered = await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
        orch, task, failure_class=fc.GUARDRAIL_EXCEEDED
    )
    assert recovered is not None
    assert recovered.status == "complete"


# ===========================================================================
# 2. Validation gate: genuine APPROVED review only
# ===========================================================================


async def test_needs_changes_verdict_does_not_recover(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    orch, task, _ = await _blocked_task_with_recoverable_evidence(
        repo, verdict="NEEDS_CHANGES"
    )
    recovered = await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
        orch, task, failure_class=fc.CONFLICT_3WAY_FAILED
    )
    assert recovered is None
    assert (await _get(orch, "1.1")).status == "tournamented"


async def test_softpass_approved_refused(tmp_path: Path) -> None:
    """An INFRA soft-pass APPROVED (``soft_passed=True``) is NOT a genuine
    reviewer verdict — recovering on it would certify an unreviewed diff (R7)."""
    repo = tmp_path / "repo"
    orch, task, _ = await _blocked_task_with_recoverable_evidence(
        repo, soft_passed=True
    )
    recovered = await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
        orch, task, failure_class=fc.CONFLICT_3WAY_FAILED
    )
    assert recovered is None


async def test_no_review_evidence_does_not_recover(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _git_init(repo, {"widget.py": "x = 1\n"})
    orch = await _mk_orch(repo, [_mk_task("1.1", ["widget.py"])])
    diff = _make_appliable_diff(repo, "widget.py", "x = 1\nx = 2\n")
    await _seed_tournament(orch, "1.1", final_diff=diff)  # tournament but NO review
    await _advance_to_tournamented(orch, "1.1")
    recovered = await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
        orch, await _get(orch, "1.1"), failure_class=fc.CONFLICT_3WAY_FAILED
    )
    assert recovered is None


# ===========================================================================
# 3. Winning-diff source ladder (3b): APPROVED verdict + a non-empty validated
#    diff resolvable from tournament.final_diff (a CONVERGED winner, PREFERRED
#    when present) → developer.diff → evidence/{task}.patch. A converged
#    tournament is PREFERRED but NOT required — the real discarded cases
#    (django-10914 / flask-4992) are single-candidate with NO tournament at all.
# ===========================================================================


async def test_recovers_from_developer_patch_when_no_tournament(
    tmp_path: Path,
) -> None:
    """The single-candidate shape (django-10914 / flask-4992): tournament OFF /
    absent, so the validated developer diff (CoderEvidence) IS the winning
    artifact, gated by the same genuine APPROVED review. Requiring a converged
    tournament here would re-exclude exactly this case — so a converged
    tournament must be PREFERRED, never REQUIRED."""
    repo = tmp_path / "repo"
    orch, task, _ = await _blocked_task_with_recoverable_evidence(
        repo, use_tournament=False
    )
    recovered = await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
        orch, task, failure_class=fc.CONFLICT_ABANDON
    )
    assert recovered is not None
    assert recovered.status == "complete"


async def test_recovers_from_developer_patch_when_tournament_final_diff_empty(
    tmp_path: Path,
) -> None:
    """Ladder tier 2: a converged tournament winner is on record (PREFERRED) but
    its ``final_diff`` was emptied by ``error_max_turns`` truncation. The ladder
    falls through to the validated developer patch (CoderEvidence), which
    carries the same winning diff → recovery lands it."""
    repo = tmp_path / "repo"
    _git_init(repo, {"widget.py": "x = 1\n"})
    orch = await _mk_orch(repo, [_mk_task("1.1", ["widget.py"])])
    diff = _make_appliable_diff(repo, "widget.py", "x = 1\nx = 2\n")
    await _seed_review(orch, "1.1")
    await _seed_tournament(orch, "1.1", final_diff="", converged=True)  # truncated
    await _seed_developer(orch, "1.1", diff=diff)
    await _advance_to_tournamented(orch, "1.1")
    baseline = _commit_count(repo)
    recovered = await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
        orch, await _get(orch, "1.1"), failure_class=fc.CONFLICT_3WAY_FAILED
    )
    assert recovered is not None
    assert recovered.status == "complete"
    assert _commit_count(repo) == baseline + 1
    assert "x = 2" in _head_file(repo, "widget.py")


async def test_recovers_from_evidence_patch_when_no_tournament(
    tmp_path: Path,
) -> None:
    """WS3 (3b) — the single-candidate compounding case (django-10914 shape).
    NO tournament evidence at all and the developer diff was emptied by
    ``error_max_turns`` truncation; the APPROVED fix survives ONLY as
    ``evidence/{task}.patch`` — the fallback source must find and land it."""
    repo = tmp_path / "repo"
    _git_init(repo, {"widget.py": "x = 1\n"})
    orch = await _mk_orch(repo, [_mk_task("1.1", ["widget.py"])])
    diff = _make_appliable_diff(repo, "widget.py", "x = 1\nx = 2\n")
    await _seed_review(orch, "1.1")  # genuine APPROVED, NO tournament seeded
    await _seed_developer(orch, "1.1", diff=None)  # truncated
    await _seed_patch(orch, "1.1", diff=diff)  # durable survivor
    await _advance_to_tournamented(orch, "1.1")
    baseline = _commit_count(repo)
    recovered = await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
        orch, await _get(orch, "1.1"), failure_class=fc.CONFLICT_3WAY_FAILED
    )
    assert recovered is not None
    assert recovered.status == "complete"
    # The ONLY source with the diff was evidence/{task}.patch → recovery used it.
    assert _commit_count(repo) == baseline + 1
    assert "x = 2" in _head_file(repo, "widget.py")


async def test_non_converged_tournament_is_not_used_as_a_source(
    tmp_path: Path,
) -> None:
    """A non-converged tournament is NOT a judge-ranked winner, so it is not used
    as a diff SOURCE — with no other appliable source the task stays unrecovered.
    (Convergence still governs the TOURNAMENT source; it just no longer gates
    recovery overall.)"""
    repo = tmp_path / "repo"
    _git_init(repo, {"widget.py": "x = 1\n"})
    orch = await _mk_orch(repo, [_mk_task("1.1", ["widget.py"])])
    diff = _make_appliable_diff(repo, "widget.py", "x = 1\nx = 2\n")
    await _seed_review(orch, "1.1")
    await _seed_tournament(orch, "1.1", final_diff=diff, converged=False)
    await _advance_to_tournamented(orch, "1.1")
    recovered = await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
        orch, await _get(orch, "1.1"), failure_class=fc.CONFLICT_3WAY_FAILED
    )
    assert recovered is None


async def test_no_appliable_diff_does_not_recover(tmp_path: Path) -> None:
    """Genuine APPROVED but nothing to apply — no diff resolvable from ANY ladder
    source (empty/absent tournament, developer, and patch) → no-op."""
    repo = tmp_path / "repo"
    _git_init(repo, {"widget.py": "x = 1\n"})
    orch = await _mk_orch(repo, [_mk_task("1.1", ["widget.py"])])
    await _seed_review(orch, "1.1")
    await _seed_tournament(orch, "1.1", final_diff="   \n")  # whitespace-only
    await _seed_developer(orch, "1.1", diff=None)  # empty
    # No evidence/{task}.patch written → the ladder resolves nothing.
    await _advance_to_tournamented(orch, "1.1")
    recovered = await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
        orch, await _get(orch, "1.1"), failure_class=fc.CONFLICT_3WAY_FAILED
    )
    assert recovered is None


# ===========================================================================
# 4. The unforced apply IS the safety check
# ===========================================================================


async def test_unforced_apply_conflict_does_not_recover(tmp_path: Path) -> None:
    """A validated patch whose winning diff genuinely conflicts with LIVE main
    must NOT recover — the clean, unforced apply fails, so it stays blockable.
    Nothing is forced (no 3-way / rewrite)."""
    repo = tmp_path / "repo"
    _git_init(repo, {"widget.py": "x = 1\n"})
    orch = await _mk_orch(repo, [_mk_task("1.1", ["widget.py"])])
    diff = _make_appliable_diff(repo, "widget.py", "x = 1\nx = 2\n")
    await _seed_review(orch, "1.1")
    await _seed_tournament(orch, "1.1", final_diff=diff)
    await _advance_to_tournamented(orch, "1.1")
    # Advance main so the recovered diff's hunk context no longer matches:
    # a GENUINE conflict on the same line the diff touches.
    (repo / "widget.py").write_text("x = 999\n")
    _git(repo, "commit", "-aqm", "main advanced")
    baseline = _commit_count(repo)

    recovered = await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
        orch, await _get(orch, "1.1"), failure_class=fc.CONFLICT_3WAY_FAILED
    )
    assert recovered is None
    assert _commit_count(repo) == baseline  # nothing landed on main
    assert _RECOVER_OP not in _ops(repo)


# ===========================================================================
# 5. Distinguishing markers + real apply on recovery
# ===========================================================================


async def test_recovery_stamps_markers_and_lands_diff(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    orch, task, _ = await _blocked_task_with_recoverable_evidence(repo)
    baseline = _commit_count(repo)

    recovered = await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
        orch, task, failure_class=fc.CONFLICT_3WAY_FAILED
    )
    assert recovered is not None
    assert recovered.status == "complete"
    # Distinguishing markers so nothing mistakes this for a normal clean pass.
    assert recovered.metadata.get("resolver_action") == "conflict_fallback_recovered"
    assert recovered.metadata.get("needs_human_review") is True
    # Distinctly-named ledger op.
    assert _RECOVER_OP in _ops(repo)
    # The validated patch ACTUALLY landed on main (real git apply + commit).
    assert _commit_count(repo) == baseline + 1
    assert "x = 2" in _head_file(repo, "widget.py")


# ===========================================================================
# 6. Two independently-validated siblings: one recovers, one stays blocked
# ===========================================================================


async def test_two_siblings_one_recovers_one_blocks(tmp_path: Path) -> None:
    """The core WS3 scenario. Two sibling tasks each hold an independently-
    validated patch. Sibling A's patch applies cleanly to live main → recovers.
    Sibling B's patch genuinely conflicts with live main → stays ``blocked``.
    Driven through ``block_task`` (the real chokepoint) at conflict-exhaustion.
    No global ranking/selection — each re-checks live main at its own point."""
    repo = tmp_path / "repo"
    _git_init(repo, {"a.py": "a = 1\n", "b.py": "b = 1\n"})
    orch = await _mk_orch(
        repo, [_mk_task("1.1", ["a.py"], "A"), _mk_task("1.2", ["b.py"], "B")]
    )
    diff_a = _make_appliable_diff(repo, "a.py", "a = 1\na = 2\n")
    diff_b = _make_appliable_diff(repo, "b.py", "b = 1\nb = 2\n")
    await _seed_review(orch, "1.1")
    await _seed_tournament(orch, "1.1", final_diff=diff_a)
    await _seed_review(orch, "1.2")
    await _seed_tournament(orch, "1.2", final_diff=diff_b)
    await _advance_to_tournamented(orch, "1.1")
    await _advance_to_tournamented(orch, "1.2")

    # Make sibling B genuinely conflict: mutate its file on live main so B's
    # winning diff no longer applies. Sibling A's file is untouched.
    (repo / "b.py").write_text("b = 999\n")
    _git(repo, "commit", "-aqm", "b.py diverged on main")

    task_a = await block_task(
        orch,
        await _get(orch, "1.1"),
        failure_class=fc.CONFLICT_3WAY_FAILED,
        raw_error="conflict_escalation:3way_failed",
        meta={"blocked_reason": "conflict_escalation:3way_failed"},
    )
    task_b = await block_task(
        orch,
        await _get(orch, "1.2"),
        failure_class=fc.CONFLICT_3WAY_FAILED,
        raw_error="conflict_escalation:3way_failed",
        meta={"blocked_reason": "conflict_escalation:3way_failed"},
    )

    # A recovered; B genuinely conflicted and stayed blocked.
    assert task_a.status == "complete", task_a.status
    assert task_a.metadata.get("resolver_action") == "conflict_fallback_recovered"
    assert task_b.status == "blocked", task_b.status
    # A's validated work landed on main; B's did not.
    assert "a = 2" in _head_file(repo, "a.py")
    assert _head_file(repo, "b.py") == "b = 999\n"
    # The recovery op fired exactly once (for A only).
    assert _ops(repo).count(_RECOVER_OP) == 1


# ===========================================================================
# 7. block_task wiring: recovery short-circuits the blocked commit
# ===========================================================================


async def test_block_task_recovers_instead_of_blocking(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    orch, task, _ = await _blocked_task_with_recoverable_evidence(repo)
    result = await block_task(
        orch,
        task,
        failure_class=fc.CONFLICT_3WAY_FAILED,
        raw_error="conflict_escalation:3way_failed",
        meta={"blocked_reason": "conflict_escalation:3way_failed"},
    )
    assert result.status == "complete"
    assert result.status != "blocked"
    assert _RECOVER_OP in _ops(repo)


async def test_block_task_recovers_on_non_conflict_class_when_validated(
    tmp_path: Path,
) -> None:
    """WS3 (3a) via the real chokepoint: ``block_task`` on a NON-conflict class
    (``test_diagnosis_hardfail``) with the SAME recoverable evidence now
    short-circuits to ``complete`` instead of blocking — proving the
    ``blocker_guard`` hook is no longer gated to the conflict classes."""
    repo = tmp_path / "repo"
    orch, task, _ = await _blocked_task_with_recoverable_evidence(repo)
    result = await block_task(
        orch,
        task,
        failure_class=fc.TEST_DIAGNOSIS_HARDFAIL,
        raw_error="test_diagnosis: hardfail",
        meta={"blocked_reason": "test_diagnosis: hardfail"},
    )
    assert result.status == "complete"
    assert result.status != "blocked"
    assert _RECOVER_OP in _ops(repo)


async def test_block_task_still_blocks_when_no_genuine_approved(
    tmp_path: Path,
) -> None:
    """The VALIDATION gate — not the failure class — is the safety. A block on
    ANY class WITHOUT a genuine reviewer APPROVED (here NEEDS_CHANGES) is never
    recovered: ``block_task`` commits the terminal ``blocked`` transition."""
    repo = tmp_path / "repo"
    orch, task, _ = await _blocked_task_with_recoverable_evidence(
        repo, verdict="NEEDS_CHANGES"
    )
    result = await block_task(
        orch,
        task,
        failure_class=fc.TEST_DIAGNOSIS_HARDFAIL,
        raw_error="test_diagnosis: hardfail",
        meta={"blocked_reason": "test_diagnosis: hardfail"},
    )
    assert result.status == "blocked"
    assert _RECOVER_OP not in _ops(repo)


# ===========================================================================
# 8. Resume-safety: crash after apply, before ledger flush → no double-apply
# ===========================================================================


async def test_resume_after_apply_no_double_apply(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Mirror Tier J / WS5's apply→ledger→FSM-walk ordering: a crash AFTER the
    unforced apply lands on main but BEFORE the FSM-walk completes must not
    double-apply. On resume the SAME persisted diff no longer applies to the new
    HEAD (it is already there), so the re-run falls through — main keeps exactly
    one commit."""
    repo = tmp_path / "repo"
    orch, task, _ = await _blocked_task_with_recoverable_evidence(repo)
    baseline = _commit_count(repo)

    # Simulate a crash DURING the FSM-walk (i.e. after the apply's git commit).
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("simulated crash after apply, before ledger flush")

    monkeypatch.setattr(ep, "_walk_task_to_complete", _boom)
    crashed = False
    try:
        await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
            orch, task, failure_class=fc.CONFLICT_3WAY_FAILED
        )
    except RuntimeError:
        crashed = True
    assert crashed
    # The apply DID land (durable git commit) but the task is NOT completed.
    assert _commit_count(repo) == baseline + 1
    assert "x = 2" in _head_file(repo, "widget.py")
    assert (await _get(orch, "1.1")).status == "tournamented"

    # Resume: restore the walk and re-run the identical recovery.
    monkeypatch.undo()
    result = await ep._maybe_recover_validated_patch_on_conflict_exhaustion(
        orch, await _get(orch, "1.1"), failure_class=fc.CONFLICT_3WAY_FAILED
    )
    # No double-apply: the diff is already in HEAD → the unforced apply fails →
    # fall through. Main STILL has exactly one recovery commit.
    assert result is None
    assert _commit_count(repo) == baseline + 1


# ===========================================================================
# 9. WS3 widening — end-to-end through block_task on a NON-conflict block, with
#    the winning diff surviving ONLY in evidence/{task}.patch (the exact
#    forensic class: django-10914 / flask-4992 lost the gold fix this way).
# ===========================================================================


async def test_widened_recovery_from_evidence_patch_on_test_diagnosis_hardfail(
    tmp_path: Path,
) -> None:
    """The headline WS3 scenario, faithful to the REAL forensic shape
    (django-10914 task 1.1 / flask-4992 task 1.c3): a SINGLE-CANDIDATE,
    reviewer-APPROVED task with NO tournament evidence at all, reaching a
    terminal ``blocked`` for a NON-conflict reason (``test_diagnosis_hardfail``),
    whose winning diff was truncated out of the developer JSON so it survives
    ONLY as ``evidence/{task}.patch``. Driven through the real ``block_task``
    chokepoint, it must RECOVER to ``complete`` (needs-verification markers + the
    distinct recovery ledger op) and the validated patch must reach ``main`` —
    instead of being silently discarded. (No tournament is fabricated: a
    converged winner must NOT be a precondition for recovery.)"""
    repo = tmp_path / "repo"
    _git_init(repo, {"widget.py": "x = 1\n"})
    orch = await _mk_orch(repo, [_mk_task("1.1", ["widget.py"])])
    diff = _make_appliable_diff(repo, "widget.py", "x = 1\nx = 2\n")
    await _seed_review(orch, "1.1")  # genuine APPROVED, NO tournament seeded
    await _seed_patch(orch, "1.1", diff=diff)  # ONLY surviving copy
    await _advance_to_tournamented(orch, "1.1")
    baseline = _commit_count(repo)

    result = await block_task(
        orch,
        await _get(orch, "1.1"),
        failure_class=fc.TEST_DIAGNOSIS_HARDFAIL,
        raw_error="test_diagnosis: hardfail",
        meta={"blocked_reason": "test_diagnosis: hardfail"},
    )

    assert result.status == "complete"
    assert result.status != "blocked"
    # Needs-verification markers so nothing mistakes this for a clean pass.
    assert result.metadata.get("resolver_action") == "conflict_fallback_recovered"
    assert result.metadata.get("needs_human_review") is True
    # Distinct recovery ledger op, carrying needs_verification=True.
    assert _RECOVER_OP in _ops(repo)
    rec = next(e for e in ledger_mod.read_entries(repo) if e.op == _RECOVER_OP)
    assert rec.payload.get("needs_verification") is True
    assert rec.payload.get("failure_class") == fc.TEST_DIAGNOSIS_HARDFAIL
    # The validated patch actually landed on main (real git apply + commit).
    assert _commit_count(repo) == baseline + 1
    assert "x = 2" in _head_file(repo, "widget.py")


async def test_widened_safety_net_unapplyable_patch_stays_blocked(
    tmp_path: Path,
) -> None:
    """The safety net, widened. Same single-candidate non-conflict block (diff
    only in ``evidence/{task}.patch``, no tournament), but the winning patch
    genuinely CONFLICTS with live ``main`` (a sibling landed a colliding
    change). The clean, UNFORCED apply fails — so the task must stay ``blocked``
    (nothing is ever forced in). Proves the widened trigger did not weaken the
    apply-time safety."""
    repo = tmp_path / "repo"
    _git_init(repo, {"widget.py": "x = 1\n"})
    orch = await _mk_orch(repo, [_mk_task("1.1", ["widget.py"])])
    diff = _make_appliable_diff(repo, "widget.py", "x = 1\nx = 2\n")
    await _seed_review(orch, "1.1")  # genuine APPROVED, NO tournament seeded
    await _seed_patch(orch, "1.1", diff=diff)  # only surviving copy
    await _advance_to_tournamented(orch, "1.1")
    # Advance main so the recovered diff's hunk context no longer matches.
    (repo / "widget.py").write_text("x = 999\n")
    _git(repo, "commit", "-aqm", "main advanced")
    baseline = _commit_count(repo)

    result = await block_task(
        orch,
        await _get(orch, "1.1"),
        failure_class=fc.GUARDRAIL_EXCEEDED,
        raw_error="guardrail: turn budget exhausted",
        meta={"blocked_reason": "guardrail: turn budget exhausted"},
    )

    assert result.status == "blocked"
    assert _RECOVER_OP not in _ops(repo)
    assert _commit_count(repo) == baseline  # nothing landed on main
