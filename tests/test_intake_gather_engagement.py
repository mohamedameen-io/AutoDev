"""Intake gather *engagement* tests (v0.42.1 F3, gate-a).

Run-5 field failure: all 3 gather sources skipped (``n_facts=0``) — issue #199
was never pulled, so "enrichment" was only the assumed clarifying answers. These
tests pin the two highest-value fixes:

- **repo**: stays available whenever plain-explore evidence with non-empty
  findings exists under the default config, and logs a STRUCTURED skip reason
  (``reuse_disabled`` | ``no_evidence`` | ``empty_findings``) when it does skip,
  so a field run is auditable.
- **github**: when the thin intent carries NO explicit ``#NNN`` ref but ``gh`` is
  available, the source AUTONOMOUSLY discovers the canonical issue via
  ``gh issue list`` (scoped to the git remote, searched by symptom keywords),
  guarded by a token-overlap match so a wrong/unrelated issue is discarded.

No network: ``gh`` / ``git remote`` are stubbed by monkeypatching the helpers.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

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
# Part A — repo activation + structured skip observability
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_repo_available_under_default_cfg_with_findings(tmp_path: Path) -> None:
    """GUARD: with plain-explore evidence + default cfg, repo is available."""
    cfg = default_config().intake
    await _write_explore(tmp_path)
    assert await RepoSource().available(cwd=tmp_path, intent="x", cfg=cfg) is True


def _skip_reasons(cap: list[dict]) -> list[str]:
    """Reasons from any ``intake.gather.repo_skipped`` events captured."""
    return [
        e.get("reason")
        for e in cap
        if e.get("event") == "intake.gather.repo_skipped"
    ]


@pytest.mark.asyncio
async def test_repo_skip_logs_no_evidence_reason(tmp_path: Path) -> None:
    """No explorer evidence at all → skip with reason=no_evidence (auditable)."""
    cfg = default_config().intake
    with capture_logs() as cap:
        assert await RepoSource().available(cwd=tmp_path, intent="x", cfg=cfg) is False
    assert "no_evidence" in _skip_reasons(cap)


@pytest.mark.asyncio
async def test_repo_skip_logs_empty_findings_reason(tmp_path: Path) -> None:
    """Explorer 'ran' but produced whitespace-only findings (rate-limited →
    empty .text): skip with reason=empty_findings, not a silent no-op."""
    cfg = default_config().intake
    await _write_explore(tmp_path, findings="   ")
    with capture_logs() as cap:
        assert await RepoSource().available(cwd=tmp_path, intent="x", cfg=cfg) is False
    assert "empty_findings" in _skip_reasons(cap)


@pytest.mark.asyncio
async def test_repo_skip_logs_reuse_disabled_reason(tmp_path: Path) -> None:
    """reuse_explorer_evidence explicitly false → skip reason=reuse_disabled."""
    await _write_explore(tmp_path)
    cfg = default_config().intake
    cfg.reuse_explorer_evidence = False
    with capture_logs() as cap:
        assert await RepoSource().available(cwd=tmp_path, intent="x", cfg=cfg) is False
    assert "reuse_disabled" in _skip_reasons(cap)


# --------------------------------------------------------------------------- #
# Part B — github autonomous discovery + match guard
# --------------------------------------------------------------------------- #


def _bootstrap_remote(monkeypatch: pytest.MonkeyPatch, slug: str = "o/r") -> None:
    monkeypatch.setattr(github_mod, "_gh_available", lambda: True)
    monkeypatch.setattr(github_mod, "_git_remote_slug", lambda cwd: slug)


@pytest.mark.asyncio
async def test_github_autonomous_discovery_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No explicit ref + a matching issue from gh issue list → available True,
    and the discovered #NNN flows into prepare_prompt()."""
    _bootstrap_remote(monkeypatch)
    issues = [
        {
            "number": 199,
            "title": "Mistral 429 rate limit crashes the run",
            "body": "The 429 rate limit error aborts execution mid-task.",
        }
    ]
    monkeypatch.setattr(
        github_mod, "_gh_issue_list", lambda slug, keywords: issues
    )
    cfg = default_config().intake
    src = GitHubSource()
    intent = "Fix the 429 rate limit crash that aborts the run"
    assert await src.available(cwd=tmp_path, intent=intent, cfg=cfg) is True
    frag = await src.prepare_prompt(cwd=tmp_path, intent=intent, cfg=cfg)
    assert "#199" in frag or "199" in frag


@pytest.mark.asyncio
async def test_github_match_guard_discards_unrelated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No explicit ref + an UNRELATED issue (no symptom overlap) → discarded,
    available False (degrade to repo-only), reason logged."""
    _bootstrap_remote(monkeypatch)
    issues = [
        {
            "number": 7,
            "title": "Update the marketing landing page footer",
            "body": "Change the copyright year in the footer banner.",
        }
    ]
    monkeypatch.setattr(
        github_mod, "_gh_issue_list", lambda slug, keywords: issues
    )
    cfg = default_config().intake
    intent = "Fix the 429 rate limit crash that aborts the run mid task"
    assert (
        await GitHubSource().available(cwd=tmp_path, intent=intent, cfg=cfg) is False
    )


@pytest.mark.asyncio
async def test_github_discovery_no_issues_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No explicit ref + gh issue list returns nothing → available False."""
    _bootstrap_remote(monkeypatch)
    monkeypatch.setattr(github_mod, "_gh_issue_list", lambda slug, keywords: [])
    cfg = default_config().intake
    assert (
        await GitHubSource().available(
            cwd=tmp_path, intent="some symptom with no matching issue", cfg=cfg
        )
        is False
    )


@pytest.mark.asyncio
async def test_github_discovery_no_remote_returns_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No explicit ref + no git remote → available False (cannot scope gh)."""
    monkeypatch.setattr(github_mod, "_gh_available", lambda: True)
    monkeypatch.setattr(github_mod, "_git_remote_slug", lambda cwd: None)
    cfg = default_config().intake
    assert (
        await GitHubSource().available(
            cwd=tmp_path, intent="429 rate limit crash", cfg=cfg
        )
        is False
    )


@pytest.mark.asyncio
async def test_github_explicit_ref_still_activates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REGRESSION: an explicit #199 still activates WITHOUT touching discovery."""
    monkeypatch.setattr(github_mod, "_gh_available", lambda: True)

    def _boom(slug: str, keywords: list[str]) -> list[dict]:
        raise AssertionError("discovery must not run when an explicit ref exists")

    monkeypatch.setattr(github_mod, "_gh_issue_list", _boom)
    cfg = default_config().intake
    src = GitHubSource()
    assert (
        await src.available(
            cwd=tmp_path, intent="Fix the 429 bug, see #199", cfg=cfg
        )
        is True
    )
    frag = await src.prepare_prompt(cwd=tmp_path, intent="Fix #199", cfg=cfg)
    assert "#199" in frag


def test_git_remote_slug_parses_https_and_ssh() -> None:
    """The remote parser handles https and ssh URL forms, .git suffix stripped."""
    assert (
        github_mod._slug_from_remote_url("https://github.com/octo/cat.git") == "octo/cat"
    )
    assert (
        github_mod._slug_from_remote_url("git@github.com:octo/cat.git") == "octo/cat"
    )
    assert (
        github_mod._slug_from_remote_url("https://github.com/octo/cat") == "octo/cat"
    )
    assert github_mod._slug_from_remote_url("not a url") is None


def test_symptom_keywords_drops_stopwords() -> None:
    kws = github_mod._symptom_keywords("Fix the 429 rate limit crash that aborts a run")
    assert "429" in kws
    assert "rate" in kws or "limit" in kws or "crash" in kws
    # common stopwords are dropped
    assert "the" not in kws and "that" not in kws and "a" not in kws
