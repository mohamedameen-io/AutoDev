"""intake_sources gather tests — agent-driven dispatch, parsing, degrade paths.

Mirrors the orchestrator test style (StubAdapter + a bootstrapped git repo).
Covers: parsed GatheredFacts from a stubbed enricher; an unavailable source
(jira with no key / github with no ref) skipped without raising; an unreachable
source (the agent omits a source's facts) degrades to fewer facts not a crash;
the repo source reads explorer evidence via read_evidence (mocked on disk).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.intake_sources import GatherSource, gather_facts, parse_facts_for
from orchestrator.intake_sources.github import GitHubSource, _references
from orchestrator.intake_sources.jira import JiraSource, _jira_keys
from orchestrator.intake_sources.repo import RepoSource
from orchestrator.intake_sources.sessions import SessionSource
from state.evidence import write_evidence
from state.schemas import ExploreEvidence, SpecGaps
from stub_adapter import StubAdapter, ok


def _bootstrap_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True)
    (tmp_path / "main.py").write_text("def main():\n    return 1\n")
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(tmp_path), check=True)


def _make_orch(cwd: Path, adapter: StubAdapter) -> Orchestrator:
    cfg = default_config()
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    registry = build_registry(cfg)
    return Orchestrator(
        cwd=cwd, cfg=cfg, adapter=adapter, registry=registry, session_id="sess-intake"
    )


def _facts_block(*rows: str) -> str:
    body = "\n".join(rows)
    return f"```facts\n{body}\n```\n"


async def _write_explore(cwd: Path, findings: str = "bar() drops a token") -> None:
    ev = ExploreEvidence(
        task_id="plan-explore",
        findings=findings,
        files_referenced=["src/foo.py"],
    )
    await write_evidence(cwd, "plan-explore", ev)


# --------------------------------------------------------------------------- #
# Reference / key detection (pure)
# --------------------------------------------------------------------------- #


def test_github_detects_short_refs_and_urls() -> None:
    short, urls = _references(
        "Fixes #199 and org/repo#42; see https://github.com/o/r/pull/7"
    )
    assert "#199" in short and "#42" in short
    assert "https://github.com/o/r/pull/7" in urls


def test_github_no_reference_means_unavailable_inputs() -> None:
    short, urls = _references("plain prose with no issue reference at all")
    assert short == [] and urls == []


def test_jira_detects_keys_and_denylists_lookalikes() -> None:
    keys = _jira_keys("See PROJ-123 and ABC-9, but ignore GH-5 and ADR-0044")
    assert "PROJ-123" in keys and "ABC-9" in keys
    assert "GH-5" not in keys and "ADR-0044" not in keys


# --------------------------------------------------------------------------- #
# parse_facts_for (shared parser)
# --------------------------------------------------------------------------- #


def test_parse_facts_filters_by_source_and_validates() -> None:
    resp = _facts_block(
        "repo | src/foo.py:10-20 | drops token",
        "github | github:o/r#199 | issue names mechanisms",
        "jira | PROJ-1 | wontfix",
        "malformed line with no pipes",
        "repo |  | empty ref dropped",
    )
    repo_facts = parse_facts_for(resp, "repo")
    assert len(repo_facts) == 1
    assert repo_facts[0].ref == "src/foo.py:10-20"
    assert parse_facts_for(resp, "github")[0].source == "github"
    # malformed + empty-ref rows are silently dropped
    assert parse_facts_for(resp, "jira")[0].ref == "PROJ-1"


def test_parse_facts_no_block_returns_empty() -> None:
    assert parse_facts_for("no fenced block here", "repo") == []


# --------------------------------------------------------------------------- #
# Source availability
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_repo_available_only_with_explorer_evidence(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    cfg = default_config().intake
    src = RepoSource()
    assert await src.available(cwd=tmp_path, intent="x", cfg=cfg) is False
    await _write_explore(tmp_path)
    assert await src.available(cwd=tmp_path, intent="x", cfg=cfg) is True


@pytest.mark.asyncio
async def test_repo_unavailable_when_reuse_disabled(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    await _write_explore(tmp_path)
    cfg = default_config().intake
    cfg.reuse_explorer_evidence = False
    assert await RepoSource().available(cwd=tmp_path, intent="x", cfg=cfg) is False


@pytest.mark.asyncio
async def test_jira_unavailable_without_key(tmp_path: Path) -> None:
    cfg = default_config().intake
    assert await JiraSource().available(cwd=tmp_path, intent="no key", cfg=cfg) is False
    assert (
        await JiraSource().available(cwd=tmp_path, intent="PROJ-7 bug", cfg=cfg) is True
    )


@pytest.mark.asyncio
async def test_github_prompt_carries_exclude_globs(tmp_path: Path) -> None:
    cfg = default_config().intake
    cfg.exclude_globs = ["**/solution/**"]
    frag = await GitHubSource().prepare_prompt(
        cwd=tmp_path, intent="fix #199", cfg=cfg
    )
    assert "**/solution/**" in frag
    assert "EXCLUSION GUARD" in frag


@pytest.mark.asyncio
async def test_session_unavailable_without_prior_sessions(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    cfg = default_config().intake
    assert await SessionSource().available(cwd=tmp_path, intent="x", cfg=cfg) is False
    sess = tmp_path / ".autodev" / "sessions" / "old-sess"
    sess.mkdir(parents=True)
    (sess / "snapshot.json").write_text("{}")
    assert await SessionSource().available(cwd=tmp_path, intent="x", cfg=cfg) is True


# --------------------------------------------------------------------------- #
# gather_facts end-to-end (StubAdapter)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gather_facts_returns_parsed_facts(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    await _write_explore(tmp_path)
    response = _facts_block(
        "repo | src/foo.py:10-20 | bar() drops the trailing token",
        "github | github:o/r#199 | issue names three mechanisms",
    )
    adapter = StubAdapter({"intake_enricher": ok(response)})
    orch = _make_orch(tmp_path, adapter)
    cfg = orch.cfg.intake
    facts = await gather_facts(
        orch,
        cwd=tmp_path,
        intent="Fix the 429 bug, see #199",
        gaps=SpecGaps(ok=False, missing=["acceptance"]),
        cfg=cfg,
    )
    assert {f.source for f in facts} == {"repo", "github"}
    assert any(f.ref == "github:o/r#199" for f in facts)
    # exactly one agent dispatch (single union gather)
    assert adapter.count("intake_enricher") == 1


@pytest.mark.asyncio
async def test_gather_skips_unavailable_jira_without_raising(tmp_path: Path) -> None:
    """jira is in cfg.sources but the intent has no key (MCP-absent analogue):
    it is skipped, gather still returns the available sources' facts, no raise."""
    _bootstrap_repo(tmp_path)
    await _write_explore(tmp_path)
    # Agent emits a jira fact too, but jira was never an active source, so its
    # fragment is absent and its parser is not run — only repo facts come back.
    response = _facts_block(
        "repo | src/foo.py:1 | a repo fact",
        "jira | PROJ-9 | should NOT appear (jira inactive)",
    )
    adapter = StubAdapter({"intake_enricher": ok(response)})
    orch = _make_orch(tmp_path, adapter)
    cfg = orch.cfg.intake
    cfg.sources = ["repo", "jira"]  # jira selected but unavailable (no key)
    facts = await gather_facts(
        orch,
        cwd=tmp_path,
        intent="no issue ref here",  # no #NNN, no PROJ-key
        gaps=SpecGaps(ok=False, missing=["scope"]),
        cfg=cfg,
    )
    assert [f.source for f in facts] == ["repo"]
    assert all(f.source != "jira" for f in facts)


@pytest.mark.asyncio
async def test_gather_no_available_sources_returns_empty(tmp_path: Path) -> None:
    """No explorer evidence, no issue ref, no prior sessions → nothing to gather,
    no agent dispatch, empty list (never blocks)."""
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({"intake_enricher": ok(_facts_block("repo | x:1 | y"))})
    orch = _make_orch(tmp_path, adapter)
    facts = await gather_facts(
        orch,
        cwd=tmp_path,
        intent="plain intent, no references",
        gaps=SpecGaps(ok=False, missing=["scope"]),
        cfg=orch.cfg.intake,
    )
    assert facts == []
    assert adapter.count("intake_enricher") == 0


@pytest.mark.asyncio
async def test_gather_degrades_on_dispatch_exception(tmp_path: Path) -> None:
    """A raising adapter never propagates — gather returns []."""
    _bootstrap_repo(tmp_path)
    await _write_explore(tmp_path)

    def _boom(_inv: object) -> object:
        raise RuntimeError("adapter exploded")

    adapter = StubAdapter({"intake_enricher": _boom})  # type: ignore[dict-item]
    orch = _make_orch(tmp_path, adapter)
    facts = await gather_facts(
        orch,
        cwd=tmp_path,
        intent="anything #1",
        gaps=SpecGaps(ok=True, missing=[]),
        cfg=orch.cfg.intake,
    )
    assert facts == []


@pytest.mark.asyncio
async def test_gather_empty_response_returns_empty(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    await _write_explore(tmp_path)
    adapter = StubAdapter({"intake_enricher": ok("")})
    orch = _make_orch(tmp_path, adapter)
    facts = await gather_facts(
        orch,
        cwd=tmp_path,
        intent="x",
        gaps=SpecGaps(ok=False, missing=["scope"]),
        cfg=orch.cfg.intake,
    )
    assert facts == []


@pytest.mark.asyncio
async def test_repo_source_reads_explorer_evidence_into_prompt(tmp_path: Path) -> None:
    """repo source reuses explorer evidence (read_evidence) — its fragment
    carries the findings + 'do NOT re-explore', and the dispatched prompt does
    too. Confirms KD5 (no second exploration pass)."""
    _bootstrap_repo(tmp_path)
    await _write_explore(tmp_path, findings="UNIQUE_FINDING_MARKER in bar()")
    frag = await RepoSource().prepare_prompt(
        cwd=tmp_path, intent="x", cfg=default_config().intake
    )
    assert "UNIQUE_FINDING_MARKER" in frag
    # Newline-insensitive: the "do not re-explore" instruction may wrap.
    assert "re-explore" in frag.lower() and "already been gathered" in frag.lower()

    # And end-to-end the dispatched prompt includes the findings.
    adapter = StubAdapter({"intake_enricher": ok(_facts_block("repo | src/foo.py:1 | f"))})
    orch = _make_orch(tmp_path, adapter)
    cfg = orch.cfg.intake
    cfg.sources = ["repo"]
    await gather_facts(
        orch, cwd=tmp_path, intent="x", gaps=SpecGaps(ok=False, missing=["scope"]), cfg=cfg
    )
    prompts = adapter.prompts_for("intake_enricher")
    assert prompts and "UNIQUE_FINDING_MARKER" in prompts[0]


def test_sources_satisfy_protocol() -> None:
    for src in (RepoSource(), GitHubSource(), JiraSource(), SessionSource()):
        assert isinstance(src, GatherSource)
