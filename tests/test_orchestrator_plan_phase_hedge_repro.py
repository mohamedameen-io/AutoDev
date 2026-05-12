"""Phase 0: end-to-end reproducer for the architect-hedge bug class.

Three synthetic fixtures from
:mod:`tests.fixtures.regression_synthetic` exercise the v0.26.2
plan-phase under different architect output patterns:

  * ``ARCHITECT_NOTES_IN_EDIT_SCOPE`` — bare-token EDIT_SCOPE entry.
    Recovers via the v0.26.2 persistent-drop.

  * ``ARCHITECT_PARENS_IN_TASK_FILES`` — paren-hedged Task.files entry.
    The v0.26.2 persistent-drop walks ``Plan.edit_scope`` (and was
    extended in v0.26.2 Phase 3 to also walk ``Task.files``), so the
    drop fires after three recurrences.

  * ``ARCHITECT_CLEAN_PLAN`` — control: no hedge. Single-shot success.

This file pins the **v0.26.2 baseline behavior**. Commit 11 (Phase 1
tightening) is expected to flip the parens-variant assertion to
"Phase 1 parser strips the paren-hedge upstream, no drop op fires."
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator

from stub_adapter import StubAdapter, ok
from fixtures.regression_synthetic import (
    ARCHITECT_CLEAN_PLAN,
    ARCHITECT_NOTES_IN_EDIT_SCOPE,
    ARCHITECT_PARENS_IN_TASK_FILES,
)


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-hedge-repro",
    )


def _init_git_repo_with_math(cwd: Path) -> None:
    """Bootstrap a populated git repo with ``src/math/__init__.py``."""
    subprocess.run(["git", "init", "-q"], cwd=str(cwd), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(cwd), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(cwd), check=True
    )
    (cwd / "src" / "math").mkdir(parents=True, exist_ok=True)
    (cwd / "src" / "math" / "__init__.py").write_text(
        "def add(a, b): return a + b\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=str(cwd), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=str(cwd), check=True
    )


@pytest.mark.asyncio
async def test_notes_in_edit_scope_recovers_via_v026_drop(
    tmp_path: Path,
) -> None:
    """v0.26.2 baseline: architect emits the bare-token ``notes`` entry
    three times; persistent-drop fires; the surviving scope is
    ``["src/math"]`` and a ``scope_entry_dropped`` ledger op records
    the action.
    """
    _init_git_repo_with_math(tmp_path)
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": [
                ok(ARCHITECT_NOTES_IN_EDIT_SCOPE),
                ok(ARCHITECT_NOTES_IN_EDIT_SCOPE),
                ok(ARCHITECT_NOTES_IN_EDIT_SCOPE),
            ],
        }
    )
    orch = _make_orch(tmp_path, adapter)

    plan = await orch.plan("Add subtract")
    assert plan is not None
    assert plan.edit_scope == ["src/math"]
    assert adapter.count("architect") == 3

    from state.ledger import read_entries

    ledger = read_entries(tmp_path)
    drop_ops = [e for e in ledger if e.op == "scope_entry_dropped"]
    assert len(drop_ops) == 1
    assert drop_ops[0].payload["path"] == "notes"


@pytest.mark.asyncio
async def test_parens_in_task_files_baseline_behavior(
    tmp_path: Path,
) -> None:
    """v0.26.2 BASELINE: the paren-hedged ``Task.files`` entry is the
    bug class Phase 1 (Commit 3) closes upstream.

    Today the architect emits the paren-hedge three times; v0.26.2
    Phase 3 extended the drop to walk ``Task.files``, so the drop
    fires and removes the bad entry. The test pins that behavior so
    Commit 11 can flip to "Phase 1 stripped the hedge upstream on
    attempt #1; no drop op fired."
    """
    _init_git_repo_with_math(tmp_path)
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": [
                ok(ARCHITECT_PARENS_IN_TASK_FILES),
                ok(ARCHITECT_PARENS_IN_TASK_FILES),
                ok(ARCHITECT_PARENS_IN_TASK_FILES),
            ],
        }
    )
    orch = _make_orch(tmp_path, adapter)

    plan = await orch.plan("Add subtract")
    assert plan is not None
    # v0.26.2 baseline: drop walked Task.files and removed the
    # paren-hedged entry. The surviving task.files is just the real
    # path. The architect was called three times.
    assert adapter.count("architect") == 3
    task = plan.phases[0].tasks[0]
    assert task.files == ["src/math/__init__.py"], (
        f"v0.26.2 baseline: expected only the real file to survive "
        f"the drop, got {task.files!r}"
    )

    from state.ledger import read_entries

    ledger = read_entries(tmp_path)
    drop_ops = [e for e in ledger if e.op == "scope_entry_dropped"]
    # v0.26.2 baseline: exactly one drop op fires (for the paren
    # entry). Commit 11 flips this to ``len(drop_ops) == 0`` once
    # Phase 1's parser shape-check strips the paren upstream.
    assert len(drop_ops) == 1
    assert "(and any helper file" in drop_ops[0].payload["path"]


@pytest.mark.asyncio
async def test_clean_plan_passes_through_in_one_attempt(
    tmp_path: Path,
) -> None:
    """Control: a clean plan validates on the first attempt — no retry,
    no drop. Regression guard against accidentally tightening the
    parser in a way that rejects valid output.
    """
    _init_git_repo_with_math(tmp_path)
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": ok(ARCHITECT_CLEAN_PLAN),
        }
    )
    orch = _make_orch(tmp_path, adapter)

    plan = await orch.plan("Add subtract")
    assert plan is not None
    assert plan.edit_scope == ["src/math"]
    assert adapter.count("architect") == 1

    from state.ledger import read_entries

    ledger = read_entries(tmp_path)
    drop_ops = [e for e in ledger if e.op == "scope_entry_dropped"]
    assert drop_ops == []
