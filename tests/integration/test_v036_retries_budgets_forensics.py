"""v0.36.0 integration test: G1 + D1 + D3 + E1/E2 + F1/F2/F3 in one sequence.

The full happy path stitches:
  - G1: spec_validator passes a well-formed bug.md fixture.
  - D1: rejection diagnosis groups by class.
  - D3: structural retry routes to sonnet.
  - E1/E2: huge-repo + retry-attempt budget scaling.
  - F1: per-attempt + per-recovery-tier ledger ops emitted.
  - F2: NetworkProbeFailure surfaced with .suggestion.
  - F3: status --blocked reads the ledger and renders the surface.

These cross-finding tests are deliberately lightweight — each component
is exercised in isolation; the integration test only validates that the
public surfaces compose without import / config errors.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from rich.console import Console

from adapters.base import NetworkProbeFailure
from orchestrator.file_existence_validator import _classify_rejection
from orchestrator.plan_phase_recovery import should_change_model_for_class
from orchestrator.retry_envelope import (
    diagnosis_for_class,
)
from orchestrator.spec_validator import validate_spec_text
from tournament.task_overrides import resolve_task_max_turns


_BUG_FIXTURE = """\
# Bug: dashboard renders wrong color after refresh

The dashboard widget shows the wrong color after the user clicks the
refresh button. Expected behavior is the widget renders the active
color from settings.

## Acceptance
- [ ] Widget renders the active color after refresh.
"""


def test_full_v036_happy_path(tmp_path: Path) -> None:
    """G1 → D1 → D3 → E1 → F1 → F3 — all pieces compose."""
    # G1: well-formed spec accepted.
    spec_result = validate_spec_text(_BUG_FIXTURE)
    assert spec_result.ok, spec_result.reasons

    # D1: a `.md` deliverable under notes/ classifies as
    # ``new_md_deliverable`` and the diagnosis paragraph mentions both
    # action options.
    assert _classify_rejection("notes/foo.md") == "new_md_deliverable"
    assert "[new]" in diagnosis_for_class("new_md_deliverable")

    # D3: opus + missing_on_disk routes to sonnet.
    assert (
        should_change_model_for_class(
            "claude-opus-4-7", "missing_on_disk", "sonnet"
        )
        == "sonnet"
    )

    # E1: role-keyed huge-repo dict is populated by default.
    from config.schema import TaskOverridesConfig

    cfg = TaskOverridesConfig()
    assert cfg.huge_repo_multipliers["explorer"] == 3.0

    # E2: retry-attempt 2 doubles the budget.
    from state.schemas import Task

    task = Task(
        id="1.1",
        phase_id="1",
        title="t",
        description="d",
        files=[],
        complexity="medium",
        acceptance=[],
    )
    assert (
        resolve_task_max_turns(task, spec_default=10, retry_attempt=2) == 40
    )

    # F1 + F3: render the recovery-tier section from a synthesised ledger.
    (tmp_path / ".autodev").mkdir()
    (tmp_path / ".autodev" / "plan-ledger.jsonl").write_text(
        json.dumps(
            {
                "op": "recovery_tier_attempted",
                "payload": {
                    "tier": 4,
                    "outcome": "applied",
                    "reason": "recurrent_path_failure",
                    "from_state": None,
                    "to_state": "dropped:notes/foo.md",
                },
            }
        )
        + "\n"
    )
    from cli.commands.status import _collect_recovery_outcomes, _render_recovery_outcomes

    rows = _collect_recovery_outcomes(tmp_path / ".autodev" / "plan-ledger.jsonl")
    assert len(rows) == 1
    out = StringIO()
    console = Console(file=out, force_terminal=False)
    _render_recovery_outcomes(console, rows)
    rendered = out.getvalue()
    assert "Recovery Tier Outcomes" in rendered

    # F2: structured exception carries a suggestion field.
    exc = NetworkProbeFailure(
        adapter="claude_code",
        attempts=3,
        last_error="connection reset",
        suggestion="check VPN",
    )
    assert exc.suggestion == "check VPN"


def test_status_blocked_after_simulated_failure(tmp_path: Path) -> None:
    """A simulated failure run produces ledger ops that F3 surfaces."""
    (tmp_path / ".autodev" / "debug").mkdir(parents=True)
    (tmp_path / ".autodev" / "plan-ledger.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "op": "architect_attempt_failed",
                    "payload": {
                        "attempt": 1,
                        "model": "claude-opus-4-7",
                        "duration_s": 2.1,
                        "rejection_count": 3,
                        "primary_class": "new_md_deliverable",
                    },
                },
                {
                    "op": "path_rejection_recorded",
                    "payload": {
                        "task_id": "",
                        "path": "notes/foo.md",
                        "class": "new_md_deliverable",
                    },
                },
            ]
        )
        + "\n"
    )
    (tmp_path / ".autodev" / "debug" / "architect-failed-1000.md").write_text(
        "# rejected\n"
    )

    from cli.commands.status import _collect_recovery_outcomes, _find_architect_dumps
    from state.paths import autodev_root, ledger_path

    rows = _collect_recovery_outcomes(ledger_path(tmp_path))
    dumps = _find_architect_dumps(autodev_root(tmp_path))
    assert any(r["op"] == "architect_attempt_failed" for r in rows)
    assert any(r["op"] == "path_rejection_recorded" for r in rows)
    assert len(dumps) == 1
    # The most-recent rejection's class is surfaced via the diagnosis lib.
    rejection_class = rows[-1]["payload"]["class"]
    assert "[new]" in diagnosis_for_class(rejection_class)
