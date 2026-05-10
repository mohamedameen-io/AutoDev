"""Tests for v0.25.0 wiring: ``orchestrator.plan_phase`` injects a
``candidate_files`` block into the architect's :class:`DelegationEnvelope`
when the file/symbol index exists.

These tests exercise the wiring layer (``plan_phase.py`` reading from
``IndexQuery`` and putting the result into ``architect_env.context``).
The index core (``state.file_index``) ships in parallel; tests here
either monkey-patch ``IndexQuery`` directly or run against a real
index file when the parallel agent's code has landed.

Three behaviors covered (per the v0.25.0 plan):

  * ``test_candidate_files_block_in_architect_envelope`` — happy path:
    the prompt sent to the architect contains the rendered digest.
  * ``test_candidate_files_empty_string_when_index_disabled`` —
    ``cfg.index_enabled=False`` zeros the digest; the architect still
    runs but the prompt's CANDIDATE FILES block is empty.
  * ``test_index_query_failure_does_not_block_plan`` — a transient
    index error is logged + swallowed; the planner continues with an
    empty digest, no exception escapes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from state.schemas import Plan

from stub_adapter import StubAdapter, ok


CANONICAL_PLAN_MD = """# Plan: Add subtract(a, b)

## Phase 1: Implement

### Task 1.1: Add subtract function to math.py
  - Description: Add subtract(a, b) that returns a - b
  - Files: math.py
  - Acceptance:
    - [ ] Function subtract defined
"""


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(repo), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(repo), check=True
    )
    (repo / "math.py").write_text("def add(a, b): return a + b\n")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=str(repo), check=True
    )


def _make_orch(cwd: Path, adapter: StubAdapter, *, index_enabled: bool = True) -> Orchestrator:
    cfg = default_config()
    cfg.index_enabled = index_enabled
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-test-plan-index",
    )


@pytest.mark.asyncio
async def test_candidate_files_block_in_architect_envelope(
    tmp_path: Path,
) -> None:
    """When the index is present, the architect prompt contains the rendered
    candidate-files digest. We mock :class:`IndexQuery` so this test does
    not depend on the parallel-agent's index core being on disk yet.
    """
    _git_init(tmp_path)

    # Create a fake .autodev/index.db so the existence check passes;
    # contents don't matter because we monkey-patch IndexQuery.
    autodev = tmp_path / ".autodev"
    autodev.mkdir(parents=True, exist_ok=True)
    (autodev / "index.db").write_bytes(b"fake-sqlite-bytes")

    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )

    fake_digest = mock.MagicMock()
    fake_digest.render.return_value = (
        "CANDIDATE_FILES (top matches):\n  - math.py (file)\n"
    )
    fake_query = mock.MagicMock()
    fake_query.get_candidates_for_spec.return_value = fake_digest

    fake_index_module = mock.MagicMock()
    fake_index_module.IndexQuery = mock.MagicMock(return_value=fake_query)

    with mock.patch.dict(
        "sys.modules", {"state.file_index": fake_index_module}
    ):
        orch = _make_orch(tmp_path, adapter, index_enabled=True)
        plan = await orch.plan("Add subtract(a, b)")

    assert isinstance(plan, Plan)
    architect_prompts = adapter.prompts_for("architect")
    assert len(architect_prompts) == 1
    # The candidate-files digest text we returned from the mock should
    # be substring-present in the prompt body.
    assert "CANDIDATE_FILES" in architect_prompts[0]
    assert "math.py" in architect_prompts[0]


@pytest.mark.asyncio
async def test_candidate_files_empty_string_when_index_disabled(
    tmp_path: Path,
) -> None:
    """``cfg.index_enabled=False`` skips the IndexQuery call entirely;
    the architect prompt contains an empty candidate_files context value
    (no rendered digest, no exception)."""
    _git_init(tmp_path)

    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )

    # Even if a fake module is registered, it must NOT be queried because
    # cfg.index_enabled=False short-circuits before the import.
    fake_query = mock.MagicMock()
    fake_index_module = mock.MagicMock()
    fake_index_module.IndexQuery = mock.MagicMock(return_value=fake_query)

    with mock.patch.dict(
        "sys.modules", {"state.file_index": fake_index_module}
    ):
        orch = _make_orch(tmp_path, adapter, index_enabled=False)
        plan = await orch.plan("Add subtract(a, b)")

    assert plan is not None
    # IndexQuery must not have been instantiated.
    fake_index_module.IndexQuery.assert_not_called()
    # And the architect prompt should not contain a CANDIDATE_FILES header
    # (the digest is empty when disabled).
    architect_prompts = adapter.prompts_for("architect")
    assert "CANDIDATE_FILES (top matches" not in architect_prompts[0]


@pytest.mark.asyncio
async def test_index_query_failure_does_not_block_plan(tmp_path: Path) -> None:
    """A transient :class:`IndexQuery` error during ``get_candidates_for_spec``
    must be logged + swallowed. The planner continues with an empty digest;
    the architect call still fires, and the Plan is approved."""
    _git_init(tmp_path)

    autodev = tmp_path / ".autodev"
    autodev.mkdir(parents=True, exist_ok=True)
    (autodev / "index.db").write_bytes(b"fake-sqlite-bytes")

    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": ok(CANONICAL_PLAN_MD),
        }
    )

    fake_query = mock.MagicMock()
    fake_query.get_candidates_for_spec.side_effect = RuntimeError(
        "simulated index corruption"
    )
    fake_index_module = mock.MagicMock()
    fake_index_module.IndexQuery = mock.MagicMock(return_value=fake_query)

    with mock.patch.dict(
        "sys.modules", {"state.file_index": fake_index_module}
    ):
        orch = _make_orch(tmp_path, adapter, index_enabled=True)
        plan = await orch.plan("Add subtract(a, b)")

    # Plan still approved — the failure was caught.
    assert plan is not None
    assert adapter.count("architect") == 1
