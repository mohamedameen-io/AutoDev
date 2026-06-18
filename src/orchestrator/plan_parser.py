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
from dataclasses import dataclass
from typing import Literal, cast

from errors import AutodevError
from autologging import get_logger
from orchestrator.dependency_inference import infer_plan_dependencies
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
# v0.24.3: ``[new]`` prefix on a comma-split ``Files:`` entry marks the path
# as one the task itself will create. Parser strips the prefix and routes
# the path into ``Task.files_new`` so :func:`validate_files_exist` skips
# it during the on-disk existence sweep.
_RE_NEW_PREFIX = re.compile(r"^\s*\[new\]\s*", re.I)
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
# v0.20.0 C1: per-task extended-scope block. Architects emit
# ``- Extended-scope: path/a, path/b`` (single-line) OR
# ``- Extended-scope:`` followed by ``  - path`` items (multi-line).
# The parser matches the inline form here; the multi-line form falls
# through into the Files / Description state machine via ``in_extended_scope_block``.
_RE_EXTENDED_SCOPE = re.compile(
    r"^\s*-\s*Extended[-_]scope\s*:\s*(.*?)\s*$",
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


@dataclass(frozen=True)
class ParsedFilesReport:
    """Per-entry result of :func:`_normalize_path_entry`.

    ``path`` is non-None when the entry survived the v0.27 Phase 1
    shape-check; ``drop_reason`` is non-None when the parser stripped
    the entry as hedge text. Callers typically materialise a list of
    these reports, then partition into kept-paths + dropped-entries so
    the dropped set can be logged for forensics.
    """

    raw: str
    path: str | None
    drop_reason: str | None


# v0.27 Phase 1: tokens the architect sometimes emits as a placeholder
# instead of a real path. Compared case-insensitively against the
# whole-string after stripping.
_PATH_PLACEHOLDER_TOKENS: frozenset[str] = frozenset(
    {"TBD", "TODO", "N/A", "NONE", "TBA", "PLACEHOLDER", "FIXME"}
)


def _normalize_path_entry(raw: str) -> ParsedFilesReport:
    """Normalise a single architect-emitted path entry.

    v0.27 audit §1 hardening: the parser previously preserved hedge
    text (paren-tails, comment-tails, bare placeholder tokens) so the
    on-disk validator + the v0.26.2 persistent-drop loop had to clean
    them up downstream. Phase 1 rejects them at parse time so the
    plan structure is canonical before validation runs.

    Drop rules (returns ``path=None`` with ``drop_reason``):

      * Empty after strip.
      * Inline ``# comment`` reduces the entry to nothing.
      * Contains ``(`` or ``)`` (paren-hedge).
      * Contains ``[`` or ``]`` (bracket-hedge; the ``[new]`` prefix is
        stripped upstream by :data:`_RE_NEW_PREFIX`).
      * Whole-string matches a placeholder token (case-insensitive).
      * Contains a space but no ``/`` — a multi-word phrase rather
        than a path. Legitimate paths with spaces have at least one
        slash (e.g. ``docs/My File.md``).

    Preserved entries: stripped of trailing ``/`` and inline
    ``# comment`` tails; everything else returned verbatim.
    """
    s = raw.strip()
    if not s:
        return ParsedFilesReport(raw=raw, path=None, drop_reason="empty")

    # Strip inline `# comment` tail; legitimate paths never include `#`.
    if "#" in s:
        head = s.split("#", 1)[0].rstrip()
        if not head:
            return ParsedFilesReport(
                raw=raw, path=None, drop_reason="comment_only"
            )
        s = head

    # Paren-hedge: legitimate repo-relative paths never contain parens.
    if "(" in s or ")" in s:
        return ParsedFilesReport(
            raw=raw, path=None, drop_reason="paren_hedge"
        )

    # Bracket-hedge: the `[new]` prefix is stripped upstream via
    # :data:`_RE_NEW_PREFIX`. Anything else with brackets is malformed.
    if "[" in s or "]" in s:
        return ParsedFilesReport(
            raw=raw, path=None, drop_reason="bracket_hedge"
        )

    # Placeholder token (case-insensitive whole-string match).
    if s.upper() in _PATH_PLACEHOLDER_TOKENS:
        return ParsedFilesReport(
            raw=raw, path=None, drop_reason="placeholder"
        )

    # Space-without-slash: prose phrase, not a path. Legitimate paths
    # with spaces are kept iff they also contain a ``/`` separator.
    if " " in s and "/" not in s:
        return ParsedFilesReport(
            raw=raw, path=None, drop_reason="space_without_slash"
        )

    s = s.rstrip("/")
    if not s:
        return ParsedFilesReport(
            raw=raw, path=None, drop_reason="empty_after_strip"
        )
    return ParsedFilesReport(raw=raw, path=s, drop_reason=None)


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
                # v0.24.3: paths the task will CREATE — parser routes
                # ``[new] path`` entries from the ``Files:`` line here so
                # validate_files_exist skips them during on-disk checks.
                "files_new": [],
                "acceptance": [],
                "depends_on": [],
                "requires": [],
                # v0.20.0 C1
                "extended_scope": [],
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
                # v0.27 Phase 1: route every entry through the shared
                # shape-check. Hedge text (parens, brackets, placeholder
                # tokens) is dropped here so the validator + persistent-
                # drop loop don't have to clean it up downstream.
                report = _normalize_path_entry(item_m.group(1))
                if report.path is not None:
                    if current_phase is None:
                        plan_edit_scope.append(report.path)
                    elif current_task is None:
                        scope_list = current_phase.get("edit_scope")
                        if scope_list is None:
                            scope_list = []
                            current_phase["edit_scope"] = scope_list
                        scope_list.append(report.path)
                else:
                    logger.warning(
                        "plan_parser.edit_scope_entry_dropped",
                        raw=report.raw,
                        reason=report.drop_reason,
                    )
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
            # v0.24.3: partition each comma-split entry into ``files`` vs.
            # ``files_new`` based on a leading ``[new]`` prefix. The prefix
            # is stripped before storage; the routing decides whether
            # ``validate_files_exist`` will require the path to exist.
            # v0.27 Phase 1: each remainder is shape-checked via
            # :func:`_normalize_path_entry` so hedge text (paren-tails,
            # comment-tails, placeholder tokens) is rejected before
            # the path reaches the validator.
            files_existing: list[str] = []
            files_to_create: list[str] = []
            for raw_entry in files_m.group(1).split(","):
                stripped = raw_entry.strip()
                if not stripped:
                    continue
                if _RE_NEW_PREFIX.match(stripped):
                    remainder = _RE_NEW_PREFIX.sub(
                        "", stripped, count=1
                    ).strip()
                    report = _normalize_path_entry(remainder)
                    if report.path is not None:
                        files_to_create.append(report.path)
                    else:
                        logger.warning(
                            "plan_parser.task_files_new_entry_dropped",
                            raw=report.raw,
                            reason=report.drop_reason,
                            task_id=current_task["id"],
                        )
                else:
                    report = _normalize_path_entry(stripped)
                    if report.path is not None:
                        files_existing.append(report.path)
                    else:
                        logger.warning(
                            "plan_parser.task_files_entry_dropped",
                            raw=report.raw,
                            reason=report.drop_reason,
                            task_id=current_task["id"],
                        )
            current_task["files"] = files_existing
            current_task["files_new"] = files_to_create
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

        # v0.20.0 C1: per-task ``Extended-scope:`` parsing. Mirrors the
        # ``Files:`` parser — comma-separated single-line list. Each
        # entry is normalized via the schema validator (trim trailing /,
        # reject absolute, reject ``..``).
        # v0.27 Phase 1: each entry runs through the shared shape-check
        # so hedge text is rejected at parse time.
        ext_scope_m = _RE_EXTENDED_SCOPE.match(line)
        if ext_scope_m:
            payload = ext_scope_m.group(1).strip()
            if payload:
                surviving: list[str] = []
                for raw_entry in payload.split(","):
                    stripped = raw_entry.strip()
                    if not stripped:
                        continue
                    report = _normalize_path_entry(stripped)
                    if report.path is not None:
                        surviving.append(report.path)
                    else:
                        logger.warning(
                            "plan_parser.extended_scope_entry_dropped",
                            raw=report.raw,
                            reason=report.drop_reason,
                            task_id=current_task["id"],
                        )
                current_task["extended_scope"] = surviving
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

    # v0.41.0 A2: populate implicit ``depends_on`` for same-phase tasks that
    # consume an earlier task's output (file overlap on a created/edited path,
    # or a task-id reference in the description). The architect now emits
    # explicit ``Depends:`` lines in most cases (see architect.md "TASK
    # DEPENDENCIES"), but inference is the belt-and-suspenders that closes the
    # Run-3 parallel-worktree incoherence (1.1 creates a serializer, 1.2 routes
    # through it, no dep declared). Tasks with an explicit ``depends_on`` are
    # left untouched; inference only ever adds backward edges (later → earlier
    # in declaration order) so it cannot introduce a cycle.
    infer_plan_dependencies(phases)

    # Field-finding F-1 (edit_scope self-consistency) is repaired NOT here but
    # at plan-init, AFTER the on-disk drop / empty-scope guard pass
    # (``orchestrator.plan_phase._validate_with_persistent_drop``). Running the
    # repair at parse time would pre-populate a phase's ``edit_scope`` with its
    # task-declared files and thereby mask the empty-scope guard: a phase that
    # narrows to an on-disk-missing path would no longer go empty after the
    # drop, so the P0 silent-widen guard could never fire. The repair must see
    # the post-drop scope, hence its placement downstream of validation.
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
        # v0.24.3: paths the task will CREATE (parsed from ``[new] <path>``
        # entries on the ``Files:`` line). Skipped by validate_files_exist
        # during the on-disk existence sweep.
        files_new=raw.get("files_new", []),
        acceptance=crit,
        depends_on=raw.get("depends_on", []),
        requires=raw.get("requires", []),
        # v0.8.0: per-task complexity bucket parsed from ``- Complexity:`` body
        # line. ``None`` for legacy plans / tasks the architect didn't tag —
        # downstream resolver returns ``None`` and execute_phase falls back
        # to the spec default.
        complexity=raw.get("complexity"),
        # v0.20.0 C1: per-task extended scope parsed from ``- Extended-scope:``.
        extended_scope=raw.get("extended_scope", []),
        assigned_agent="developer",
    )


__all__ = [
    "PlanParseError",
    "ParsedFilesReport",
    "parse_plan_markdown",
]
