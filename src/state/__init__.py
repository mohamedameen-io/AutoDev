"""Durable state subsystem for autodev.

Exposes:
  - :mod:`.paths` — filesystem layout constants and path builders for
    ``.autodev/``.
  - :mod:`.schemas` — pydantic v2 ports of plan/task/evidence types.
  - :mod:`.lockfile` — async-friendly wrapper around ``filelock.FileLock``.
  - :mod:`.ledger` — append-only JSONL with CAS hash chaining + replay.
  - :mod:`.plan_manager` — load / save / mutate plan via the ledger.
  - :mod:`.evidence` — pydantic-validated evidence bundle I/O.
  - :mod:`.knowledge` — Phase-4 stub; Phase 9 implements tiered ranking.

Every disk mutation routes through :func:`.lockfile.plan_lock` to keep
concurrent writers serialized, and uses atomic ``tmp -> os.replace`` writes.
"""

from __future__ import annotations

from state.evidence import (
    list_evidence,
    read_evidence,
    write_evidence,
    write_patch,
)
from state.knowledge import KnowledgeStore
from state.ledger import (
    LedgerEntry,
    append_entry,
    compute_hash,
    read_entries,
    replay_ledger,
    snapshot_plan,
)
from state.lockfile import plan_lock
from state.paths import (
    AUTODEV_DIR,
    CONFIG_FILE,
    EVIDENCE_DIR,
    KNOWLEDGE_FILE,
    LEDGER_FILE,
    LOCK_FILE,
    PLAN_FILE,
    REJECTED_LESSONS_FILE,
    SESSIONS_DIR,
    SPEC_FILE,
    TOURNAMENTS_DIR,
    autodev_root,
    ensure_autodev_dir,
    evidence_dir,
    evidence_path,
    ledger_path,
    plan_path,
    session_events_path,
    tournaments_dir,
)
from state.plan_manager import PlanManager
from state.schemas import (
    AcceptanceCriterion,
    CoderEvidence,
    CriticEvidence,
    Evidence,
    ExploreEvidence,
    Phase,
    Plan,
    ReviewEvidence,
    SMEEvidence,
    Task,
    TaskStatus,
    TestEvidence,
)


__all__ = [
    "AUTODEV_DIR",
    "AcceptanceCriterion",
    "CONFIG_FILE",
    "CoderEvidence",
    "CriticEvidence",
    "EVIDENCE_DIR",
    "Evidence",
    "ExploreEvidence",
    "KNOWLEDGE_FILE",
    "KnowledgeStore",
    "LEDGER_FILE",
    "LOCK_FILE",
    "LedgerEntry",
    "PLAN_FILE",
    "Phase",
    "Plan",
    "PlanManager",
    "REJECTED_LESSONS_FILE",
    "ReviewEvidence",
    "SESSIONS_DIR",
    "SMEEvidence",
    "SPEC_FILE",
    "TOURNAMENTS_DIR",
    "Task",
    "TaskStatus",
    "TestEvidence",
    "append_entry",
    "autodev_root",
    "compute_hash",
    "ensure_autodev_dir",
    "evidence_dir",
    "evidence_path",
    "ledger_path",
    "list_evidence",
    "plan_lock",
    "plan_path",
    "read_entries",
    "read_evidence",
    "replay_ledger",
    "session_events_path",
    "snapshot_plan",
    "tournaments_dir",
    "write_evidence",
    "write_patch",
]
