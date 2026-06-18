"""Tests for v0.11.0 conflict escalation helpers in execute_phase.

Covers:

* :func:`_parse_conflict_resolution` — directive extraction + parser
  fallback to ``abandon-task`` on malformed input.
* :func:`_escalate_conflict_to_critic` — builds the right envelope and
  forwards the parsed resolution.

The ``_execute_one_worker`` integration with apply_patch_to_main is
exercised in commit 14's tests (test_orchestrator_conflict_escalation_wired.py).
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest

from orchestrator import execute_phase as ep
from orchestrator.execute_phase import (
    ConflictResolution,
    _parse_conflict_resolution,
)
from state.plan_manager import PlanManager
from state.schemas import Phase, Plan, Task


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# _parse_conflict_resolution
# ---------------------------------------------------------------------------


def test_parse_resolution_rebase_and_retry() -> None:
    """Plain rebase-and-retry directive."""
    text = "Some preamble.\n\nRESOLUTION: rebase-and-retry\n"
    out = _parse_conflict_resolution(text)
    assert out.action == "rebase-and-retry"
    assert out.rewrite_guidance == ""


def test_parse_resolution_abandon_task() -> None:
    """Abandon-task directive."""
    text = "Cannot reconcile.\n\nRESOLUTION: abandon-task\n"
    out = _parse_conflict_resolution(text)
    assert out.action == "abandon-task"
    assert out.rewrite_guidance == ""


def test_parse_resolution_rewrite_captures_guidance() -> None:
    """Rewrite directive: lines before the directive are captured as guidance."""
    text = (
        "Re-implement using the lazy-import pattern.\n"
        "Move the yaml import inside the function body.\n"
        "\n"
        "RESOLUTION: rewrite\n"
    )
    out = _parse_conflict_resolution(text)
    assert out.action == "rewrite"
    assert "lazy-import pattern" in out.rewrite_guidance
    assert "yaml" in out.rewrite_guidance


def test_parse_resolution_unparseable_falls_back_to_abandon() -> None:
    """No directive found → defensive default of abandon-task."""
    out = _parse_conflict_resolution("Just some random text without a directive.")
    assert out.action == "abandon-task"


def test_parse_resolution_empty_string_falls_back_to_abandon() -> None:
    """Empty critic response → abandon-task."""
    out = _parse_conflict_resolution("")
    assert out.action == "abandon-task"


def test_parse_resolution_picks_last_directive_when_multiple() -> None:
    """If the response has multiple RESOLUTION: lines, the LAST one wins."""
    text = (
        "Initial thought:\nRESOLUTION: rebase-and-retry\n\n"
        "Actually no:\nRESOLUTION: abandon-task\n"
    )
    out = _parse_conflict_resolution(text)
    assert out.action == "abandon-task"


def test_parse_resolution_directive_must_be_on_own_line() -> None:
    """``RESOLUTION: rewrite`` mid-line is NOT matched (must be anchored)."""
    text = "Some text RESOLUTION: rewrite within a sentence does not count.\n"
    out = _parse_conflict_resolution(text)
    assert out.action == "abandon-task"


# ---------------------------------------------------------------------------
# _escalate_conflict_to_critic
# ---------------------------------------------------------------------------


def _mk_plan(tasks: list[Task]) -> Plan:
    return Plan(
        plan_id="p-conflict",
        spec_hash="cafe",
        phases=[Phase(id="1", title="conflict", tasks=tasks)],
        created_at=_iso(),
        updated_at=_iso(),
    )


def _make_orch(tmp_path: Path, pm: PlanManager) -> Any:
    from config.defaults import default_config

    cfg = default_config()
    cfg.tournaments.execute_max_parallel_tasks = 1
    cfg.tournaments.phase_review.enabled = False

    captured: dict = {"prompts": []}

    class FakeAdapter:
        async def execute(self, inv):
            captured["prompts"].append(inv.prompt)
            captured["last_inv"] = inv
            from adapters.types import AgentResult

            # Default response: rebase-and-retry. Tests can override.
            response = captured.get("next_response", "RESOLUTION: rebase-and-retry\n")
            return AgentResult(
                success=True,
                text=response,
                duration_s=0.01,
                files_changed=[],
                diff="",
            )

    class FakeRegistry:
        def get(self, role):
            from adapters.types import AgentSpec

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

    orch = type(
        "Orch",
        (),
        {
            "cwd": tmp_path,
            "session_id": "test",
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
    return orch


@pytest.mark.asyncio
async def test_escalate_builds_conflict_context_block(tmp_path: Path) -> None:
    """The envelope passed to delegate() contains a CONFLICT_CONTEXT block."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan(
            [Task(id="1.1", phase_id="1", title="t", description="d")]
        )
    )
    orch = _make_orch(tmp_path, pm)
    task = (await pm.get_task("1.1")) or pytest.fail("task not found")

    out = await ep._escalate_conflict_to_critic(
        orch,
        task,
        worktree=tmp_path / "wt",
        conflict_diff="diff --git a/foo b/foo\n+new line\n",
        already_applied_diff="diff --git a/foo b/foo\n+other line\n",
        conflict_files=["foo.py", "bar.py"],
    )
    # Default fake adapter returns rebase-and-retry.
    assert out.action == "rebase-and-retry"
    # Verify the prompt includes the CONFLICT_CONTEXT marker + file list.
    prompt = orch._captured["prompts"][0]
    assert "CONFLICT_CONTEXT:" in prompt
    assert "failing_task_id: 1.1" in prompt
    assert "foo.py" in prompt
    assert "bar.py" in prompt


@pytest.mark.asyncio
async def test_escalate_returns_abandon_when_critic_says_so(tmp_path: Path) -> None:
    """Critic returning RESOLUTION: abandon-task surfaces as that action."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    orch._captured["next_response"] = (
        "Cannot reconcile.\n\nRESOLUTION: abandon-task\n"
    )
    task = (await pm.get_task("1.1")) or pytest.fail()
    out = await ep._escalate_conflict_to_critic(
        orch,
        task,
        worktree=tmp_path / "wt",
        conflict_diff="",
    )
    assert out.action == "abandon-task"


@pytest.mark.asyncio
async def test_escalate_returns_rewrite_with_guidance(tmp_path: Path) -> None:
    """Critic returning rewrite includes the preceding text as guidance."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    orch._captured["next_response"] = (
        "Use lazy yaml import inside parse_config.\n\n"
        "RESOLUTION: rewrite\n"
    )
    task = (await pm.get_task("1.1")) or pytest.fail()
    out = await ep._escalate_conflict_to_critic(
        orch,
        task,
        worktree=tmp_path / "wt",
        conflict_diff="",
    )
    assert out.action == "rewrite"
    assert "lazy yaml" in out.rewrite_guidance


def test_conflict_resolution_default_action_is_abandon() -> None:
    """The dataclass default action is abandon-task (defensive)."""
    out = ConflictResolution()
    assert out.action == "abandon-task"
    assert out.rewrite_guidance == ""


# ---------------------------------------------------------------------------
# _apply_with_conflict_escalation — wired into apply_patch_to_main failure
# ---------------------------------------------------------------------------


class FakeWorktreeMgr:
    """Stub WorktreeManager that simulates apply behavior for tests."""

    def __init__(
        self,
        apply_fail_first: bool = True,
        three_way_succeeds: bool = True,
        rewrite_succeeds_round: int = 1,
    ):
        self._apply_fail_first = apply_fail_first
        self._three_way_succeeds = three_way_succeeds
        self._rewrite_succeeds_round = rewrite_succeeds_round
        self.apply_calls: list[tuple[bool, int]] = []  # (three_way, attempt_idx)
        self.attempt = 0
        # A2: the conflict-escalation helper now auto-attempts a ``--3way``
        # apply the moment a plain apply fails (BEFORE the critic). 3-way
        # attempts are a distinct mechanism from plain-apply *rewrite rounds*,
        # so they must NOT consume the ``rewrite_succeeds_round`` counter —
        # otherwise inserting the auto-3way step would silently shift which
        # plain attempt "succeeds". Count plain attempts separately.
        self.plain_attempt = 0

    async def apply_patch_to_main(
        self,
        worktree,
        base_ref: str = "HEAD",
        three_way: bool = False,
        commit_message: str | None = None,
    ) -> None:
        from orchestrator.worktree import WorktreeError

        self.attempt += 1
        self.apply_calls.append((three_way, self.attempt))
        if three_way:
            if self._three_way_succeeds:
                return
            raise WorktreeError("3way apply also failed")
        self.plain_attempt += 1
        # First plain attempt: optionally fail.
        if self.plain_attempt == 1:
            if self._apply_fail_first:
                raise WorktreeError("first apply failed (conflict)")
            return
        # Subsequent plain attempts (rewrite round retries): succeed on the
        # configured round count. ``rewrite_succeeds_round`` is the number
        # of rewrite rounds before success (1 = succeed after 1 rewrite,
        # i.e. the 2nd plain attempt).
        if self.plain_attempt >= self._rewrite_succeeds_round + 1:
            return
        raise WorktreeError(
            f"apply attempt {self.plain_attempt} still conflicts"
        )

    async def get_diff_vs_base(self, worktree, base_ref: str = "HEAD") -> str:
        return "diff --git a/foo b/foo\n+conflict\n"

    async def abort_failed_apply(self, targets: list[str] | None = None) -> None:
        # v0.41.0 A3: the conflict-escalation path now cleans the main tree
        # before marking a task blocked. The fake tree needs the method to
        # exist; there is nothing to clean in-memory, so this is a no-op.
        self.abort_calls = getattr(self, "abort_calls", 0) + 1


@pytest.mark.asyncio
async def test_apply_with_conflict_escalation_rebase_and_retry(
    tmp_path: Path,
) -> None:
    """A plain-apply conflict is auto-retried with three_way=True.

    A2: the 3-way retry now fires automatically the moment a plain apply
    fails, BEFORE the critic. A reconcilable conflict therefore applies via
    ``--3way`` and the helper returns success without a critic round.
    """
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    orch._captured["next_response"] = "RESOLUTION: rebase-and-retry\n"
    task = (await pm.get_task("1.1")) or pytest.fail()
    await pm.update_task_status("1.1", "in_progress")

    fake_wm = FakeWorktreeMgr(apply_fail_first=True, three_way_succeeds=True)
    success = await ep._apply_with_conflict_escalation(
        orch, task, tmp_path / "wt", fake_wm  # type: ignore[arg-type]
    )
    assert success is True
    # First call was non-3way (conflict); second was the auto-3way retry.
    assert fake_wm.apply_calls[0] == (False, 1)
    assert fake_wm.apply_calls[1] == (True, 2)


@pytest.mark.asyncio
async def test_apply_with_conflict_escalation_abandon(tmp_path: Path) -> None:
    """abandon-task directive blocks the task."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    orch._captured["next_response"] = "RESOLUTION: abandon-task\n"
    task = (await pm.get_task("1.1")) or pytest.fail()
    await pm.update_task_status("1.1", "in_progress")

    # A2: the critic is only consulted on a GENUINE conflict — i.e. when the
    # auto-3way apply ALSO fails. Model that here (``three_way_succeeds=
    # False``) so the abandon branch is reachable; a reconcilable conflict
    # would now apply via auto-3way and never reach the critic.
    fake_wm = FakeWorktreeMgr(apply_fail_first=True, three_way_succeeds=False)
    success = await ep._apply_with_conflict_escalation(
        orch, task, tmp_path / "wt", fake_wm  # type: ignore[arg-type]
    )
    assert success is False
    t = await pm.get_task("1.1")
    assert t is not None and t.status == "blocked"
    assert t.blocked_reason and "conflict_escalation:abandon" in t.blocked_reason


@pytest.mark.asyncio
async def test_apply_with_conflict_escalation_rewrite_succeeds(
    tmp_path: Path,
) -> None:
    """rewrite directive re-invokes developer; subsequent apply succeeds."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    orch._captured["next_response"] = (
        "Use lazy import.\n\nRESOLUTION: rewrite\n"
    )
    task = (await pm.get_task("1.1")) or pytest.fail()
    await pm.update_task_status("1.1", "in_progress")

    # A2: genuine conflict (auto-3way fails) so the critic rewrite branch is
    # reached; the developer rewrites and the next PLAIN apply succeeds.
    fake_wm = FakeWorktreeMgr(
        apply_fail_first=True,
        three_way_succeeds=False,
        rewrite_succeeds_round=1,
    )
    success = await ep._apply_with_conflict_escalation(
        orch, task, tmp_path / "wt", fake_wm  # type: ignore[arg-type]
    )
    assert success is True
    # Multiple apply attempts: initial plain conflict, auto-3way (fails),
    # then the post-rewrite plain retry succeeds.
    assert len(fake_wm.apply_calls) >= 2


@pytest.mark.asyncio
async def test_apply_with_conflict_escalation_rewrite_cap_exceeded(
    tmp_path: Path,
) -> None:
    """If rewrite never succeeds, abandon after the cap."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    orch._captured["next_response"] = "Try again.\n\nRESOLUTION: rewrite\n"
    task = (await pm.get_task("1.1")) or pytest.fail()
    await pm.update_task_status("1.1", "in_progress")

    # A2: genuine conflict (auto-3way also fails) so the critic rewrite loop
    # is reached. Rewrite "succeeds" on plain round 99 (never within cap).
    fake_wm = FakeWorktreeMgr(
        apply_fail_first=True,
        three_way_succeeds=False,
        rewrite_succeeds_round=99,
    )
    success = await ep._apply_with_conflict_escalation(
        orch, task, tmp_path / "wt", fake_wm  # type: ignore[arg-type]
    )
    assert success is False
    t = await pm.get_task("1.1")
    assert t is not None and t.status == "blocked"
    assert t.blocked_reason and "rewrite_cap_exceeded" in t.blocked_reason


@pytest.mark.asyncio
async def test_apply_with_conflict_escalation_3way_also_fails(
    tmp_path: Path,
) -> None:
    """If the 3-way apply also fails, the task is blocked."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    orch._captured["next_response"] = "RESOLUTION: rebase-and-retry\n"
    task = (await pm.get_task("1.1")) or pytest.fail()
    await pm.update_task_status("1.1", "in_progress")

    fake_wm = FakeWorktreeMgr(
        apply_fail_first=True, three_way_succeeds=False
    )
    success = await ep._apply_with_conflict_escalation(
        orch, task, tmp_path / "wt", fake_wm  # type: ignore[arg-type]
    )
    assert success is False
    t = await pm.get_task("1.1")
    assert t is not None and t.status == "blocked"
    assert t.blocked_reason and "3way_failed" in t.blocked_reason


@pytest.mark.asyncio
async def test_apply_with_conflict_escalation_clean_apply_no_critic(
    tmp_path: Path,
) -> None:
    """When the initial apply succeeds, the critic is NOT invoked."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan([Task(id="1.1", phase_id="1", title="t", description="d")])
    )
    orch = _make_orch(tmp_path, pm)
    task = (await pm.get_task("1.1")) or pytest.fail()
    await pm.update_task_status("1.1", "in_progress")

    fake_wm = FakeWorktreeMgr(apply_fail_first=False)
    success = await ep._apply_with_conflict_escalation(
        orch, task, tmp_path / "wt", fake_wm  # type: ignore[arg-type]
    )
    assert success is True
    assert orch._captured["prompts"] == []  # no critic call


# ---------------------------------------------------------------------------
# F-4: apply-time edit-scope enforcement (off | warn | block). The effective
# scope is computed IN-HELPER as (phase.edit_scope or plan.edit_scope) UNION
# (task.files + task.files_new + task.extended_scope); empty → no check. The
# diff's target files are pulled via the manager's ``get_diff_vs_base`` +
# ``extract_files_from_diff`` and checked with ``dag.is_in_scope``.
# ---------------------------------------------------------------------------


class ScopeFakeWorktreeMgr(FakeWorktreeMgr):
    """FakeWorktreeMgr whose diff target paths are configurable.

    ``apply_patch_to_main`` records whether ``edit_scope`` was forwarded so
    block-mode (effective scope passed) vs off/warn-mode (None passed) can be
    distinguished — and it raises ``EditScopeViolation`` like the real gate
    when a forwarded scope excludes a diff path, so block-mode is end-to-end.
    """

    def __init__(self, diff_files: list[str], **kw: Any) -> None:
        super().__init__(**kw)
        self._diff_files = diff_files
        # (three_way, attempt_idx, edit_scope) for each apply call.
        self.scope_apply_calls: list[tuple[bool, int, list[str] | None]] = []

    async def get_diff_vs_base(self, worktree: Any, base_ref: str = "HEAD") -> str:
        return "".join(
            f"diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n@@ -0,0 +1 @@\n+x\n"
            for f in self._diff_files
        )

    async def apply_patch_to_main(  # type: ignore[override]
        self,
        worktree: Any,
        base_ref: str = "HEAD",
        three_way: bool = False,
        edit_scope: list[str] | None = None,
        commit_message: str | None = None,
    ) -> None:
        self.attempt += 1
        self.scope_apply_calls.append((three_way, self.attempt, edit_scope))
        # Mirror the real gate: a forwarded scope excluding any diff path
        # raises BEFORE the apply lands (block mode).
        if edit_scope:
            from orchestrator.dag import EditScopeViolation, is_in_scope

            for fp in self._diff_files:
                if not is_in_scope(fp, edit_scope):
                    raise EditScopeViolation(
                        f"diff hunk targets out-of-scope file {fp!r}; "
                        f"resolved edit_scope = {edit_scope!r}"
                    )
        # Clean apply otherwise (no conflict modelled here).
        return None


def _mk_plan_scoped(tasks: list[Task], plan_scope: list[str]) -> Plan:
    return Plan(
        plan_id="p-scope",
        spec_hash="cafe",
        phases=[Phase(id="1", title="conflict", tasks=tasks)],
        edit_scope=plan_scope,
        created_at=_iso(),
        updated_at=_iso(),
    )


def _read_ledger_ops(tmp_path: Path) -> list[str]:
    import json

    from state.ledger import ledger_path

    lp = ledger_path(tmp_path)
    if not lp.exists():
        return []
    return [json.loads(line)["op"] for line in lp.read_text().splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_apply_scope_off_skips_check(tmp_path: Path) -> None:
    """policy=off: no scope check, no warn — edit_scope=None forwarded."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan_scoped(
            [Task(id="1.1", phase_id="1", title="t", description="d", files=["src/a.py"])],
            plan_scope=["src"],
        )
    )
    orch = _make_orch(tmp_path, pm)
    orch.cfg.enforce_apply_time_edit_scope = "off"
    task = (await pm.get_task("1.1")) or pytest.fail()
    await pm.update_task_status("1.1", "in_progress")

    # Diff touches an out-of-scope file, but policy=off → no warn, applies.
    fake_wm = ScopeFakeWorktreeMgr(diff_files=["docs/out.md"], apply_fail_first=False)
    success = await ep._apply_with_conflict_escalation(
        orch, task, tmp_path / "wt", fake_wm  # type: ignore[arg-type]
    )
    assert success is True
    # No edit_scope forwarded (None), and no warn ledger op.
    assert all(scope is None for _, _, scope in fake_wm.scope_apply_calls)
    assert "edit_scope_apply_violation" not in _read_ledger_ops(tmp_path)


@pytest.mark.asyncio
async def test_apply_scope_warn_logs_and_applies(tmp_path: Path) -> None:
    """policy=warn: out-of-scope file LOGS + ledger breadcrumb, STILL applies."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan_scoped(
            [Task(id="1.1", phase_id="1", title="t", description="d", files=["src/a.py"])],
            plan_scope=["src"],
        )
    )
    orch = _make_orch(tmp_path, pm)
    orch.cfg.enforce_apply_time_edit_scope = "warn"
    task = (await pm.get_task("1.1")) or pytest.fail()
    await pm.update_task_status("1.1", "in_progress")

    fake_wm = ScopeFakeWorktreeMgr(diff_files=["docs/out.md"], apply_fail_first=False)
    success = await ep._apply_with_conflict_escalation(
        orch, task, tmp_path / "wt", fake_wm  # type: ignore[arg-type]
    )
    # WARN never blocks: the diff applies.
    assert success is True
    # Apply was called with edit_scope=None (warn does not gate the apply).
    assert any(scope is None for _, _, scope in fake_wm.scope_apply_calls)
    # A best-effort ledger breadcrumb was appended.
    assert "edit_scope_apply_violation" in _read_ledger_ops(tmp_path)


@pytest.mark.asyncio
async def test_apply_scope_warn_no_warn_when_declared_files_only(
    tmp_path: Path,
) -> None:
    """A task editing ONLY its declared ``files`` must NOT warn, even if those
    files lie outside the plan/phase edit_scope (the union includes them)."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan_scoped(
            [
                Task(
                    id="1.1",
                    phase_id="1",
                    title="t",
                    description="d",
                    files=["lib/helper.py"],  # outside plan_scope=["src"]
                )
            ],
            plan_scope=["src"],
        )
    )
    orch = _make_orch(tmp_path, pm)
    orch.cfg.enforce_apply_time_edit_scope = "warn"
    task = (await pm.get_task("1.1")) or pytest.fail()
    await pm.update_task_status("1.1", "in_progress")

    # Diff touches exactly the declared file.
    fake_wm = ScopeFakeWorktreeMgr(diff_files=["lib/helper.py"], apply_fail_first=False)
    success = await ep._apply_with_conflict_escalation(
        orch, task, tmp_path / "wt", fake_wm  # type: ignore[arg-type]
    )
    assert success is True
    assert "edit_scope_apply_violation" not in _read_ledger_ops(tmp_path)


@pytest.mark.asyncio
async def test_apply_scope_warn_no_warn_when_files_new(tmp_path: Path) -> None:
    """A task creating a file declared in ``files_new`` must NOT warn."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan_scoped(
            [
                Task(
                    id="1.1",
                    phase_id="1",
                    title="t",
                    description="d",
                    files=["src/a.py"],
                    files_new=["src/new_helper.py"],
                )
            ],
            plan_scope=["src"],
        )
    )
    orch = _make_orch(tmp_path, pm)
    orch.cfg.enforce_apply_time_edit_scope = "warn"
    task = (await pm.get_task("1.1")) or pytest.fail()
    await pm.update_task_status("1.1", "in_progress")

    fake_wm = ScopeFakeWorktreeMgr(
        diff_files=["src/a.py", "src/new_helper.py"], apply_fail_first=False
    )
    success = await ep._apply_with_conflict_escalation(
        orch, task, tmp_path / "wt", fake_wm  # type: ignore[arg-type]
    )
    assert success is True
    assert "edit_scope_apply_violation" not in _read_ledger_ops(tmp_path)


@pytest.mark.asyncio
async def test_apply_scope_warn_empty_scope_is_noop(tmp_path: Path) -> None:
    """Empty effective scope (no plan/phase scope AND empty task files) →
    no check, no warn (legacy whole-repo no-op). This is what keeps an
    empty-``files`` corrective from being warned/blocked."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        # No plan edit_scope, task has EMPTY files (corrective-task shape).
        _mk_plan_scoped(
            [Task(id="1.1", phase_id="1", title="t", description="d")],
            plan_scope=[],
        )
    )
    orch = _make_orch(tmp_path, pm)
    orch.cfg.enforce_apply_time_edit_scope = "warn"
    task = (await pm.get_task("1.1")) or pytest.fail()
    await pm.update_task_status("1.1", "in_progress")

    fake_wm = ScopeFakeWorktreeMgr(diff_files=["anywhere/x.py"], apply_fail_first=False)
    success = await ep._apply_with_conflict_escalation(
        orch, task, tmp_path / "wt", fake_wm  # type: ignore[arg-type]
    )
    assert success is True
    # Empty scope → edit_scope=None forwarded, no warn.
    assert all(scope is None for _, _, scope in fake_wm.scope_apply_calls)
    assert "edit_scope_apply_violation" not in _read_ledger_ops(tmp_path)


@pytest.mark.asyncio
async def test_apply_scope_block_raises_and_blocks_task(tmp_path: Path) -> None:
    """policy=block: an out-of-scope diff forwards the effective scope to the
    apply gate, which raises EditScopeViolation. The helper catches it and
    blocks the task directly (a scope violation is a deliberate policy block,
    NOT a recoverable merge conflict — no critic round)."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan_scoped(
            [Task(id="1.1", phase_id="1", title="t", description="d", files=["src/a.py"])],
            plan_scope=["src"],
        )
    )
    orch = _make_orch(tmp_path, pm)
    orch.cfg.enforce_apply_time_edit_scope = "block"
    task = (await pm.get_task("1.1")) or pytest.fail()
    await pm.update_task_status("1.1", "in_progress")

    # Out-of-scope diff: the forwarded scope (incl. "src") excludes
    # docs/out.md → the gate raises on the FIRST plain apply. No conflict is
    # modelled (apply_fail_first=False); the EditScopeViolation is what stops
    # the apply, not a WorktreeError.
    fake_wm = ScopeFakeWorktreeMgr(
        diff_files=["docs/out.md"],
        apply_fail_first=False,
    )
    success = await ep._apply_with_conflict_escalation(
        orch, task, tmp_path / "wt", fake_wm  # type: ignore[arg-type]
    )
    assert success is False
    t = await pm.get_task("1.1")
    assert t is not None and t.status == "blocked"
    assert t.blocked_reason and "edit_scope_apply_violation" in t.blocked_reason
    # The critic was NOT consulted (direct block).
    assert orch._captured["prompts"] == []
    # The apply call forwarded the effective (non-None) scope including "src".
    assert any(
        scope is not None and "src" in scope
        for _, _, scope in fake_wm.scope_apply_calls
    )


@pytest.mark.asyncio
async def test_apply_scope_block_in_scope_applies_cleanly(tmp_path: Path) -> None:
    """policy=block: an IN-scope diff passes the gate and applies normally."""
    pm = PlanManager(tmp_path, session_id="s1")
    await pm.init_plan(
        _mk_plan_scoped(
            [Task(id="1.1", phase_id="1", title="t", description="d", files=["src/a.py"])],
            plan_scope=["src"],
        )
    )
    orch = _make_orch(tmp_path, pm)
    orch.cfg.enforce_apply_time_edit_scope = "block"
    task = (await pm.get_task("1.1")) or pytest.fail()
    await pm.update_task_status("1.1", "in_progress")

    fake_wm = ScopeFakeWorktreeMgr(diff_files=["src/a.py"], apply_fail_first=False)
    success = await ep._apply_with_conflict_escalation(
        orch, task, tmp_path / "wt", fake_wm  # type: ignore[arg-type]
    )
    assert success is True
    t = await pm.get_task("1.1")
    assert t is not None and t.status != "blocked"
