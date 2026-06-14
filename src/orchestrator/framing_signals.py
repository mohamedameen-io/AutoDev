"""Deterministic structural signals for the framing phase (ADR-0044).

Two *structural* signals feed the conservative classifier as disconfirming
evidence:

* **recurrence_at_seam** — prior *human* fixes touching the candidate files/symbols
  (``git log`` scoped to those paths). AutoDev's own ``autodev: task ...`` commits are
  excluded BY MESSAGE (canonical format ``execute_phase.py``) so the signal does not
  self-justify (design-doc §14). Fires at ``count >= 1``.
* **boundary_repeatedly_touched** — how many distinct prior AutoDev *tasks* changed the
  candidate files (read from the ledger + developer evidence). AutoDev tasks are
  intentionally COUNTED here (the semantic is "how many tasks fought this boundary"), so
  there is NO commit exclusion. Fires at ``count >= 2``.

Both ``compute_*`` functions DEGRADE-NOT-RAISE: on timeout / any exception they log a
warning and return a not-fired :class:`StructuralSignal` with ``confidence == 0.0``.

The lexical "hypothesis-is-a-trim" signal is computed in :mod:`framing_phase` and is
scrutiny-only — it is NEVER structural and can never alone satisfy the gate.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from autologging import get_logger
from state.evidence import read_evidence
from state.file_index import CandidateDigest
from state.ledger import stream_entries
from state.schemas import CoderEvidence

logger = get_logger()

# Canonical AutoDev commit-message format (``execute_phase.py``:
# ``f"autodev: task {task.id} ({task.title})"``). Match the MESSAGE, not the author
# (authors can be rewritten by corporate git hooks).
AUTODEV_COMMIT_PATTERN = re.compile(r"^autodev: task \S+")

# A ``git log --oneline`` line: ``<abbrev_sha> <subject>``. Candidate file paths from
# the index (e.g. ``src/foo.py``) never match — they don't start with a hex run
# followed by whitespace.
_COMMIT_LINE_RE = re.compile(r"^([0-9a-f]{7,40})\s+(.*)$")


@dataclass(frozen=True)
class StructuralSignal:
    """One deterministic structural signal result."""

    name: str
    fired: bool
    confidence: float
    evidence: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _is_autodev_commit(msg: str) -> bool:
    """True when ``msg`` is one of AutoDev's own task commits (excluded from recurrence)."""
    return bool(AUTODEV_COMMIT_PATTERN.match(msg.strip()))


def _candidate_files(candidate_digest: CandidateDigest | None) -> list[str]:
    """Extract candidate file paths from the STRUCTURED digest object.

    Reads ``symbol_hits[].file_path`` and ``file_hits[].path`` (never the rendered
    string — a string has neither). Deduplicated, order-stable.
    """
    if candidate_digest is None:
        return []
    seen: dict[str, None] = {}
    for sh in candidate_digest.symbol_hits:
        if sh.file_path:
            seen.setdefault(sh.file_path, None)
    for fh in candidate_digest.file_hits:
        if fh.path:
            seen.setdefault(fh.path, None)
    return list(seen)


def _parse_recurrence_commits(stdout: str) -> list[str]:
    """Return the SHAs of non-AutoDev commits from ``git log --oneline`` output."""
    shas: list[str] = []
    for line in stdout.splitlines():
        m = _COMMIT_LINE_RE.match(line)
        if m is None:
            continue  # a --name-only file line or blank line
        sha, msg = m.group(1), m.group(2)
        if _is_autodev_commit(msg):
            continue
        shas.append(sha)
    return shas


async def compute_recurrence_at_seam(
    cwd: Path,
    candidate_digest: CandidateDigest,
    timeout_s: float = 60,
) -> tuple[int, list[str], StructuralSignal]:
    """Count prior HUMAN fixes touching the candidate files (recurrence-at-seam).

    Path-scoped ``git log`` (ADR-0043 huge-repo guard). Excludes AutoDev's own
    ``autodev: task`` commits. Degrades to a not-fired signal on timeout/error.
    """
    candidate_files = _candidate_files(candidate_digest)
    if not candidate_files:
        return 0, [], StructuralSignal(
            "recurrence_at_seam", False, 0.0, "no candidate files", {}
        )
    try:
        proc = subprocess.run(
            ["git", "log", "--oneline", "--name-only", "--", *candidate_files],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("framing_signals.git_log_timeout", files=len(candidate_files))
        return 0, [], StructuralSignal(
            "recurrence_at_seam", False, 0.0, "git log timeout", {}
        )
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        logger.warning("framing_signals.recurrence_failed", err=str(exc))
        return 0, [], StructuralSignal(
            "recurrence_at_seam", False, 0.0, f"error: {exc}", {}
        )
    shas = _parse_recurrence_commits(proc.stdout)
    count = len(shas)
    fired = count >= 1
    confidence = min(1.0, 0.5 + 0.1 * count) if fired else 0.0
    return count, shas, StructuralSignal(
        "recurrence_at_seam",
        fired,
        confidence,
        f"{count} prior human fix(es) at this seam",
        {"shas": shas},
    )


async def compute_boundary_repeatedly_touched(
    cwd: Path,
    candidate_digest: CandidateDigest,
) -> tuple[int, StructuralSignal]:
    """Count distinct prior AutoDev TASKS that changed the candidate files.

    Reads the ledger (``update_task_status`` → ``complete``) + each task's developer
    evidence ``files_changed``. AutoDev tasks are intentionally counted (no exclusion).
    Missing/corrupt evidence is skipped. Degrades to a not-fired signal on error.
    """
    candidate_files = set(_candidate_files(candidate_digest))
    if not candidate_files:
        return 0, StructuralSignal(
            "boundary_repeatedly_touched", False, 0.0, "no candidate files", {}
        )
    count = 0
    matched_tasks: list[str] = []
    try:
        seen: set[str] = set()
        for entry in stream_entries(cwd):
            if entry.op != "update_task_status":
                continue
            if entry.payload.get("status") != "complete":
                continue
            task_id = entry.payload.get("task_id")
            if not isinstance(task_id, str) or task_id in seen:
                continue
            seen.add(task_id)
            ev = await read_evidence(cwd, task_id, "developer")
            if isinstance(ev, CoderEvidence) and (
                set(ev.files_changed) & candidate_files
            ):
                count += 1
                matched_tasks.append(task_id)
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        logger.warning("framing_signals.boundary_failed", err=str(exc))
        return 0, StructuralSignal(
            "boundary_repeatedly_touched", False, 0.0, f"error: {exc}", {}
        )
    fired = count >= 2
    confidence = min(1.0, 0.5 + 0.1 * count) if fired else 0.0
    return count, StructuralSignal(
        "boundary_repeatedly_touched",
        fired,
        confidence,
        f"{count} prior AutoDev task(s) touched this boundary",
        {"tasks": matched_tasks},
    )
