"""Parse architect_b / synthesizer corrective direction text into Tasks.

Used by :func:`orchestrator.execute_phase._run_phase_review` after a B / AB
phase-review winner. The parser splits the direction text on top-level
bullet markers (``-``, ``*``, ``1.``) and produces one :class:`Task` per
top-level bullet, with the bullet's first line as title and the full
indented body as description.

This is intentionally lightweight: no architect roundtrip in v0.9.0 — the
direction text comes straight from architect_b / synthesizer in the
tournament, which we trust to follow the prompted "one bullet per task"
format. Malformed direction text degrades gracefully: an empty list is
returned and the caller continues forward (the phase is recorded as
``corrective_required`` but no actual sub-tasks land, which the
orchestrator treats as a soft failure of the corrective pass).
"""

from __future__ import annotations

import re
from typing import Literal

from autologging import get_logger
from state.schemas import Task


logger = get_logger(__name__)


# Top-level bullets only — leading whitespace ≤ 1 space (so deeply-indented
# sub-bullets stay nested inside the parent bullet's description).
_RE_BULLET = re.compile(r"^(?:[-*]|\d+\.)\s+(.+)$")
_RE_TOP_LEVEL_BULLET = re.compile(r"^[ ]?(?:[-*]|\d+\.)\s+(.+)$")


def parse_corrective_direction(
    text: str,
    phase_id: str,
    base_task_count: int,
    phase_complexity: str | None = None,
    tournament_id: str | None = None,
    max_tasks: int | None = None,
) -> list[Task]:
    """Parse architect_b's corrective direction into ``Task`` objects.

    Args:
        text: The direction text (typically architect_b's bullet list, or
            the synthesizer's merged version). Each top-level bullet
            becomes one corrective Task.
        phase_id: The phase being corrected. Tasks are stamped with this
            value as :attr:`Task.phase_id` and as the prefix of the
            generated id (``f"{phase_id}.c{N}"``).
        base_task_count: The number of existing tasks in the phase BEFORE
            corrective injection. Used to compute the suffix N so
            corrective ids never collide with the architect's original
            tasks.
        phase_complexity: Optional rollup of the phase's task complexity
            buckets. Stamped onto each corrective Task so the orchestrator's
            per-task ``max_turns`` / ``timeout_s`` resolver can scale them
            appropriately. Falls back to ``"medium"`` when ``None``.
        tournament_id: Optional tournament id, recorded in the task
            metadata for observability ("which tournament generated this
            sub-task?").
        max_tasks: v0.37.0 H2: optional per-call cap on the number of
            corrective tasks returned. The caller (orchestrator) supplies
            the phase's remaining cumulative budget so plan inflation
            observed in real-world runs cannot bypass the cap by emitting
            a long bullet list in a single round. Bullets beyond the cap
            are silently dropped; the count lands in the ``dropped``
            field of the ``corrective_parser.parsed`` log event for
            forensics. ``None`` (default) preserves pre-v0.37.0 behaviour
            (no per-call cap).

    Returns:
        A list of :class:`Task` objects. Empty if ``text`` has no
        recognisable top-level bullets. Length never exceeds ``max_tasks``
        when supplied.
    """
    if not text or not text.strip():
        return []

    bullets = _split_top_level_bullets(text)
    tasks: list[Task] = []
    complexity_lit: Literal["simple", "medium", "complex"] = (
        phase_complexity  # type: ignore[assignment]
        if phase_complexity in {"simple", "medium", "complex"}
        else "medium"
    )

    for idx, body in enumerate(bullets, start=1):
        title = body.splitlines()[0].strip() if body.strip() else ""
        if not title:
            continue
        description = body.strip()
        new_id = f"{phase_id}.c{base_task_count + idx}"
        metadata: dict = {"origin": "phase_review_corrective"}
        if tournament_id is not None:
            metadata["tournament_id"] = tournament_id
        tasks.append(
            Task(
                id=new_id,
                phase_id=phase_id,
                title=title[:200],  # cap to avoid pathological titles
                description=description,
                complexity=complexity_lit,
                assigned_agent="developer",
                metadata=metadata,
            )
        )
        # v0.37.0 H2: stop as soon as we reach the per-call cap so we
        # don't materialise Tasks the caller would throw away.
        if max_tasks is not None and len(tasks) >= max_tasks:
            break

    # v0.37.0 H2: count of recognised top-level bullets that did NOT make
    # it into the returned list — either because we hit ``max_tasks`` or
    # because they were body-empty (filtered by the ``not title`` skip
    # above). Both are interesting to forensics.
    dropped = max(0, len(bullets) - len(tasks))
    logger.info(
        "corrective_parser.parsed",
        phase_id=phase_id,
        base_task_count=base_task_count,
        produced=len(tasks),
        dropped=dropped,
        max_tasks=max_tasks,
    )
    return tasks


def _split_top_level_bullets(text: str) -> list[str]:
    """Group lines into top-level bullets.

    A top-level bullet starts at column 0 (or with at most one leading
    space) with ``-``, ``*``, or ``\\d+.``. Subsequent indented lines
    accumulate into the current bullet's body. Blank lines are preserved
    inside a bullet (so a bullet's body can include separators).
    """
    bullets: list[str] = []
    current: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if _RE_TOP_LEVEL_BULLET.match(line):
            if current:
                bullets.append("\n".join(current).rstrip())
                current = []
            # Strip the leading marker; keep the rest as the first body line.
            m = _RE_TOP_LEVEL_BULLET.match(line)
            if m is not None:
                current.append(m.group(1))
        else:
            if current:
                current.append(line)
    if current:
        bullets.append("\n".join(current).rstrip())
    return [b for b in bullets if b.strip()]


__all__ = ["parse_corrective_direction"]
