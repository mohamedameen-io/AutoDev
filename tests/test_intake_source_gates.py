"""intake_sources availability-gate regression tests (Cluster C1).

The Run-4 field failure: every gather source reported ``nothing_available`` —
including ``repo`` (which should ride the explorer evidence) and ``github``
(which should activate on a ``#NNN`` ref when the ``gh`` CLI is present). These
tests pin the exact activation conditions per the ADR-0045 plan:

- **repo** activates iff explorer evidence exists AND
  ``cfg.reuse_explorer_evidence`` is on (reuse_explorer_evidence, no second pass).
- **github** activates iff a ``#NNN`` / ``GH-NNN`` / GitHub-URL ref is in the
  intent AND the ``gh`` CLI is available — and deactivates if either is absent.
- **jira** stays gated on a Jira key only (MCP reachability is decided at
  dispatch time), so this file does NOT change the jira contract.

No network calls: the ``gh`` probe is monkeypatched on both branches so the
tests are deterministic regardless of whether ``gh`` is installed in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.defaults import default_config
from orchestrator.intake_sources import github as github_mod
from orchestrator.intake_sources.github import GitHubSource
from orchestrator.intake_sources.repo import RepoSource
from state.evidence import write_evidence
from state.schemas import ExploreEvidence


async def _write_explore(cwd: Path, findings: str = "bar() drops a token") -> None:
    ev = ExploreEvidence(
        task_id="plan-explore",
        findings=findings,
        files_referenced=["src/foo.py"],
    )
    await write_evidence(cwd, "plan-explore", ev)


# --------------------------------------------------------------------------- #
# repo: rides the explorer evidence (reuse_explorer_evidence)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_repo_activates_given_explorer_evidence(tmp_path: Path) -> None:
    """With explorer evidence on disk + reuse on, repo is available (the
    Run-4-DOA case that should have been live)."""
    cfg = default_config().intake
    src = RepoSource()
    # No explorer evidence yet → not available.
    assert await src.available(cwd=tmp_path, intent="fix a bug", cfg=cfg) is False
    # Explorer pass has run → repo rides its evidence.
    await _write_explore(tmp_path)
    assert await src.available(cwd=tmp_path, intent="fix a bug", cfg=cfg) is True


@pytest.mark.asyncio
async def test_repo_deactivates_when_reuse_disabled(tmp_path: Path) -> None:
    await _write_explore(tmp_path)
    cfg = default_config().intake
    cfg.reuse_explorer_evidence = False
    assert await RepoSource().available(cwd=tmp_path, intent="x", cfg=cfg) is False


@pytest.mark.asyncio
async def test_repo_deactivates_when_evidence_findings_empty(tmp_path: Path) -> None:
    """An explorer evidence file with whitespace-only findings is not real
    evidence — repo stays inactive (no empty fragment dispatched)."""
    await _write_explore(tmp_path, findings="   ")
    cfg = default_config().intake
    assert await RepoSource().available(cwd=tmp_path, intent="x", cfg=cfg) is False


# --------------------------------------------------------------------------- #
# github: #NNN ref AND gh available
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_github_activates_given_ref_and_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``#199`` ref + an available ``gh`` CLI → github source is live."""
    monkeypatch.setattr(github_mod, "_gh_available", lambda: True)
    cfg = default_config().intake
    assert (
        await GitHubSource().available(
            cwd=tmp_path, intent="Fix the 429 bug, see #199", cfg=cfg
        )
        is True
    )


@pytest.mark.asyncio
async def test_github_activates_for_full_url_with_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github_mod, "_gh_available", lambda: True)
    cfg = default_config().intake
    assert (
        await GitHubSource().available(
            cwd=tmp_path,
            intent="see https://github.com/o/r/pull/7 for context",
            cfg=cfg,
        )
        is True
    )


@pytest.mark.asyncio
async def test_github_deactivates_without_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No issue ref → github is inactive even when gh is installed."""
    monkeypatch.setattr(github_mod, "_gh_available", lambda: True)
    cfg = default_config().intake
    assert (
        await GitHubSource().available(
            cwd=tmp_path, intent="plain prose, no issue ref", cfg=cfg
        )
        is False
    )


@pytest.mark.asyncio
async def test_github_deactivates_without_gh_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``#199`` ref but NO ``gh`` CLI (the headless-runner reality) → github is
    inactive: the agent could not run ``gh issue view`` anyway, so we never spend
    a fragment on it (this is the half of the gate the field run was missing)."""
    monkeypatch.setattr(github_mod, "_gh_available", lambda: False)
    cfg = default_config().intake
    assert (
        await GitHubSource().available(
            cwd=tmp_path, intent="Fix the 429 bug, see #199", cfg=cfg
        )
        is False
    )


def test_gh_available_probe_uses_which(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default ``_gh_available`` probe is a no-network ``shutil.which`` check."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh" if name == "gh" else None)
    assert github_mod._gh_available() is True
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert github_mod._gh_available() is False
