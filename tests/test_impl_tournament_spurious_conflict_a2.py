"""A2 (reproduce-first) — spurious ``conflict_3way_failed`` on trivial fixes.

Field finding #2: a trivial single-task fix that one candidate implements
cleanly is turned into a *blocking* ``conflict_3way_failed`` (which then
drives ``re_architect`` → corrective task → the WS-4 ledger-wipe chain that
A1 patched downstream). A2 targets the conflict at its source.

These tests are REPRODUCE-FIRST: they drive the *real* apply machinery
(``WorktreeManager.apply_patch_to_main`` via
``execute_phase._apply_with_conflict_escalation``) over real git repos and
worktrees so the question "is the conflict in the multi-candidate meta-merge
or in the apply-to-main of the winning diff?" is answered by evidence, not by
reading. The resolver/LLM slice is fully stubbed.

Mechanism confirmed by these tests (see module-level NOTE below):

* ``conflict_3way_failed`` is emitted from exactly one place —
  ``_apply_with_conflict_escalation`` → ``apply_patch_to_main`` of the
  **per-task developer worktree diff** against main (``execute_phase.py``
  ~2441). The multi-candidate meta-merge
  (``_impl_meta_merge_via_diff_synthesis``) NEVER emits it: every failure
  path there is caught and degraded to ``_fallback_strongest_survivor``.
* A trivial single-candidate fix applied to an unchanged main applies
  cleanly (no conflict) — the baseline that must stay green.
* The spurious case is an *independent* edit (different line) that collides
  only because main advanced and the flat ``git apply`` of the worktree diff
  is brittle. ``apply_patch_to_main`` already escalates plain→``--3way`` via
  the critic; the residual blocker is the ``--3way`` *also* failing on an
  independent-but-adjacent edit.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path
from typing import Any

import pytest

from orchestrator import execute_phase as ep
from orchestrator.worktree import WorktreeManager
from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


# ── git helpers ────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return out.stdout


def _git_init(path: Path) -> None:
    _git(path, "init")
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    # Deterministic: keep default branch name stable across git versions.
    with __import__("contextlib").suppress(subprocess.CalledProcessError):
        _git(path, "checkout", "-b", "main")


def _commit_file(repo: Path, rel: str, content: str, msg: str) -> str:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(content)
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").strip()


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _mk_plan(task: Task) -> Plan:
    return Plan(
        plan_id="p-a2",
        spec_hash="h",
        phases=[Phase(id="1", title="Work", tasks=[task])],
        created_at=_iso(),
        updated_at=_iso(),
    )


# ── orchestrator stub for _apply_with_conflict_escalation ──────────────
#
# _apply_with_conflict_escalation needs: orch.cwd, orch.plan_manager,
# orch.session_id, orch.cfg, orch.adapter, orch.registry, orch.knowledge.
# The critic call goes through ``_escalate_conflict_to_critic`` → delegate
# → orch.adapter.execute. We drive the critic RESOLUTION via the adapter.


def _make_orch(tmp_path: Path, pm: PlanManager, critic_resolution: str) -> Any:
    from adapters.types import AgentResult, AgentSpec
    from config.defaults import default_config

    cfg = default_config()
    cfg.tournaments.execute_max_parallel_tasks = 1
    cfg.tournaments.phase_review.enabled = False

    captured: dict = {"prompts": []}

    class FakeAdapter:
        async def execute(self, inv):
            captured["prompts"].append(getattr(inv, "prompt", ""))
            return AgentResult(
                success=True,
                text=critic_resolution,
                duration_s=0.01,
                files_changed=[],
                diff="",
            )

    class FakeRegistry:
        def get(self, role):
            return AgentSpec(
                name=role,
                model="sonnet",
                prompt="critic prompt",
                description="",
                tools=[],
                max_turns=1,
            )

    class FakeKnowledge:
        async def inject_block(self, role, task_id=None):
            return ""

    class FakeGuard:
        def start_task(self, tid):
            pass

        def end_task(self, tid):
            pass

        def pre_invocation(self, *a, **kw):
            pass

        def post_invocation(self, *a, **kw):
            pass

    class FakeLoop:
        def observe(self, *a, **kw):
            pass

    return type(
        "Orch",
        (),
        {
            "cwd": tmp_path,
            "session_id": "sess-a2",
            "plan_manager": pm,
            "cfg": cfg,
            "guardrails": FakeGuard(),
            "adapter": FakeAdapter(),
            "registry": FakeRegistry(),
            "knowledge": FakeKnowledge(),
            "loop_detector": FakeLoop(),
            "plugin_registry": None,
            "disable_impl_tournament": True,
            "_captured": captured,
        },
    )()


async def _blocked_reason(pm: PlanManager, task_id: str) -> str | None:
    t = await pm.get_task(task_id)
    return t.blocked_reason if t is not None else None


# ── Repro 1: trivial single-candidate fix → clean apply, NO conflict ───


@pytest.mark.asyncio
async def test_trivial_single_fix_applies_clean_no_conflict_3way(
    tmp_path: Path,
) -> None:
    """A trivial fix that one candidate implements cleanly must NOT block.

    Main is unchanged between worktree creation and apply (the common
    single-task case). The per-task worktree diff applies cleanly; the
    critic is never consulted and ``conflict_3way_failed`` is never raised.
    This is the GREEN baseline that the fix must not regress.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    _commit_file(repo, "foo.py", "A\nB\nC\n", "init")

    pm = PlanManager(repo, session_id="sess-a2")
    task = Task(id="1.1", phase_id="1", title="fix B", description="d", files=["foo.py"])
    await pm.init_plan(_mk_plan(task))
    await pm.update_task_status("1.1", "in_progress")

    wt_mgr = WorktreeManager(
        main_repo=repo, tournament_dir=repo / ".autodev" / "execute_worktrees"
    )
    wt = await wt_mgr.create_per_task("1.1")
    # One candidate's trivial fix.
    (wt / "foo.py").write_text("A\nB-fixed\nC\n")

    orch = _make_orch(repo, pm, critic_resolution="RESOLUTION: rebase-and-retry\n")
    applied = await ep._apply_with_conflict_escalation(orch, task, wt, wt_mgr)

    assert applied is True
    # No critic escalation needed → no conflict path taken.
    assert orch._captured["prompts"] == []
    reason = await _blocked_reason(pm, "1.1")
    assert reason is None or "3way_failed" not in reason
    await wt_mgr.cleanup_all()
    # Main got the fix.
    assert "B-fixed" in (repo / "foo.py").read_text()


# ── Repro 2: independent edit, main advanced → spurious conflict ───────


@pytest.mark.asyncio
async def test_independent_edit_with_advanced_main_blocks_conflict_3way(
    tmp_path: Path,
) -> None:
    """RED repro of the field finding.

    The developer makes a *trivial, independent* edit (line ``B``) in the
    per-task worktree. Meanwhile main advances on a *different* line
    (``C``) — exactly the situation when a sibling task committed
    concurrently. The worktree diff was generated against the now-stale
    base, so:

      * plain ``git apply`` fails (hunk context mismatch),
      * the critic says ``rebase-and-retry`` → ``--3way`` apply,
      * the ``--3way`` ALSO fails because the two independent edits fall in
        the same 3-line hunk window,
      * the task hard-blocks with ``conflict_3way_failed``.

    This documents the CONFIRMED emitter (apply-to-main of the per-task
    worktree diff, ``execute_phase.py`` ~2441) — NOT the meta-merge.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    base = _commit_file(repo, "foo.py", "A\nB\nC\n", "init")

    pm = PlanManager(repo, session_id="sess-a2")
    task = Task(id="1.1", phase_id="1", title="fix B", description="d", files=["foo.py"])
    await pm.init_plan(_mk_plan(task))
    await pm.update_task_status("1.1", "in_progress")

    wt_mgr = WorktreeManager(
        main_repo=repo, tournament_dir=repo / ".autodev" / "execute_worktrees"
    )
    wt = await wt_mgr.create_per_task("1.1", base_ref=base)
    # Developer's independent trivial fix on line B.
    (wt / "foo.py").write_text("A\nB-fixed\nC\n")

    # Main advances independently on line C (sibling task committed).
    _commit_file(repo, "foo.py", "A\nB\nC-main\n", "main advances C")

    orch = _make_orch(repo, pm, critic_resolution="RESOLUTION: rebase-and-retry\n")
    applied = await ep._apply_with_conflict_escalation(orch, task, wt, wt_mgr)

    await wt_mgr.cleanup_all()

    # Genuinely-adjacent edits: even a true cherry-pick conflicts, so this
    # MUST remain a loud block (do NOT silently drop main's change). The fix
    # preserves this — auto-3way is tried, fails (genuine conflict), then the
    # critic-escalation path still blocks with conflict_3way_failed.
    assert applied is False
    reason = await _blocked_reason(pm, "1.1")
    assert reason is not None and "3way_failed" in reason


# ── Repro 3: SPURIOUS — plain fails, --3way reconciles, but critic says    ──
#            "abandon" so --3way is never tried → spurious block (RED today) ─


@pytest.mark.asyncio
async def test_reconcilable_diff_not_spuriously_blocked_when_critic_abandons(
    tmp_path: Path,
) -> None:
    """The core spurious-conflict RED repro.

    The developer makes a clean, far-apart edit (line ``B``). Main advances
    on a *distant* line (``J``) but in a way that shifts the hunk's context
    so the *plain* ``git apply`` fails (line-number drift) — yet a ``--3way``
    apply reconciles it cleanly (independent regions, blob-backed merge).

    Today the plain-apply failure routes UNCONDITIONALLY through the critic
    LLM, and ``--3way`` is only attempted if the critic returns
    ``rebase-and-retry``. When the critic instead returns ``abandon-task``
    (a flaky/over-cautious LLM verdict), the trivially-reconcilable diff is
    NEVER tried with ``--3way`` and the task spuriously blocks.

    The fix: attempt ``--3way`` automatically the moment plain apply fails,
    BEFORE consulting the critic. A reconcilable diff applies cleanly with
    no block and no critic call. (Genuine conflicts — where ``--3way`` also
    fails — still fall through to the critic and can still block loud; see
    repro 2.)

    RED today: ``applied is False`` (critic abandoned before --3way).
    GREEN after fix: ``applied is True`` and the critic is never called.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_init(repo)
    base = _commit_file(repo, "foo.py", "L1\nL2\nL3\nL4\nL5\n", "init")

    pm = PlanManager(repo, session_id="sess-a2")
    task = Task(id="1.1", phase_id="1", title="fix L3", description="d", files=["foo.py"])
    await pm.init_plan(_mk_plan(task))
    await pm.update_task_status("1.1", "in_progress")

    wt_mgr = WorktreeManager(
        main_repo=repo, tournament_dir=repo / ".autodev" / "execute_worktrees"
    )
    wt = await wt_mgr.create_per_task("1.1", base_ref=base)
    # Developer's clean edit on line L3.
    (wt / "foo.py").write_text("L1\nL2\nL3-dev\nL4\nL5\n")

    # Main advances on a DIFFERENT line (L1) within the SAME 3-line hunk
    # window. The stale-base diff's context (L1) no longer matches main, so
    # plain `git apply` fails — but `--3way` (which rebuilds the merge base
    # from the diff's blob OIDs) reconciles cleanly: L1-main + L3-dev.
    _commit_file(repo, "foo.py", "L1-main\nL2\nL3\nL4\nL5\n", "main advances L1")

    # Pre-flight assertion: confirm the scenario is genuinely reconcilable
    # (plain fails, --3way would apply) — otherwise the repro is invalid.
    diff_text = await wt_mgr.get_diff_vs_base(wt, base_ref="HEAD")
    import subprocess as _sp

    plain = _sp.run(
        ["git", "apply", "--check", "--whitespace=fix"],
        cwd=str(repo), input=diff_text, capture_output=True, text=True,
    )
    three = _sp.run(
        ["git", "apply", "--check", "--3way", "--whitespace=fix"],
        cwd=str(repo), input=diff_text, capture_output=True, text=True,
    )
    assert plain.returncode != 0, "scenario invalid: plain apply unexpectedly clean"
    assert three.returncode == 0, "scenario invalid: --3way cannot reconcile"

    # Critic would abandon — so today --3way is never reached.
    orch = _make_orch(repo, pm, critic_resolution="RESOLUTION: abandon-task\n")
    applied = await ep._apply_with_conflict_escalation(orch, task, wt, wt_mgr)

    await wt_mgr.cleanup_all()

    # GREEN after fix: auto-3way reconciles BEFORE the critic is consulted.
    assert applied is True
    assert orch._captured["prompts"] == [], "critic should not be consulted"
    reason = await _blocked_reason(pm, "1.1")
    assert reason is None or "3way" not in reason
    # Both independent edits landed (true 3-way merge).
    merged = (repo / "foo.py").read_text()
    assert "L1-main" in merged and "L3-dev" in merged
