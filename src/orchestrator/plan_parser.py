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
from typing import Literal, cast

from errors import AutodevError
from autologging import get_logger
from state.schemas import AcceptanceCriterion, Phase, Plan, Task


logger = get_logger(__name__)


# Tokens accepted on a ``Requires:`` line — must mirror the Literal in
# :class:`state.schemas.Task.requires`. Kept as a frozenset for O(1) membership
# checks during parsing; unknown tokens are dropped with a warning.
_VALID_REQUIRES_TOKENS = frozenset(
    {"hardware", "human", "external_service", "manual"}
)


class PlanParseError(AutodevError):
    """Raised when architect output cannot be parsed into a :class:`Plan`."""


_RE_PLAN_TITLE = re.compile(r"^#\s+Plan:\s*(.+?)\s*$", re.MULTILINE)
_RE_PHASE = re.compile(r"^##\s+Phase\s+([0-9A-Za-z._-]+)\s*:\s*(.+?)\s*$")
_RE_TASK = re.compile(r"^###\s+Task\s+([0-9A-Za-z._-]+)\s*:\s*(.+?)\s*$")
_RE_FILES = re.compile(r"^\s*-\s*Files?\s*:\s*(.+?)\s*$", re.IGNORECASE)
_RE_DESC = re.compile(r"^\s*-\s*Description\s*:\s*(.+?)\s*$", re.IGNORECASE)
_RE_ACCEPT_HEADER = re.compile(r"^\s*-\s*Acceptance\s*:?\s*$", re.IGNORECASE)
_RE_ACCEPT_ITEM = re.compile(r"^\s*-\s*\[\s*[ xX]?\s*\]\s*(.+?)\s*$")
# v0.9.0: phase-level acceptance block. Same shape as the task-level
# ``_RE_ACCEPT_HEADER`` but recognized only between a ``## Phase`` heading
# and the first ``### Task`` heading inside that phase. The accumulator
# state in :func:`parse_plan_markdown` keys phase-acceptance vs.
# task-acceptance from the current cursor position rather than a separate
# regex — items are still matched by ``_RE_ACCEPT_ITEM``.
_RE_PHASE_ACCEPTANCE_HEADER = re.compile(
    r"^\s*-\s*Acceptance\s*:?\s*$", re.IGNORECASE
)
# v0.9.0: track whether an acceptance line is met (``[x]`` / ``[X]``) so
# the phase-review judge can render "X met / Y unmet" summaries. Item-level
# bookkeeping; the regex matches both ticked and unticked checkboxes.
_RE_ACCEPT_ITEM_TICKED = re.compile(
    r"^\s*-\s*\[\s*[xX]\s*\]\s*(.+?)\s*$"
)
_RE_DEPENDS = re.compile(r"^\s*-\s*Depends(?:_on|On)?\s*:\s*(.+?)\s*$", re.IGNORECASE)
_RE_REQUIRES = re.compile(
    r"^\s*-?\s*Requires\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
# v0.8.0: per-task complexity directive. Mirrors :data:`_RE_COMPLEXITY` (the
# trailing plan-level directive) but lives inside a task body, alongside
# ``- Requires:`` / ``- Description:``. Captured tokens are normalized to
# lowercase and validated against ``{"simple","medium","complex"}`` —
# unknown values are dropped with a warning so the orchestrator falls back
# to the spec default.
_RE_TASK_COMPLEXITY = re.compile(
    r"^\s*-\s*Complexity\s*:\s*(simple|medium|complex)\s*$",
    re.IGNORECASE,
)
_RE_EXECUTABLE_BY = re.compile(
    r"^\s*-?\s*EXECUTABLE_BY\s*:\s*(human|agent)\s*$",
    re.IGNORECASE,
)
_RE_COMPLEXITY = re.compile(
    r"^COMPLEXITY:\s*(simple|medium|complex)\s*$",
    re.MULTILINE | re.IGNORECASE,
)
# v0.14.0: ``EDIT_SCOPE:`` block header. Top-level (between ``# Plan:`` and the
# first ``## Phase``) → ``Plan.edit_scope``. Per-phase (between ``## Phase``
# and the first ``### Task`` in that phase) → ``Phase.edit_scope`` override.
# Items are individual ``- <path>`` lines; the parser scans them sequentially
# until the next non-matching line. Trailing ``# comment`` segments on each
# item are stripped.
_RE_EDIT_SCOPE_HEADER = re.compile(
    r"^\s*EDIT_SCOPE\s*:?\s*$",
    re.IGNORECASE,
)
_RE_EDIT_SCOPE_ITEM = re.compile(
    r"^\s*-\s*([^#\n]+?)\s*(?:#.*)?$",
)


def _iso_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def extract_complexity(md: str) -> Literal["simple", "medium", "complex"] | None:
    """Return the architect's ``COMPLEXITY:`` classification or ``None``.

    Light-weight alternative to :func:`parse_plan_markdown` for callers that
    only need the complexity bucket — specifically the plan-tournament runner,
    which runs *before* the parsed Plan is persisted to ``plan_manager`` and
    therefore can't reach the value via ``plan_manager.load()``. Reading
    directly from the architect's markdown sidesteps that ordering problem.

    Returns ``None`` for legacy plans without the line; callers fall back to
    the user-global effort default.
    """
    m = _RE_COMPLEXITY.search(md)
    if m is None:
        return None
    return cast(
        Literal["simple", "medium", "complex"],
        m.group(1).lower(),
    )


def parse_plan_markdown(md: str, *, spec_hash: str = "") -> Plan:
    """Parse architect markdown into a :class:`Plan`.

    :raises PlanParseError: when the plan title is missing, no phases are
        present, or a phase has no tasks.
    """
    if not md or not md.strip():
        raise PlanParseError("empty plan markdown")

    # Capture and strip the trailing ``COMPLEXITY: <bucket>`` directive emitted
    # by the architect. Done before the line-loop so the body parser never sees
    # the directive line. Legacy plans without this line gracefully resolve to
    # ``complexity = None`` (consumers fall back to user-global effort).
    m_complexity = _RE_COMPLEXITY.search(md)
    complexity: Literal["simple", "medium", "complex"] | None = None
    if m_complexity is not None:
        # The regex constrains the captured group to {simple, medium, complex}
        # (case-insensitive); ``.lower()`` normalizes it to one of the three
        # literals. ``cast`` tells mypy the runtime guarantee.
        complexity = cast(
            Literal["simple", "medium", "complex"],
            m_complexity.group(1).lower(),
        )
        md = _RE_COMPLEXITY.sub("", md, count=1)

    title_match = _RE_PLAN_TITLE.search(md)
    if title_match is None:
        raise PlanParseError("missing '# Plan: <title>' heading")
    plan_title = title_match.group(1).strip()

    phases: list[Phase] = []
    current_phase: dict | None = None
    current_task: dict | None = None
    in_acceptance_block = False
    # v0.9.0: track whether the current acceptance block belongs to the
    # phase header (no current_task) vs. a specific task. Same cursor —
    # only the destination dict differs.
    in_phase_acceptance_block = False
    # v0.14.0: EDIT_SCOPE accumulator. Top-level scope lands on
    # ``plan_edit_scope``; per-phase scope on ``current_phase["edit_scope"]``.
    # ``in_edit_scope_block`` tracks whether we're inside a block (either
    # top-level or phase-level — disambiguated by ``current_phase``).
    plan_edit_scope: list[str] = []
    in_edit_scope_block = False

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
        # v0.9.0: synthesize phase-level AcceptanceCriterion objects from the
        # accumulated phase acceptance bullets. Each entry is a dict
        # {"description": str, "met": bool}; we map to the schema model here
        # so the parser stays the only producer of AcceptanceCriterion.
        phase_acc = [
            AcceptanceCriterion(
                id=f"ph-ac-{i + 1}",
                description=item["description"],
                met=item["met"],
            )
            for i, item in enumerate(current_phase.get("acceptance", []))
        ]
        phases.append(
            Phase(
                id=current_phase["id"],
                title=current_phase["title"],
                description=current_phase.get("description", ""),
                tasks=[
                    _make_task(t, current_phase["id"]) for t in current_phase["tasks"]
                ],
                acceptance=phase_acc,
                # v0.14.0: ``None`` (default, inherit from plan) when no
                # per-phase EDIT_SCOPE block was emitted. A non-None list
                # is the explicit override (including the empty list,
                # which opts the phase into legacy whole-repo behavior).
                edit_scope=current_phase.get("edit_scope"),
            )
        )
        current_phase = None

    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            # Blank line ends an acceptance block but keeps the task open.
            in_acceptance_block = False
            in_phase_acceptance_block = False
            in_edit_scope_block = False
            continue

        phase_m = _RE_PHASE.match(line)
        if phase_m:
            _finalize_phase()
            current_phase = {
                "id": phase_m.group(1).strip(),
                "title": phase_m.group(2).strip(),
                "description": "",
                "tasks": [],
                # v0.9.0: phase-level acceptance accumulator. Stays open until
                # the first ``### Task`` heading in the phase (or the next
                # ``## Phase`` / EOF).
                "acceptance": [],
                # v0.14.0: per-phase EDIT_SCOPE override accumulator.
                # ``None`` means "no per-phase block emitted" (inherit
                # plan scope). A non-None list — including the empty list
                # — is an explicit override.
                "edit_scope": None,
            }
            current_task = None
            in_acceptance_block = False
            in_phase_acceptance_block = False
            in_edit_scope_block = False
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
                "requires": [],
            }
            in_acceptance_block = False
            # v0.9.0: a Task heading definitively ends the phase-acceptance
            # accumulator — subsequent ``- [x] ...`` items belong to the
            # task, not the phase.
            in_phase_acceptance_block = False
            # v0.14.0: a Task heading also closes a per-phase EDIT_SCOPE
            # block — subsequent ``- <path>`` items inside the task body
            # are NOT scope overrides.
            in_edit_scope_block = False
            continue

        # v0.14.0: EDIT_SCOPE block recognition. The header opens a block
        # of ``- <path>`` items. The destination is determined by cursor:
        # before any phase heading → plan_edit_scope; inside a phase
        # before the first task → current_phase["edit_scope"].
        if _RE_EDIT_SCOPE_HEADER.match(line):
            in_edit_scope_block = True
            in_acceptance_block = False
            in_phase_acceptance_block = False
            # Initialize per-phase override list if we're inside a phase
            # (so an empty block still produces an explicit empty-list
            # override, distinct from "block absent" → None inherit).
            if current_phase is not None and current_task is None:
                current_phase["edit_scope"] = []
            continue

        if in_edit_scope_block:
            item_m = _RE_EDIT_SCOPE_ITEM.match(line)
            if item_m:
                entry = item_m.group(1).strip().rstrip("/")
                if entry:
                    if current_phase is None:
                        plan_edit_scope.append(entry)
                    elif current_task is None:
                        # We initialized to [] when the header opened —
                        # safe to .append.
                        scope_list = current_phase.get("edit_scope")
                        if scope_list is None:
                            scope_list = []
                            current_phase["edit_scope"] = scope_list
                        scope_list.append(entry)
                continue
            # Non-item line ends the block.
            in_edit_scope_block = False

        # v0.9.0: phase-level acceptance handling. Only fires BEFORE the
        # first task in a phase (when ``current_task is None``). The
        # ``- Acceptance:`` header opens the block; subsequent ``- [ ] ...``
        # items accumulate into ``current_phase["acceptance"]``.
        if current_phase is not None and current_task is None:
            if _RE_PHASE_ACCEPTANCE_HEADER.match(line):
                in_phase_acceptance_block = True
                in_acceptance_block = False
                continue
            if in_phase_acceptance_block:
                item_m = _RE_ACCEPT_ITEM.match(line)
                if item_m:
                    is_ticked = bool(_RE_ACCEPT_ITEM_TICKED.match(line))
                    current_phase["acceptance"].append(
                        {
                            "description": item_m.group(1).strip(),
                            "met": is_ticked,
                        }
                    )
                    continue
                in_phase_acceptance_block = False

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

        req_m = _RE_REQUIRES.match(line)
        if req_m:
            survivors: list[str] = []
            for raw_token in req_m.group(1).split(","):
                tok = raw_token.strip().lower()
                if not tok:
                    continue
                if tok in _VALID_REQUIRES_TOKENS:
                    survivors.append(tok)
                else:
                    logger.warning(
                        "plan_parser.unknown_requires_token",
                        token=tok,
                        task_id=current_task["id"],
                    )
            current_task["requires"].extend(survivors)
            in_acceptance_block = False
            continue

        # v0.8.0: per-task complexity directive. The regex constrains the
        # captured group to {simple, medium, complex} (case-insensitive); we
        # still defensively validate the lowercased token and drop unknowns
        # with a warning so the resolver falls back to the spec default.
        complexity_m = _RE_TASK_COMPLEXITY.match(line)
        if complexity_m:
            tok = complexity_m.group(1).strip().lower()
            if tok in {"simple", "medium", "complex"}:
                current_task["complexity"] = tok
            else:
                logger.warning(
                    "plan_parser.unknown_task_complexity_token",
                    token=tok,
                    task_id=current_task["id"],
                )
            in_acceptance_block = False
            continue

        exec_by_m = _RE_EXECUTABLE_BY.match(line)
        if exec_by_m:
            who = exec_by_m.group(1).strip().lower()
            if who == "human":
                current_task["requires"].append("manual")
            # ``agent`` is a no-op — explicit declaration that the task is
            # agent-executable. Recorded only to allow architects to be
            # symmetric in their markup.
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
        complexity=complexity,
        # v0.14.0: top-level EDIT_SCOPE block flows here. Empty list when
        # the block was absent — the schema validator passes through, and
        # downstream consumers (validate_edit_scope) treat empty as the
        # legacy whole-repo no-op.
        edit_scope=plan_edit_scope,
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
        requires=raw.get("requires", []),
        # v0.8.0: per-task complexity bucket parsed from ``- Complexity:`` body
        # line. ``None`` for legacy plans / tasks the architect didn't tag —
        # downstream resolver returns ``None`` and execute_phase falls back
        # to the spec default.
        complexity=raw.get("complexity"),
        assigned_agent="developer",
    )


__all__ = ["PlanParseError", "parse_plan_markdown"]
