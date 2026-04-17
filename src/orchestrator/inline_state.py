"""Suspend/resume state for the inline (file-based) adapter mode.

When the ``InlineAdapter`` raises ``DelegationPendingSignal``, the
orchestrator writes its current FSM position to ``.autodev/inline-state.json``
and exits.  The agent then reads the delegation, completes the task, writes
a response file, and runs ``autodev resume`` to continue.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from adapters.inline_types import InlineSuspendState
from state.paths import inline_state_path


def write_suspend_state(
    cwd: Path,
    session_id: str,
    pending_task_id: str,
    pending_role: str,
    delegation_path: Path,
    response_path: Path,
    orchestrator_step: str,
    retry_count: int = 0,
    last_issues: list[str] | None = None,
) -> None:
    """Write .autodev/inline-state.json."""
    state = InlineSuspendState(
        session_id=session_id,
        suspended_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
        pending_task_id=pending_task_id,
        pending_role=pending_role,
        delegation_path=str(delegation_path.relative_to(cwd))
        if delegation_path.is_relative_to(cwd)
        else str(delegation_path),
        response_path=str(response_path.relative_to(cwd))
        if response_path.is_relative_to(cwd)
        else str(response_path),
        orchestrator_step=orchestrator_step,  # type: ignore[arg-type]
        retry_count=retry_count,
        last_issues=last_issues or [],
    )
    p = inline_state_path(cwd)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(state.model_dump_json(indent=2), encoding="utf-8")


def load_suspend_state(cwd: Path) -> InlineSuspendState | None:
    """Return the suspend state if present, else None."""
    p = inline_state_path(cwd)
    if not p.exists():
        return None
    return InlineSuspendState.model_validate_json(p.read_text(encoding="utf-8"))


def clear_suspend_state(cwd: Path) -> None:
    """Delete inline-state.json after successful resume."""
    p = inline_state_path(cwd)
    p.unlink(missing_ok=True)


__all__ = ["clear_suspend_state", "load_suspend_state", "write_suspend_state"]
