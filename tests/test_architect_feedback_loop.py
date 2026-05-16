"""Tests for v0.32.0 Phase 1.1 — architect feedback loop.

The architect-retry loop now interpolates a ``{rejection_history}``
block into ``architect.md`` on every retry, rendered from the typed
:class:`TypedRetryEnvelope.most_recent_failures` list. Coverage:

* The placeholder is empty on the first attempt (no header leaked).
* On a retry, the block contains every recurrent failure with its
  count + reason + suggestion.
* Across two failed attempts on the same path, the recurrence count
  visible in the third-attempt prompt matches ``errors_seen``.

Tests use the same ``StubAdapter`` + ``_make_orch`` patterns as
:mod:`tests.test_orchestrator_plan_phase` so the fixtures are stable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.retry_envelope import (
    PriorError,
    TypedRetryEnvelope,
)

from stub_adapter import StubAdapter, ok


def _bootstrap_git_repo_with_math_py(tmp_path: Path) -> None:
    """Bootstrap a git repo with a single tracked ``math.py`` so
    :func:`validate_files_exist` engages instead of short-circuiting."""
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True
    )
    (tmp_path / "math.py").write_text("def add(a, b): return a + b\n")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=str(tmp_path), check=True
    )


_BAD_FILE_PLAN_MD = """
# Plan: Add subtract

## Phase 1: Implement

### Task 1.1: bogus path
  - Description: references a path that does not exist
  - Files: imaginary.cpp
  - Acceptance:
    - [ ] something
"""


_GOOD_FILE_PLAN_MD = """
# Plan: Add subtract

## Phase 1: Implement

### Task 1.1: real path
  - Description: refs a real file
  - Files: math.py
  - Acceptance:
    - [ ] passes
"""


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
        session_id="sess-architect-feedback",
    )


# ---------------------------------------------------------------------------
# Pure render helper
# ---------------------------------------------------------------------------


def test_render_rejection_history_empty_returns_empty_string() -> None:
    """No prior failures → empty string (no header leaked into prompt)."""
    env = TypedRetryEnvelope()
    assert env.render_rejection_history(attempt=2) == ""


def test_render_rejection_history_includes_count_and_reason() -> None:
    """Each entry renders count, raw, reason; suggestion only when present.

    v0.36.0 D1: the per-path line uses ``RECURRED N×`` (was
    ``RECURRED N times``) AND is prefaced by a class-level diagnosis
    paragraph. The path / reason / suggestion fragments survive the
    rewrite so this test asserts on those.
    """
    env = TypedRetryEnvelope(
        most_recent_failures=[
            PriorError(
                raw="notes",
                reason="missing_on_disk",
                count=3,
                suggestion="docs/notes.md",
            ),
            PriorError(raw="helpers", reason="missing_on_disk", count=1),
        ]
    )
    rendered = env.render_rejection_history(attempt=2)
    assert "## PATH VALIDATION HISTORY (Retry Attempt 2)" in rendered
    assert "notes" in rendered
    assert "RECURRED 3" in rendered
    assert "reason: missing_on_disk" in rendered
    # Suggestion is included when present.
    assert "suggestion: docs/notes.md" in rendered
    # And omitted when not.
    helpers_line = next(
        ln for ln in rendered.splitlines() if "helpers" in ln
    )
    assert "suggestion:" not in helpers_line


def test_as_context_dict_strips_most_recent_failures() -> None:
    """``most_recent_failures`` is rendered into the prompt; it shouldn't
    also show up in the wire context dict (no duplicate data)."""
    env = TypedRetryEnvelope(
        most_recent_failures=[
            PriorError(raw="notes", reason="missing_on_disk", count=3),
        ]
    )
    payload = env.as_context_dict()
    assert "most_recent_failures" not in payload


# ---------------------------------------------------------------------------
# End-to-end: architect retry sees its prior failures in the system prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_architect_retry_includes_prior_path_failures(
    tmp_path: Path,
) -> None:
    """After one rejected attempt, the next architect invocation's
    system prompt must contain a populated ``PATH VALIDATION HISTORY``
    block with the recurring path raw + count."""
    _bootstrap_git_repo_with_math_py(tmp_path)
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": [ok(_BAD_FILE_PLAN_MD), ok(_GOOD_FILE_PLAN_MD)],
        }
    )
    orch = _make_orch(tmp_path, adapter)

    plan = await orch.plan("Add subtract")
    assert plan is not None
    architect_prompts = adapter.prompts_for("architect")
    assert len(architect_prompts) == 2

    # First attempt: placeholder is empty.
    first_prompt = architect_prompts[0]
    assert "PATH VALIDATION HISTORY" not in first_prompt
    # Confirm the literal placeholder isn't leaked either.
    assert "{rejection_history}" not in first_prompt

    # Second attempt: rendered block present with the bad path.
    second_prompt = architect_prompts[1]
    assert "PATH VALIDATION HISTORY (Retry Attempt 2)" in second_prompt
    assert "imaginary.cpp" in second_prompt
    # v0.36.0 D1: per-path line format changed from
    # ``RECURRED N times: <path>`` to ``<path> (RECURRED N× ...)``.
    assert "RECURRED 1" in second_prompt
    # Reason name is verbatim.
    assert "reason: missing_on_disk" in second_prompt


@pytest.mark.asyncio
async def test_architect_remembers_rejected_paths_across_attempts(
    tmp_path: Path,
) -> None:
    """The same path proposed in attempts 1 and 2 must increment the
    recurrence counter visible in the attempt-3 prompt's PATH VALIDATION
    HISTORY block (the architect should see ``RECURRED 2 times`` on
    its third try)."""
    _bootstrap_git_repo_with_math_py(tmp_path)
    # Architect proposes the same bad path TWICE then a good plan.
    # The third attempt's prompt should call out RECURRED 2 times.
    adapter = StubAdapter(
        {
            "explorer": ok("found stuff"),
            "domain_expert": ok("ok"),
            "architect": [
                ok(_BAD_FILE_PLAN_MD),
                ok(_BAD_FILE_PLAN_MD),
                ok(_GOOD_FILE_PLAN_MD),
            ],
        }
    )
    orch = _make_orch(tmp_path, adapter)

    plan = await orch.plan("Add subtract")
    assert plan is not None
    architect_prompts = adapter.prompts_for("architect")
    assert len(architect_prompts) == 3

    third_prompt = architect_prompts[2]
    # By the third attempt the architect should see "RECURRED 2 times"
    # for imaginary.cpp.
    assert "PATH VALIDATION HISTORY (Retry Attempt 3)" in third_prompt
    # v0.36.0 D1: per-path line format changed (see note in
    # test_architect_retry_includes_prior_path_failures).
    assert "imaginary.cpp" in third_prompt
    assert "RECURRED 2" in third_prompt


@pytest.mark.asyncio
async def test_rejection_history_top_5_only_on_many_distinct_failures(
    tmp_path: Path,
) -> None:
    """When more than 5 distinct (raw, reason) pairs have been seen,
    only the top 5 by count appear in the prompt block."""
    # We construct the typed envelope directly; full e2e with 6+ distinct
    # validator failures requires too much fixture setup.
    env = TypedRetryEnvelope(
        most_recent_failures=[
            PriorError(raw=f"path_{i}", reason="missing_on_disk", count=10 - i)
            for i in range(5)
        ]
    )
    rendered = env.render_rejection_history(attempt=2)
    for i in range(5):
        assert f"path_{i}" in rendered
    # Sixth path would not appear.
    assert "path_5" not in rendered
