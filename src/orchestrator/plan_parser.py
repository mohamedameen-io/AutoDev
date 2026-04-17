"""Deterministic parser for the architect's plan-markdown output.

Expected format::

  # Plan: <title>
  ## Phase 1: <phase title>
  ### Task 1.1: <task title>
    - Description: <text>
    - Files: path/a.py, path/b.py
    - Acceptance:
      - [ ] first criterion
      - [ ] second criterion
    - Depends: 1.0

Forgiving about heading whitespace, trailing colons, and missing fields —
strict only on the overall structure (plan title + at least one phase with
at least one task per phase).
"""

from __future__ import annotations

import datetime as _dt
import re
import uuid

from errors import AutodevError
from state.schemas import AcceptanceCriterion, Phase, Plan, Task


class PlanParseError(AutodevError):
    """Raised when architect output cannot be parsed into a :class:`Plan`."""


_RE_PLAN_TITLE = re.compile(r"^#\s+Plan:\s*(.+?)\s*$", re.MULTILINE)
_RE_PHASE = re.compile(r"^##\s+Phase\s+([0-9A-Za-z._-]+)\s*:\s*(.+?)\s*$")
_RE_TASK = re.compile(r"^###\s+Task\s+([0-9A-Za-z._-]+)\s*:\s*(.+?)\s*$")
_RE_FILES = re.compile(r"^\s*-\s*Files?\s*:\s*(.+?)\s*$", re.IGNORECASE)
_RE_DESC = re.compile(r"^\s*-\s*Description\s*:\s*(.+?)\s*$", re.IGNORECASE)
_RE_ACCEPT_HEADER = re.compile(r"^\s*-\s*Acceptance\s*:?\s*$", re.IGNORECASE)
_RE_ACCEPT_ITEM = re.compile(r"^\s*-\s*\[\s*[ xX]?\s*\]\s*(.+?)\s*$")
_RE_DEPENDS = re.compile(r"^\s*-\s*Depends(?:_on|On)?\s*:\s*(.+?)\s*$", re.IGNORECASE)


def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def parse_plan_markdown(md: str, *, spec_hash: str = "") -> Plan:
    """Parse architect markdown into a :class:`Plan`.

    :raises PlanParseError: when the plan title is missing, no phases are
        present, or a phase has no tasks.
    """
    if not md or not md.strip():
        raise PlanParseError("empty plan markdown")

    title_match = _RE_PLAN_TITLE.search(md)
    if title_match is None:
        raise PlanParseError("missing '# Plan: <title>' heading")
    plan_title = title_match.group(1).strip()

    phases: list[Phase] = []
    current_phase: dict | None = None
    current_task: dict | None = None
    in_acceptance_block = False

    def _finalize_task() -> None:
        nonlocal current_task, in_acceptance_block
        if current_task is None or current_phase is None:
            return
        current_phase["tasks"].append(current_task)
        current_task = None
        in_acceptance_block = False

    def _finalize_phase() -> None:
        nonlocal current_phase
        if current_phase is None:
            return
        _finalize_task()
        if not current_phase["tasks"]:
            raise PlanParseError(f"phase {current_phase['id']!r} has no tasks")
        phases.append(
            Phase(
                id=current_phase["id"],
                title=current_phase["title"],
                description=current_phase.get("description", ""),
                tasks=[
                    _make_task(t, current_phase["id"]) for t in current_phase["tasks"]
                ],
            )
        )
        current_phase = None

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            # Blank line ends an acceptance block but keeps the task open.
            in_acceptance_block = False
            continue

        phase_m = _RE_PHASE.match(line)
        if phase_m:
            _finalize_phase()
            current_phase = {
                "id": phase_m.group(1).strip(),
                "title": phase_m.group(2).strip(),
                "description": "",
                "tasks": [],
            }
            current_task = None
            in_acceptance_block = False
            continue

        task_m = _RE_TASK.match(line)
        if task_m:
            if current_phase is None:
                raise PlanParseError(
                    f"task {task_m.group(1)!r} appears before any phase heading"
                )
            _finalize_task()
            current_task = {
                "id": task_m.group(1).strip(),
                "title": task_m.group(2).strip(),
                "description": "",
                "files": [],
                "acceptance": [],
                "depends_on": [],
            }
            in_acceptance_block = False
            continue

        if current_task is None:
            continue

        files_m = _RE_FILES.match(line)
        if files_m:
            current_task["files"] = [
                s.strip() for s in files_m.group(1).split(",") if s.strip()
            ]
            in_acceptance_block = False
            continue

        desc_m = _RE_DESC.match(line)
        if desc_m:
            current_task["description"] = desc_m.group(1).strip()
            in_acceptance_block = False
            continue

        dep_m = _RE_DEPENDS.match(line)
        if dep_m:
            current_task["depends_on"] = [
                s.strip() for s in dep_m.group(1).split(",") if s.strip()
            ]
            in_acceptance_block = False
            continue

        if _RE_ACCEPT_HEADER.match(line):
            in_acceptance_block = True
            continue

        if in_acceptance_block:
            item_m = _RE_ACCEPT_ITEM.match(line)
            if item_m:
                current_task["acceptance"].append(item_m.group(1).strip())
                continue
            in_acceptance_block = False

    _finalize_phase()

    if not phases:
        raise PlanParseError("no phases found in plan markdown")

    now = _iso_now()
    return Plan(
        plan_id=f"plan-{uuid.uuid4().hex[:12]}",
        spec_hash=spec_hash,
        phases=phases,
        metadata={"title": plan_title},
        created_at=now,
        updated_at=now,
    )


def _make_task(raw: dict, phase_id: str) -> Task:
    crit = [
        AcceptanceCriterion(id=f"ac-{i + 1}", description=desc)
        for i, desc in enumerate(raw.get("acceptance", []))
    ]
    return Task(
        id=raw["id"],
        phase_id=phase_id,
        title=raw["title"],
        description=raw.get("description", "") or raw["title"],
        files=raw.get("files", []),
        acceptance=crit,
        depends_on=raw.get("depends_on", []),
        assigned_agent="developer",
    )


__all__ = ["PlanParseError", "parse_plan_markdown"]
