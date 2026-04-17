"""Centralized ``.autodev/`` filesystem layout constants + path builders.

All file-system I/O in :mod:`state` and :mod:`orchestrator`
routes through this module so the on-disk layout is defined in exactly one
place. See section E of the implementation plan.
"""

from __future__ import annotations

from pathlib import Path


AUTODEV_DIR: str = ".autodev"
CONFIG_FILE: str = "config.json"
SPEC_FILE: str = "spec.md"
PLAN_FILE: str = "plan.json"
LEDGER_FILE: str = "plan-ledger.jsonl"
KNOWLEDGE_FILE: str = "knowledge.jsonl"
REJECTED_LESSONS_FILE: str = "rejected_lessons.jsonl"
EVIDENCE_DIR: str = "evidence"
TOURNAMENTS_DIR: str = "tournaments"
SESSIONS_DIR: str = "sessions"
LOCK_FILE: str = ".lock"


def autodev_root(cwd: Path) -> Path:
    """Return the absolute path to ``<cwd>/.autodev/``."""
    return Path(cwd) / AUTODEV_DIR


def ensure_autodev_dir(cwd: Path) -> Path:
    """Create ``.autodev/`` (and its parents) if missing. Returns the root."""
    root = autodev_root(cwd)
    root.mkdir(parents=True, exist_ok=True)
    return root


def config_path(cwd: Path) -> Path:
    return autodev_root(cwd) / CONFIG_FILE


def spec_path(cwd: Path) -> Path:
    return autodev_root(cwd) / SPEC_FILE


def plan_path(cwd: Path) -> Path:
    return autodev_root(cwd) / PLAN_FILE


def ledger_path(cwd: Path) -> Path:
    return autodev_root(cwd) / LEDGER_FILE


def knowledge_path(cwd: Path) -> Path:
    return autodev_root(cwd) / KNOWLEDGE_FILE


def rejected_lessons_path(cwd: Path) -> Path:
    return autodev_root(cwd) / REJECTED_LESSONS_FILE


def lock_path(cwd: Path) -> Path:
    return autodev_root(cwd) / LOCK_FILE


def evidence_dir(cwd: Path) -> Path:
    return autodev_root(cwd) / EVIDENCE_DIR


def evidence_path(cwd: Path, task_id: str, kind: str) -> Path:
    """Return ``.autodev/evidence/{task_id}-{kind}.json``.

    ``task_id`` is used verbatim; callers are responsible for keeping ids to
    a filesystem-safe charset (they already do, e.g. ``"1.1"``).
    """
    return evidence_dir(cwd) / f"{task_id}-{kind}.json"


def patch_path(cwd: Path, task_id: str) -> Path:
    """Return ``.autodev/evidence/{task_id}.patch`` — raw unified diff text."""
    return evidence_dir(cwd) / f"{task_id}.patch"


def tournaments_dir(cwd: Path) -> Path:
    return autodev_root(cwd) / TOURNAMENTS_DIR


def sessions_dir(cwd: Path) -> Path:
    return autodev_root(cwd) / SESSIONS_DIR


def session_events_path(cwd: Path, session_id: str) -> Path:
    """Return ``.autodev/sessions/<session_id>/events.jsonl``."""
    return sessions_dir(cwd) / session_id / "events.jsonl"


def session_snapshot_path(cwd: Path, session_id: str) -> Path:
    """Return ``.autodev/sessions/<session_id>/snapshot.json``."""
    return sessions_dir(cwd) / session_id / "snapshot.json"


# Inline adapter paths.
DELEGATIONS_DIR: str = "delegations"
RESPONSES_DIR: str = "responses"
INLINE_STATE_FILE: str = "inline-state.json"


def delegations_dir(cwd: Path) -> Path:
    """Return ``.autodev/delegations/``."""
    return autodev_root(cwd) / DELEGATIONS_DIR


def delegation_path(cwd: Path, task_id: str, role: str) -> Path:
    """Return ``.autodev/delegations/{task_id}-{role}.md``."""
    return delegations_dir(cwd) / f"{task_id}-{role}.md"


def responses_dir(cwd: Path) -> Path:
    """Return ``.autodev/responses/``."""
    return autodev_root(cwd) / RESPONSES_DIR


def response_path(cwd: Path, task_id: str, role: str) -> Path:
    """Return ``.autodev/responses/{task_id}-{role}.json``."""
    return responses_dir(cwd) / f"{task_id}-{role}.json"


def inline_state_path(cwd: Path) -> Path:
    """Return ``.autodev/inline-state.json``."""
    return autodev_root(cwd) / INLINE_STATE_FILE


__all__ = [
    "AUTODEV_DIR",
    "CONFIG_FILE",
    "DELEGATIONS_DIR",
    "EVIDENCE_DIR",
    "INLINE_STATE_FILE",
    "KNOWLEDGE_FILE",
    "LEDGER_FILE",
    "LOCK_FILE",
    "PLAN_FILE",
    "REJECTED_LESSONS_FILE",
    "RESPONSES_DIR",
    "SESSIONS_DIR",
    "SPEC_FILE",
    "TOURNAMENTS_DIR",
    "autodev_root",
    "config_path",
    "delegation_path",
    "delegations_dir",
    "ensure_autodev_dir",
    "evidence_dir",
    "evidence_path",
    "inline_state_path",
    "knowledge_path",
    "ledger_path",
    "lock_path",
    "patch_path",
    "plan_path",
    "rejected_lessons_path",
    "response_path",
    "responses_dir",
    "session_events_path",
    "session_snapshot_path",
    "sessions_dir",
    "spec_path",
    "tournaments_dir",
]
