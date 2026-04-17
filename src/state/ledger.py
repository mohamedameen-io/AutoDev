"""Append-only JSONL plan ledger with CAS hash chaining + replay.

Each ledger entry is a JSON object on its own line. Entries form a
hash-chained append-only log:

    entry[n].prev_hash == entry[n-1].self_hash

The genesis entry has ``prev_hash == ""``. Replay walks the chain and raises
:class:`~errors.LedgerCorruptError` if any link is broken (tampering,
truncated mid-write, or an entry was dropped).

Supported ops:

  - ``init_plan``: embeds the initial Plan payload so the ledger is
    self-sufficient (no plan.json required for replay).
  - ``update_plan``: overwrites Plan with a new payload (coarse-grained).
  - ``update_task_status``: mutates a single task's status.
  - ``append_evidence``: audit-only record that evidence was produced.
  - ``mark_blocked`` / ``mark_complete``: task terminals.
  - ``snapshot``: embeds the current Plan — lets replay short-circuit from
    the last snapshot instead of walking from genesis.

All disk I/O here assumes the caller holds :func:`plan_lock`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from errors import LedgerCorruptError
from autologging import get_logger
from state.paths import autodev_root, ledger_path
from state.schemas import Plan


logger = get_logger(__name__)


LedgerOp = Literal[
    "init_plan",
    "update_plan",
    "update_task_status",
    "append_evidence",
    "mark_blocked",
    "mark_complete",
    "snapshot",
    "plan_tournament_complete",
    "impl_tournament_complete",
]


class LedgerEntry(BaseModel):
    """One append-only ledger record.

    The ``self_hash`` field is computed over all other fields (with
    ``prev_hash`` included). See :func:`compute_hash`.
    """

    model_config = ConfigDict(extra="forbid")

    seq: int  # monotonically increasing, starts at 1
    timestamp: str  # ISO 8601, UTC
    session_id: str
    op: LedgerOp
    payload: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str  # self_hash of entry[n-1]; "" for genesis
    self_hash: str  # hash of this entry excluding self_hash itself


def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def compute_hash(entry_dict_without_hash: dict[str, Any]) -> str:
    """Return a 16-char SHA-256 prefix of the canonical JSON form."""
    canon = json.dumps(entry_dict_without_hash, sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def _entry_without_self_hash(entry: LedgerEntry) -> dict[str, Any]:
    d = entry.model_dump(mode="json")
    d.pop("self_hash", None)
    return d


async def append_entry(
    cwd: Path,
    op: LedgerOp,
    payload: dict[str, Any],
    session_id: str,
) -> LedgerEntry:
    """Append one entry to the ledger. Caller must hold :func:`plan_lock`.

    Computes ``seq = prev.seq + 1`` and ``prev_hash = prev.self_hash`` by
    reading the last line of the file.
    """
    lp = ledger_path(cwd)
    lp.parent.mkdir(parents=True, exist_ok=True)

    prev_seq, prev_hash = _read_last_entry_head(lp)

    entry_body: dict[str, Any] = {
        "seq": prev_seq + 1,
        "timestamp": _iso_now(),
        "session_id": session_id,
        "op": op,
        "payload": payload,
        "prev_hash": prev_hash,
    }
    self_hash = compute_hash(entry_body)
    entry_body["self_hash"] = self_hash

    entry = LedgerEntry.model_validate(entry_body)

    line = json.dumps(entry.model_dump(mode="json"), sort_keys=True) + "\n"
    _atomic_append(lp, line)
    logger.info("ledger.append", op=op, seq=entry.seq, session_id=session_id)
    return entry


def _read_last_entry_head(path: Path) -> tuple[int, str]:
    """Return ``(last_seq, last_self_hash)`` for the existing ledger.

    Returns ``(0, "")`` if the file is missing or empty. Raises
    :class:`LedgerCorruptError` if the last non-empty line is malformed —
    we refuse to append after a corrupt tail because a correct ``prev_hash``
    cannot be computed.
    """
    if not path.exists():
        return (0, "")
    last_line: str | None = None
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if stripped:
                last_line = stripped
    if last_line is None:
        return (0, "")
    try:
        last = json.loads(last_line)
    except json.JSONDecodeError as exc:
        raise LedgerCorruptError(
            f"last ledger line is not valid JSON: {exc}. "
            "Manual recovery required — inspect the trailing line of "
            f"{path} and either remove it or restore from a snapshot."
        ) from exc
    seq = last.get("seq")
    self_hash = last.get("self_hash")
    if not isinstance(seq, int) or not isinstance(self_hash, str):
        raise LedgerCorruptError(
            f"last ledger line is missing seq/self_hash fields: {path}"
        )
    return (seq, self_hash)


def _atomic_append(path: Path, line: str) -> None:
    """Append ``line`` durably.

    Strategy: copy-existing-contents-to-tmp, append, then ``os.replace``.
    This is resilient to the process being killed mid-write: either the
    whole new file is visible or nothing changed.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_bytes() if path.exists() else b""
    # tempfile in same dir to keep the replace atomic across filesystems.
    fd, tmp_path = tempfile.mkstemp(prefix=".ledger.", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(existing)
            fh.write(line.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # Best-effort cleanup on failure; the live file is untouched because
        # we only write to tmp_path until os.replace.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_entries(cwd: Path) -> list[LedgerEntry]:
    """Read + validate every ledger entry.

    Raises :class:`LedgerCorruptError` on:
      - malformed JSON on any non-empty line
      - validation failure for any entry
      - broken prev_hash / self_hash chain
      - non-monotonic ``seq``
    """
    lp = ledger_path(cwd)
    if not lp.exists():
        return []

    entries: list[LedgerEntry] = []
    prev_hash = ""
    prev_seq = 0
    with lp.open("r", encoding="utf-8") as fh:
        for idx, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerCorruptError(
                    f"ledger line {idx} is not valid JSON: {exc}. "
                    "Inspect and repair (or restore from snapshot)."
                ) from exc
            try:
                entry = LedgerEntry.model_validate(obj)
            except Exception as exc:
                raise LedgerCorruptError(
                    f"ledger line {idx} failed schema validation: {exc}"
                ) from exc

            # Chain integrity checks.
            if entry.seq != prev_seq + 1:
                raise LedgerCorruptError(
                    f"ledger line {idx}: seq jumped from {prev_seq} to {entry.seq}"
                )
            if entry.prev_hash != prev_hash:
                raise LedgerCorruptError(
                    f"ledger line {idx}: prev_hash mismatch "
                    f"(expected {prev_hash!r}, saw {entry.prev_hash!r})"
                )
            body = _entry_without_self_hash(entry)
            want = compute_hash(body)
            if want != entry.self_hash:
                raise LedgerCorruptError(
                    f"ledger line {idx}: self_hash mismatch "
                    f"(computed {want}, stored {entry.self_hash})"
                )

            entries.append(entry)
            prev_hash = entry.self_hash
            prev_seq = entry.seq
    return entries


def replay_ledger(cwd: Path) -> tuple[Plan | None, list[LedgerEntry]]:
    """Reconstruct the current :class:`Plan` from the ledger.

    Applies ops in order:
      - ``init_plan`` sets the initial Plan (payload contains serialized Plan).
      - ``snapshot`` replaces the in-memory Plan wholesale.
      - ``update_plan`` replaces with the new Plan payload.
      - ``update_task_status`` mutates one task's status (+ blocked_reason /
        retry_count / escalated if present).
      - ``mark_blocked`` / ``mark_complete`` mutate status.
      - ``append_evidence`` records the evidence path on the task but does
        not change status.

    Returns ``(None, [])`` if the ledger is empty.
    """
    entries = read_entries(cwd)
    if not entries:
        return None, entries

    plan: Plan | None = None
    for entry in entries:
        plan = _apply_op(plan, entry)
    return plan, entries


def _apply_op(plan: Plan | None, entry: LedgerEntry) -> Plan | None:
    """Apply a single ledger op to ``plan`` and return the updated plan."""
    op = entry.op
    payload = entry.payload

    if op in ("init_plan", "update_plan", "snapshot"):
        plan_payload = payload.get("plan")
        if plan_payload is None:
            raise LedgerCorruptError(
                f"entry seq={entry.seq} op={op} is missing payload.plan"
            )
        return Plan.model_validate(plan_payload)

    if op == "plan_tournament_complete":
        # Audit-only breadcrumb. Appended during the plan phase BEFORE the
        # plan is persisted via ``init_plan``, so it may legitimately appear
        # before any plan-containing op during replay. Do NOT mutate plan.
        return plan

    if op == "impl_tournament_complete":
        # Audit-only breadcrumb. Appended during the execute phase after the
        # impl tournament completes. Does NOT mutate plan state.
        return plan

    if plan is None:
        raise LedgerCorruptError(
            f"entry seq={entry.seq} op={op} applied before any init_plan"
        )

    if op == "update_task_status":
        task_id = payload.get("task_id")
        status = payload.get("status")
        if not isinstance(task_id, str) or not isinstance(status, str):
            raise LedgerCorruptError(
                f"entry seq={entry.seq} update_task_status missing task_id/status"
            )
        task = _find_task(plan, task_id)
        if task is None:
            # Plan-structure drift from the ledger — surface as corruption.
            raise LedgerCorruptError(
                f"entry seq={entry.seq} references unknown task_id={task_id}"
            )
        task.status = status  # type: ignore[assignment]
        if "blocked_reason" in payload:
            task.blocked_reason = payload["blocked_reason"]
        if "retry_count" in payload:
            task.retry_count = int(payload["retry_count"])
        if "escalated" in payload:
            task.escalated = bool(payload["escalated"])
        if "evidence_bundle" in payload:
            task.evidence_bundle = payload["evidence_bundle"]
        return plan

    if op == "mark_blocked":
        task_id = payload.get("task_id")
        if not isinstance(task_id, str):
            raise LedgerCorruptError(
                f"entry seq={entry.seq} mark_blocked missing task_id"
            )
        task = _find_task(plan, task_id)
        if task is not None:
            task.status = "blocked"
            task.blocked_reason = payload.get("reason")
        return plan

    if op == "mark_complete":
        task_id = payload.get("task_id")
        if not isinstance(task_id, str):
            raise LedgerCorruptError(
                f"entry seq={entry.seq} mark_complete missing task_id"
            )
        task = _find_task(plan, task_id)
        if task is not None:
            task.status = "complete"
        return plan

    if op == "append_evidence":
        task_id = payload.get("task_id")
        path = payload.get("path")
        if isinstance(task_id, str) and isinstance(path, str):
            task = _find_task(plan, task_id)
            if task is not None:
                task.evidence_bundle = path
        return plan

    # Unknown op — fail loudly rather than silently produce wrong state.
    raise LedgerCorruptError(f"entry seq={entry.seq} has unknown op={op!r}")


def _find_task(plan: Plan, task_id: str) -> Any:
    for phase in plan.phases:
        for task in phase.tasks:
            if task.id == task_id:
                return task
    return None


async def snapshot_plan(cwd: Path, plan: Plan, session_id: str) -> LedgerEntry:
    """Persist ``plan`` atomically to ``plan.json`` AND append a snapshot entry.

    Caller must hold :func:`plan_lock`.

    Order:
      1. Write ``plan.json`` atomically (tmp -> os.replace).
      2. Append a ``snapshot`` ledger entry containing the full plan payload.

    Both steps run under the same lock, so a crash between them at worst
    leaves a new plan.json with an out-of-date ledger — replay still works
    from the embedded Plan in the ledger, and the next successful snapshot
    makes them consistent again.
    """
    pp = autodev_root(cwd) / "plan.json"
    pp.parent.mkdir(parents=True, exist_ok=True)
    payload = plan.model_dump(mode="json")
    raw = json.dumps(payload, indent=2) + "\n"
    fd, tmp_path = tempfile.mkstemp(prefix=".plan.", suffix=".tmp", dir=str(pp.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, pp)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return await append_entry(
        cwd,
        op="snapshot",
        payload={"plan": payload},
        session_id=session_id,
    )


__all__ = [
    "LedgerEntry",
    "LedgerOp",
    "append_entry",
    "compute_hash",
    "read_entries",
    "replay_ledger",
    "snapshot_plan",
]
