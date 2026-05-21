"""Tests for ``autodev tournament phase-review`` (v0.9.0)."""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cli.commands.tournament import tournament as tournament_group
from config.defaults import default_config
from config.loader import save_config
from state.paths import config_path, plan_path
from state.schemas import (
    AcceptanceCriterion,
    Phase,
    Plan,
    Task,
)


def _iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _seed_plan(cwd: Path, *, baseline_commit: str | None = "abc123") -> None:
    """Write a minimal plan.json with a single phase to ``cwd/.autodev/``."""
    plan = Plan(
        plan_id="p-test",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Investigate",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t",
                        description="d",
                        complexity="medium",
                        status="complete",
                    ),
                ],
                acceptance=[
                    AcceptanceCriterion(
                        id="ph-ac-1", description="all good"
                    )
                ],
                baseline_commit=baseline_commit,
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )
    pp = plan_path(cwd)
    pp.parent.mkdir(parents=True, exist_ok=True)
    pp.write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    save_config(default_config(), config_path(cwd))


@pytest.fixture
def patch_runner(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the runner + adapter detect to keep the test offline."""
    captured: dict[str, Any] = {"called_with": None}

    from orchestrator.phase_review_runner import PhaseReviewOutcome

    async def fake_runner(orch, phase, baseline, tip, spec_md):
        captured["called_with"] = {
            "phase_id": phase.id,
            "baseline": baseline,
            "tip": tip,
            "spec_md": spec_md,
        }
        return PhaseReviewOutcome(
            winner="A",
            accept_phase=True,
            corrective_direction=None,
            history=[],
        )

    async def fake_get_adapter(platform, **_kwargs):
        from stub_adapter import StubAdapter, ok

        # v0.38.0 HK10: get_adapter returns (adapter, selection_meta).
        return StubAdapter({"explorer": ok("ok")}), {"platform": "claude_code"}

    monkeypatch.setattr(
        "orchestrator.phase_review_runner.run_phase_review_tournament",
        fake_runner,
    )
    monkeypatch.setattr("adapters.detect.get_adapter", fake_get_adapter)
    monkeypatch.setattr(
        "adapters.git_utils._git_rev_parse_head", lambda cwd: "tip999"
    )
    return captured


@pytest.fixture
def cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _ledger_init_plan(cwd: Path) -> None:
    """Bootstrap the ledger so PlanManager.load() returns the seeded plan.

    The CLI runs through the orchestrator -> PlanManager.load() chain,
    which ignores the bare plan.json without a matching ledger. This
    helper reuses the existing ``init_plan`` helper to write both pieces.
    """
    import asyncio

    from state.plan_manager import PlanManager

    pm = PlanManager(cwd, session_id="seed")
    plan = Plan(
        plan_id="p-test",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Investigate",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t",
                        description="d",
                        complexity="medium",
                        status="complete",
                    ),
                ],
                acceptance=[
                    AcceptanceCriterion(
                        id="ph-ac-1", description="all good"
                    )
                ],
                baseline_commit="abc123",
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )
    asyncio.run(pm.init_plan(plan))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_phase_review_subcommand_smoke(
    cwd: Path,
    patch_runner: dict[str, Any],
) -> None:
    """``autodev tournament phase-review --phase 1`` exits 0 and invokes
    the runner with the recorded baseline commit."""
    _ledger_init_plan(cwd)
    save_config(default_config(), config_path(cwd))
    runner = CliRunner()
    result = runner.invoke(
        tournament_group, ["phase-review", "--phase", "1"]
    )
    assert result.exit_code == 0, result.output
    assert "phase-review" in result.output.lower()
    # The runner saw the baseline from the plan ("abc123") and tip from
    # the patched git helper ("tip999").
    assert patch_runner["called_with"] == {
        "phase_id": "1",
        "baseline": "abc123",
        "tip": "tip999",
        "spec_md": "",
    }


def test_phase_review_with_explicit_commit_range(
    cwd: Path,
    patch_runner: dict[str, Any],
) -> None:
    """``--baseline`` and ``--tip`` override the recorded values."""
    _ledger_init_plan(cwd)
    save_config(default_config(), config_path(cwd))
    runner = CliRunner()
    result = runner.invoke(
        tournament_group,
        [
            "phase-review",
            "--phase",
            "1",
            "--baseline",
            "deadbeef",
            "--tip",
            "cafebabe",
        ],
    )
    assert result.exit_code == 0, result.output
    assert patch_runner["called_with"]["baseline"] == "deadbeef"
    assert patch_runner["called_with"]["tip"] == "cafebabe"


def test_phase_review_missing_phase_fails_with_exit_2(
    cwd: Path,
    patch_runner: dict[str, Any],
) -> None:
    """Unknown phase id → exit 2 with explanatory message."""
    _ledger_init_plan(cwd)
    save_config(default_config(), config_path(cwd))
    runner = CliRunner()
    result = runner.invoke(
        tournament_group, ["phase-review", "--phase", "999"]
    )
    assert result.exit_code == 2
    assert "999" in result.output


def test_phase_review_default_baseline_uses_phase_baseline_commit(
    cwd: Path,
    patch_runner: dict[str, Any],
) -> None:
    """When ``--baseline`` is omitted, the runner uses
    ``Phase.baseline_commit`` recorded by the orchestrator."""
    _ledger_init_plan(cwd)
    save_config(default_config(), config_path(cwd))
    runner = CliRunner()
    result = runner.invoke(
        tournament_group, ["phase-review", "--phase", "1"]
    )
    assert result.exit_code == 0
    assert patch_runner["called_with"]["baseline"] == "abc123"


def test_phase_review_no_baseline_no_override_fails(
    cwd: Path,
    patch_runner: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When neither phase.baseline_commit nor --baseline is set,
    the CLI exits 2 with an actionable message."""
    # Seed a plan whose phase has no baseline_commit.
    import asyncio

    from state.plan_manager import PlanManager

    pm = PlanManager(cwd, session_id="seed-no-baseline")
    plan = Plan(
        plan_id="p-test",
        spec_hash="0123456789abcdef",
        phases=[
            Phase(
                id="1",
                title="Investigate",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t",
                        description="d",
                        complexity="medium",
                        status="complete",
                    ),
                ],
                baseline_commit=None,
            ),
        ],
        created_at=_iso(),
        updated_at=_iso(),
        complexity="medium",
    )
    asyncio.run(pm.init_plan(plan))
    save_config(default_config(), config_path(cwd))

    runner = CliRunner()
    result = runner.invoke(
        tournament_group, ["phase-review", "--phase", "1"]
    )
    assert result.exit_code == 2
    assert "baseline" in result.output.lower()
