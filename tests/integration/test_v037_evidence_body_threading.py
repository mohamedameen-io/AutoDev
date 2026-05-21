"""v0.37.0 H1 integration: reviewer body reaches the architect-consult prompt.

End-to-end check that the helper added in
:func:`orchestrator.execute_phase._build_recent_evidence_block` actually
threads the reviewer's ``raw_response`` body into the prompt sent to
``architect_b`` during a stuck-recovery dispatch — not just the
``NEEDS_CHANGES`` verdict token.

The motivating failure mode (from a recent enterprise-codebase
stuck-recovery retrospective): sounding-board and architect-consult
prompts received only the verdict token plus a one-line reason, so
refinement decisions degenerated to coin flips. This test pins the fix
so future refactors cannot silently regress.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from adapters.types import AgentResult
from orchestrator import execute_phase as execute_phase_mod
from orchestrator.execute_phase import _dispatch_architect_consult
from state.evidence import write_evidence
from state.schemas import ReviewEvidence, Task, TestEvidence


@dataclass
class _FakeCfg:
    recent_evidence_max_chars_per_kind: int = 4000
    recent_evidence_include_kinds: list[str] = field(
        default_factory=lambda: ["review", "test", "developer"]
    )
    # v0.38.0 HK3: gate the architect-consult envelope dump. Default-on
    # in real config; left True here so the integration test exercises
    # the dump path end-to-end.
    dump_architect_consult_envelopes: bool = True


@dataclass
class _FakePlanManager:
    ledger_ops: list[tuple[str, dict]] = field(default_factory=list)
    increment_calls: list[str] = field(default_factory=list)

    async def increment_architect_consult(self, task_id: str) -> None:
        self.increment_calls.append(task_id)

    async def ledger_append(self, op: str, payload: dict) -> None:
        self.ledger_ops.append((op, payload))

    async def mark_escalated(self, task_id: str) -> None:
        return None

    async def update_task_status(
        self, task_id: str, status: str, meta: dict[str, Any] | None = None
    ) -> Task:
        return Task(
            id=task_id,
            phase_id="1",
            title="t",
            description="d",
            files=[],
            status=status,  # type: ignore[arg-type]
        )

    async def get_task(self, task_id: str) -> Task | None:
        return Task(
            id=task_id,
            phase_id="1",
            title="t",
            description="d",
            files=[],
        )

    async def load(self) -> None:
        return None


@dataclass
class _FakeOrch:
    cwd: Path
    cfg: _FakeCfg
    plan_manager: _FakePlanManager


@pytest.mark.asyncio
async def test_architect_consult_prompt_contains_reviewer_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer that emitted a substantive NEEDS_CHANGES body should
    see that body land in the architect-consult prompt, prefixed with
    the ``REVIEWER_RAW:`` section header — not just the verdict token."""
    task = Task(
        id="2.4",
        phase_id="1",
        title="add null-guard",
        description="protect call site against null caller-provided handle",
        files=["src/orch/x.py"],
    )

    reviewer_body = (
        "VERDICT: NEEDS_CHANGES\n"
        "Issues:\n"
        "- The patch adds the guard at line 42, but the caller at line 88 "
        "still passes the unchecked value through the public surface; the "
        "fix needs to be at the boundary, not the deepest call site.\n"
        "- No regression test covers the boundary case.\n"
    )
    await write_evidence(
        tmp_path,
        task.id,
        ReviewEvidence(
            task_id=task.id,
            verdict="NEEDS_CHANGES",
            issues=["fix at boundary", "missing test"],
            output_text=reviewer_body,
            raw_response=reviewer_body,
        ),
    )
    # Add test evidence too, so we also exercise the multi-kind path.
    test_body = "RESULTS: passed=10 failed=2 total=12\nfailures: test_null_at_boundary\n"
    await write_evidence(
        tmp_path,
        task.id,
        TestEvidence(
            task_id=task.id,
            passed=10,
            failed=2,
            total=12,
            output_text=test_body,
            raw_response=test_body,
        ),
    )

    captured: dict[str, str] = {}

    async def _fake_delegate(
        orch: Any,
        role: str,
        envelope: Any,
        extra_context: str = "",
        **kwargs: Any,
    ) -> AgentResult:
        captured["role"] = role
        captured["extra_context"] = extra_context
        return AgentResult(
            text="RESOLUTION: continue",
            success=True,
            files_changed=[],
            duration_s=0.1,
        )

    monkeypatch.setattr(execute_phase_mod, "delegate", _fake_delegate)

    # Stuck-state stub — only the attribute lookups in the helper matter.
    @dataclass
    class _StuckState:
        discard_count: int = 1
        pivot_count: int = 0
        search_count: int = 0
        architect_count: int = 0
        last_event: str = "reviewer NEEDS_CHANGES"

    orch = _FakeOrch(
        cwd=tmp_path,
        cfg=_FakeCfg(),
        plan_manager=_FakePlanManager(),
    )

    await _dispatch_architect_consult(
        orch,  # type: ignore[arg-type]
        task,
        stuck_state=_StuckState(),
        reason="reviewer NEEDS_CHANGES",
        prior_attempts=["attempt 1: guard at line 42 — REJECTED"],
        web_context_block="",
    )

    assert "extra_context" in captured, "delegate was never called"
    ctx = captured["extra_context"]

    # The verdict token alone is not enough; the architect must see the
    # reviewer's body to refine on substance.
    assert "REVIEWER_RAW:" in ctx, (
        "architect-consult prompt missing REVIEWER_RAW: section — "
        "reviewer body was not threaded through recent_evidence"
    )
    assert "fix needs to be at the boundary" in ctx, (
        "architect-consult prompt missing reviewer body content; "
        "the helper is sending only the verdict / reason"
    )
    # Multi-kind threading should also include the test body.
    assert "TEST_RAW:" in ctx
    assert "test_null_at_boundary" in ctx
    # And the reason still propagates — operators rely on grepping for it.
    assert "reviewer NEEDS_CHANGES" in ctx

    # v0.38.0 HK3: the architect-consult envelope dump landed on disk
    # so post-mortems can grep `.autodev/debug/architect_consult-*.json`
    # for "what did we ask the architect about this task?".
    import json as _json

    dump_dir = tmp_path / ".autodev" / "debug"
    assert dump_dir.exists(), "HK3: .autodev/debug/ directory not created"
    dumps = sorted(dump_dir.glob(f"architect_consult-{task.id}-*.json"))
    assert dumps, (
        "HK3: no architect-consult envelope dump landed in "
        ".autodev/debug/ — the helper is gated off or failed silently"
    )
    payload = _json.loads(dumps[-1].read_text(encoding="utf-8"))
    assert payload["task_id"] == task.id
    assert payload["phase_id"] == task.phase_id
    assert payload["reason"] == "reviewer NEEDS_CHANGES"
    # The reviewer body must be in the dumped recent_evidence too — same
    # contract as the live prompt thread.
    assert "REVIEWER_RAW:" in payload["recent_evidence"]
    assert "fix needs to be at the boundary" in payload["recent_evidence"]
    # prior_attempts thread-through.
    assert payload["prior_attempts"] == [
        "attempt 1: guard at line 42 — REJECTED"
    ]


@pytest.mark.asyncio
async def test_architect_consult_dump_disabled_by_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.38.0 HK3: ``dump_architect_consult_envelopes=False`` skips
    the dump cleanly — no debug file, no exception."""
    task = Task(
        id="3.7",
        phase_id="1",
        title="t",
        description="d",
        files=[],
    )

    async def _fake_delegate(
        orch: Any,
        role: str,
        envelope: Any,
        extra_context: str = "",
        **kwargs: Any,
    ) -> AgentResult:
        return AgentResult(
            text="RESOLUTION: continue",
            success=True,
            files_changed=[],
            duration_s=0.1,
        )

    monkeypatch.setattr(execute_phase_mod, "delegate", _fake_delegate)

    @dataclass
    class _StuckState:
        discard_count: int = 0
        pivot_count: int = 0
        search_count: int = 0
        architect_count: int = 0
        last_event: str = ""

    cfg = _FakeCfg()
    cfg.dump_architect_consult_envelopes = False
    orch = _FakeOrch(cwd=tmp_path, cfg=cfg, plan_manager=_FakePlanManager())

    await _dispatch_architect_consult(
        orch,  # type: ignore[arg-type]
        task,
        stuck_state=_StuckState(),
        reason="reviewer NEEDS_CHANGES",
        prior_attempts=None,
        web_context_block="",
    )

    dump_dir = tmp_path / ".autodev" / "debug"
    if dump_dir.exists():
        assert not list(dump_dir.glob(f"architect_consult-{task.id}-*.json")), (
            "HK3: dump landed despite dump_architect_consult_envelopes=False"
        )
