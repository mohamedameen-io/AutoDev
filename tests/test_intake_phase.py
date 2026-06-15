"""run_intake_phase + spec_validator.assess tests (ADR-0045).

Mirrors the framing_phase / intake_sources test style: a bootstrapped git repo +
StubAdapter, no network. Covers:
- spec_validator.assess gap detection per dimension + validate_spec_text back-compat.
- run_intake_phase: pass-through (well-formed → +0 dispatch), gap path (enrich +
  clarify + lock + IntakeEvidence), on_unanswered matrix, deterministic resume
  (0 dispatches), flag-guard + fail-safe degrade, exclude_globs threaded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator
from orchestrator.intake_phase import (
    IntakeOutcome,
    _apply_headless_policy,
    _parse_questions,
    _render_locked_spec,
    run_intake_phase,
)
from orchestrator.spec_validator import (
    SpecGaps,
    assess,
    validate_spec_text,
)
from state.evidence import read_evidence, write_evidence
from state.ledger import read_entries
from state.paths import spec_path
from state.schemas import ClarifyingAnswer, IntakeEvidence
from stub_adapter import StubAdapter, ok


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #


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
        cwd=cwd,
        cfg=cfg,
        adapter=adapter,
        registry=registry,
        session_id="sess-intake-phase",
    )


_THIN_INTENT = "fix the 429 bug"

_ENRICHED_SPEC = (
    "# Bug: Mistral 429 rate-limit crashes the run\n\n"
    "The retry loop in src/foo.py:120 does not back off (github:org/repo#199).\n\n"
    "## Success criteria\n"
    "- The run survives a 429 without crashing (github:org/repo#199).\n"
)

_QUESTIONS_BLOCK = (
    "```questions\n"
    "- id: provider\n"
    "  question: Must we stay on the current provider, or is swapping ok?\n"
    "  kind: constraint\n"
    "  options: [Stay on Mistral, Swap allowed, Let AutoDev decide]\n"
    "  recommended: Stay on Mistral\n"
    "- id: donebar\n"
    "  question: What is the done-bar for this fix?\n"
    "  kind: done_bar\n"
    "  options: [Passing tests, Also a deploy]\n"
    "  recommended: Passing tests\n"
    "```\n"
)


def _gap_path_adapter() -> StubAdapter:
    """A StubAdapter whose enricher/clarifier produce a deterministic gap path.

    The intake_enricher role is dispatched twice on the gap path: once for the
    agent-driven GATHER (emits a ```facts block) and once for ENRICH (emits the
    spec). The repo source is the only active gather source (explorer evidence is
    seeded in the test), so the facts block carries a ``repo`` row.
    """
    return StubAdapter(
        {
            "intake_enricher": [
                ok(
                    "```facts\n"
                    "repo | src/foo.py:120 | retry loop never backs off on a 429\n"
                    "```\n"
                ),
                ok(_ENRICHED_SPEC),
            ],
            "intake_clarifier": ok(_QUESTIONS_BLOCK),
        }
    )


async def _seed_explore(cwd: Path) -> None:
    """Seed plan-explore evidence so the repo gather source is available."""
    from state.schemas import ExploreEvidence

    ev = ExploreEvidence(
        task_id="plan-explore",
        findings="retry loop in src/foo.py:120 never backs off on a 429",
        files_referenced=["src/foo.py"],
    )
    await write_evidence(cwd, "plan-explore", ev)


# --------------------------------------------------------------------------- #
# spec_validator.assess — gap detection + back-compat
# --------------------------------------------------------------------------- #


def test_assess_thin_intent_reports_all_dimensions() -> None:
    gaps = assess(_THIN_INTENT)
    assert gaps.ok is False
    # thin intent: too short (scope), no acceptance, no constraint, no touchpoint.
    assert set(gaps.missing) == {"scope", "acceptance", "constraints", "touchpoints"}


def test_assess_well_formed_spec_has_no_gaps() -> None:
    spec = (
        "# Bug: parser drops trailing token in src/foo.py\n\n"
        "The bar() function in src/foo.py:120 must not drop the trailing token.\n"
        "Expected: the token is preserved. Acceptance: a regression test passes.\n"
        "We must stay backward compatible and cannot break the existing API.\n"
    )
    gaps = assess(spec)
    assert gaps.ok is True
    assert gaps.missing == []


def test_assess_empty_reports_everything() -> None:
    gaps = assess("   \n  ")
    assert gaps.ok is False
    assert set(gaps.missing) == {"scope", "acceptance", "constraints", "touchpoints"}


def test_assess_detects_missing_acceptance_only() -> None:
    # Has scope (refactor), a constraint (cannot/backward-compat), a touchpoint
    # (src/foo.py), but NO acceptance/success signal (avoid must/should which are
    # acceptance markers).
    spec = (
        "Refactor the retry logic in src/foo.py to stop crashing on a 429.\n"
        "We cannot break the public client API and need backward compatibility.\n"
    )
    gaps = assess(spec)
    assert "acceptance" in gaps.missing
    assert "scope" not in gaps.missing
    assert "constraints" not in gaps.missing
    assert "touchpoints" not in gaps.missing


def test_assess_detects_missing_touchpoints_only() -> None:
    # Long, scoped, has acceptance + constraint, but names no concrete location.
    spec = (
        "We need to fix a recurring crash that happens under load.\n"
        "Expected outcome: the system stays up. We must not break compatibility "
        "with the current release, and the result should pass the suite.\n"
    )
    gaps = assess(spec)
    assert "touchpoints" in gaps.missing
    assert "scope" not in gaps.missing
    assert "acceptance" not in gaps.missing


def test_validate_spec_text_back_compat_tracks_assess_ok() -> None:
    # The binary wrapper's ok must NOT regress: still passes a G1-valid spec and
    # rejects a thin one (assess.ok is a strict superset of G1's reasons, but the
    # well-formed/thin extremes must agree).
    wf = (
        "# Bug: crash on refresh\n\n"
        "The widget crashes on refresh. Expected: it should not crash.\n"
        "Acceptance: regression test passes.\n"
    )
    assert validate_spec_text(wf).ok is True
    assert validate_spec_text("fix bug").ok is False


# --------------------------------------------------------------------------- #
# question parser + headless policy (pure)
# --------------------------------------------------------------------------- #


def test_parse_questions_caps_and_validates() -> None:
    qs = _parse_questions(_QUESTIONS_BLOCK, max_questions=1)
    assert len(qs) == 1
    assert qs[0].id == "provider"
    assert qs[0].recommended in qs[0].options


def test_parse_questions_recommended_not_in_options_falls_back() -> None:
    block = (
        "```questions\n"
        "- id: x\n"
        "  question: a constraint?\n"
        "  kind: constraint\n"
        "  options: [A, B]\n"
        "  recommended: Z\n"  # not in options
        "```\n"
    )
    qs = _parse_questions(block, max_questions=4)
    assert len(qs) == 1
    assert qs[0].recommended == "A"  # fell back to first option


def test_parse_questions_empty_block() -> None:
    assert _parse_questions("```questions\n```\n", 4) == []
    assert _parse_questions("no block here", 4) == []


def test_apply_headless_assume_defaults_fills_recommended() -> None:
    qs = _parse_questions(_QUESTIONS_BLOCK, 4)
    answers, assumptions, blocked = _apply_headless_policy(qs, "assume_defaults")
    assert blocked is False
    assert [a.answer for a in answers] == ["Stay on Mistral", "Passing tests"]
    assert all(a.source == "default_assumed" for a in answers)
    assert len(assumptions) == 2


def test_apply_headless_block_assumes_nothing() -> None:
    qs = _parse_questions(_QUESTIONS_BLOCK, 4)
    answers, assumptions, blocked = _apply_headless_policy(qs, "block")
    assert blocked is True
    assert answers == []
    assert assumptions == []


def test_render_locked_spec_appends_answered_constraints() -> None:
    answers = [
        ClarifyingAnswer(
            question_id="provider", answer="Stay on Mistral", source="default_assumed"
        )
    ]
    locked = _render_locked_spec(_ENRICHED_SPEC, answers)
    assert "## Answered constraints" in locked
    assert "provider: Stay on Mistral (assumed default)" in locked


# --------------------------------------------------------------------------- #
# run_intake_phase — pass-through fast path
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_passthrough_well_formed_zero_dispatch(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    spec = (
        "# Bug: crash on refresh in src/foo.py\n\n"
        "The bar() widget crashes on refresh; it must not crash.\n"
        "Expected: clean refresh. Acceptance: regression test passes.\n"
        "We cannot break backward compatibility.\n"
    )
    outcome = await run_intake_phase(orch, spec)
    assert isinstance(outcome, IntakeOutcome)
    assert outcome.passthrough is True
    assert outcome.degraded is False
    # +0 LLM dispatches on the well-formed path.
    assert adapter.calls == []
    # spec.md was locked with the raw (well-formed) intent.
    assert spec_path(tmp_path).read_text().strip() == spec.strip()
    ev = await read_evidence(tmp_path, "plan-intake", "intake")
    assert isinstance(ev, IntakeEvidence)
    assert ev.gathered == [] and ev.questions == []


# --------------------------------------------------------------------------- #
# run_intake_phase — gap path (enrich + clarify + lock + evidence)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_gap_path_enriches_locks_and_writes_evidence(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    await _seed_explore(tmp_path)
    adapter = _gap_path_adapter()
    orch = _make_orch(tmp_path, adapter)
    outcome = await run_intake_phase(orch, _THIN_INTENT)

    assert outcome.passthrough is False
    assert outcome.degraded is False
    # Enriched spec is the locked one (with the Answered-constraints section).
    assert "## Success criteria" in outcome.spec
    assert "## Answered constraints" in outcome.spec
    # spec.md was rewritten with the enriched + answered spec.
    locked = spec_path(tmp_path).read_text()
    assert "Mistral 429 rate-limit" in locked
    assert "provider: Stay on Mistral" in locked

    ev = await read_evidence(tmp_path, "plan-intake", "intake")
    assert isinstance(ev, IntakeEvidence)
    assert ev.raw_intent == _THIN_INTENT
    assert ev.gaps.ok is False
    assert len(ev.gathered) == 1 and ev.gathered[0].source == "repo"
    assert len(ev.questions) == 2
    # headless default → assumed answers recorded.
    assert all(a.source == "default_assumed" for a in ev.answers)
    assert len(ev.assumptions) == 2
    assert ev.locked_spec_hash == outcome.spec_hash


@pytest.mark.asyncio
async def test_gap_path_emits_intake_ledger_ops(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    await _seed_explore(tmp_path)
    orch = _make_orch(tmp_path, _gap_path_adapter())
    await run_intake_phase(orch, _THIN_INTENT)
    ops = [e.op for e in read_entries(tmp_path)]
    for expected in (
        "intake_assessed",
        "intake_gathered",
        "intake_enriched",
        "intake_questions_posed",
        "intake_defaults_assumed",
        "spec_locked",
    ):
        assert expected in ops, f"missing ledger op {expected}; got {ops}"


# --------------------------------------------------------------------------- #
# on_unanswered matrix
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_on_unanswered_assume_defaults_never_hangs(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    await _seed_explore(tmp_path)
    orch = _make_orch(tmp_path, _gap_path_adapter())
    orch.cfg.intake.on_unanswered = "assume_defaults"
    outcome = await run_intake_phase(orch, _THIN_INTENT)
    # Completes deterministically with assumed defaults — no operator needed.
    assert outcome.assumptions
    assert outcome.degraded is False


@pytest.mark.asyncio
async def test_on_unanswered_block_locks_without_assumptions(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    await _seed_explore(tmp_path)
    orch = _make_orch(tmp_path, _gap_path_adapter())
    orch.cfg.intake.on_unanswered = "block"
    outcome = await run_intake_phase(orch, _THIN_INTENT)
    # block: no defaults assumed; intake still locks the enriched spec so planning
    # is never wedged (the standalone CLI surface is where fail exits non-zero).
    assert outcome.assumptions == []
    ev = await read_evidence(tmp_path, "plan-intake", "intake")
    assert isinstance(ev, IntakeEvidence)
    assert ev.answers == []
    assert len(ev.questions) == 2


# --------------------------------------------------------------------------- #
# deterministic resume — 0 dispatches
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resume_reads_evidence_zero_dispatch(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    # Pre-seed evidence as if a prior run already locked the spec.
    seeded = IntakeEvidence(
        task_id="plan-intake",
        raw_intent=_THIN_INTENT,
        gaps=SpecGaps(ok=False, missing=["scope"]),
        gathered=[],
        enriched_spec=_ENRICHED_SPEC,
        questions=[],
        answers=[],
        assumptions=["provider: assumed 'Stay on Mistral'"],
        locked_spec_hash="deadbeefdeadbeef",
        sources_used=["repo"],
        excluded_globs=[],
    )
    await write_evidence(tmp_path, "plan-intake", seeded)

    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    outcome = await run_intake_phase(orch, _THIN_INTENT)
    assert outcome.spec == _ENRICHED_SPEC
    assert outcome.spec_hash == "deadbeefdeadbeef"
    assert outcome.assumptions == ["provider: assumed 'Stay on Mistral'"]
    # Resume must NOT dispatch.
    assert adapter.calls == []


# --------------------------------------------------------------------------- #
# flag-guard + fail-safe degrade
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_disabled_returns_raw_intent_degraded(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    orch.cfg.intake.enabled = False
    outcome = await run_intake_phase(orch, _THIN_INTENT)
    assert outcome.degraded is True
    assert outcome.spec == _THIN_INTENT
    assert adapter.calls == []
    # disabled is a no-op: no evidence written, no spec.md side-effect.
    assert await read_evidence(tmp_path, "plan-intake", "intake") is None


@pytest.mark.asyncio
async def test_kill_switch_env_disables(tmp_path: Path, monkeypatch) -> None:
    _bootstrap_repo(tmp_path)
    monkeypatch.setenv("AUTODEV_INTAKE_DISABLED", "1")
    adapter = StubAdapter({})
    orch = _make_orch(tmp_path, adapter)
    outcome = await run_intake_phase(orch, _THIN_INTENT)
    assert outcome.degraded is True
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_raising_adapter_degrades_to_raw_intent(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)

    class _Boom(StubAdapter):
        async def execute(self, inv):  # type: ignore[override]
            raise RuntimeError("enricher exploded")

    adapter = _Boom({})
    orch = _make_orch(tmp_path, adapter)
    # The thin intent forces the gap path; the enricher dispatch raises.
    outcome = await run_intake_phase(orch, _THIN_INTENT)
    # Fail-safe: degrade to the raw intent, never propagate.
    assert outcome.degraded is True
    assert outcome.spec == _THIN_INTENT


# --------------------------------------------------------------------------- #
# exclude_globs contamination guard threaded into evidence
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_exclude_globs_threaded_into_evidence(tmp_path: Path) -> None:
    _bootstrap_repo(tmp_path)
    await _seed_explore(tmp_path)
    orch = _make_orch(tmp_path, _gap_path_adapter())
    orch.cfg.intake.exclude_globs = ["**/solution/**", "PR-200"]
    await run_intake_phase(orch, _THIN_INTENT)
    ev = await read_evidence(tmp_path, "plan-intake", "intake")
    assert isinstance(ev, IntakeEvidence)
    assert ev.excluded_globs == ["**/solution/**", "PR-200"]
