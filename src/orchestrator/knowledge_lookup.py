"""v0.32.0 Phase 4.2: knowledge-base lookup before refine.

The cross-run knowledge store (``<workspace>/.autodev/knowledge.jsonl``)
accumulates lessons every time a task discards, soft-blocks, or course-
corrects. Pre-Phase-4 the store was read at *phase setup* (via
:meth:`KnowledgeStore.inject_block`) but never consulted at *retry
decision* time — so when a task entered the escalation ladder, the
critic prompt had no awareness of "we already tried X and Y on this
task signature in the last week".

This module closes that gap with a tight, async, time-bounded helper
that the orchestrator's retry path calls *before* injecting the
``STUCK_CONTEXT`` block. The helper:

* Reads the swarm tier (per-project knowledge.jsonl).
* Filters to entries with ``metadata["event_type"] in
  {"discard", "soft_blocker"}`` — these are the genuinely informative
  failure events.
* Filters to entries newer than ``threshold_days`` so old, stale
  guidance does not crowd out fresh signal.
* Filters by task-id similarity OR ``metadata["task_signature"]``
  similarity — see :mod:`state.knowledge` for the signature scheme.
* Returns up to ``limit`` short text summaries, suitable for splicing
  into the next critic prompt.

The whole lookup is wrapped in :func:`asyncio.wait_for` with a 100ms
default timeout. A slow / wedged knowledge store MUST NOT block the
retry loop — degradation is graceful (returns ``[]``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from autologging import get_logger


if TYPE_CHECKING:
    from state.knowledge import KnowledgeEntry


logger = get_logger(__name__)


# Default lookup timeout in seconds. A slow KB query on a large
# workspace must not stall the retry loop — 100ms is generous enough
# that a healthy local read completes comfortably while still bounding
# the worst case.
_DEFAULT_LOOKUP_TIMEOUT_S: float = 0.1


# Event types that are useful as "we already tried this" context.
# winner_promoted entries are excluded — they are positive signal that
# the *whole project* learned something, not a per-task failure trace.
_RELEVANT_EVENT_TYPES: frozenset[str] = frozenset({"discard", "soft_blocker"})


async def lookup_recent_failures(
    orch: Any,
    task_id: str,
    threshold_days: int = 7,
    limit: int = 5,
    *,
    timeout_s: float = _DEFAULT_LOOKUP_TIMEOUT_S,
) -> list[str]:
    """Return up to ``limit`` short summaries of recent failures for ``task_id``.

    Args:
        orch: Orchestrator-shaped object exposing ``knowledge`` —
            either a :class:`state.knowledge.KnowledgeStore` instance
            or any object with the same ``read_all`` API. Tests pass
            a stub.
        task_id: The task whose retry path is consulting the store.
            Used for similarity filtering — entries with a matching
            ``task_id`` (recorded under ``metadata["task_id"]``) or a
            matching ``metadata["task_signature"]`` are preferred.
        threshold_days: Drop entries older than this many days.
            Default 7. Old entries usually reflect a different code
            shape — better to leave them out than risk anchoring the
            critic on stale guidance.
        limit: Cap the number of returned summaries. Default 5.
            Keeps the critic prompt budget bounded.
        timeout_s: Wall-clock cap on the underlying store read.
            Default 100ms. On timeout the helper returns ``[]`` so
            the caller can fall through to its existing critic dispatch
            without blocking.

    Returns:
        A list of human-readable summaries, each ≤200 chars, ready to
        splice into a ``STUCK_CONTEXT`` block. Empty list when:

        * the store is missing / disabled,
        * no entries match the filters,
        * the store query times out,
        * any unexpected exception fires (logged, not raised).
    """
    knowledge = getattr(orch, "knowledge", None)
    if knowledge is None:
        return []

    try:
        entries = await asyncio.wait_for(
            _read_swarm_entries(knowledge),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        logger.info(
            "knowledge_lookup.timeout",
            task_id=task_id,
            timeout_s=timeout_s,
        )
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "knowledge_lookup.read_failed",
            task_id=task_id,
            err=str(exc),
        )
        return []

    if not entries:
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, threshold_days))
    matched: list[str] = []

    for entry in entries:
        if not _is_relevant_event(entry):
            continue
        if not _is_recent(entry, cutoff):
            continue
        if not _matches_task(entry, task_id):
            continue
        summary = _summarize(entry)
        if summary:
            matched.append(summary)
        if len(matched) >= limit:
            break

    return matched


async def _read_swarm_entries(knowledge: Any) -> list["KnowledgeEntry"]:
    """Read swarm-tier entries via the store's standard async API.

    Kept as a thin shim so the timeout wrapper above has exactly one
    awaitable to guard. ``read_all(tier="swarm")`` is the public method
    on :class:`state.knowledge.KnowledgeStore`; tests can stub it on a
    duck-typed object.
    """
    return await knowledge.read_all(tier="swarm")


def _is_relevant_event(entry: "KnowledgeEntry") -> bool:
    """True when the entry was recorded for a discard / soft_blocker event."""
    metadata = getattr(entry, "metadata", None) or {}
    return metadata.get("event_type") in _RELEVANT_EVENT_TYPES


def _is_recent(entry: "KnowledgeEntry", cutoff: datetime) -> bool:
    """True when ``entry.timestamp`` is newer than ``cutoff``.

    Falls back to True (include) on any parse error — better to surface
    a possibly-stale entry than to silently drop it with no signal.
    """
    ts = getattr(entry, "timestamp", None)
    if not isinstance(ts, str) or not ts:
        return True
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed >= cutoff


def _matches_task(entry: "KnowledgeEntry", task_id: str) -> bool:
    """True when the entry is plausibly about the same task / signature.

    Match strategy:

    1. Exact match on ``metadata["task_id"]`` (the strongest signal —
       same task, same workspace).
    2. Exact match on ``metadata["task_signature"]`` (a hash defined in
       :func:`state.knowledge.compute_task_signature`; matches across
       resets / re-creations of "the same task").
    3. Fall through to ``True`` when the entry has neither key — the
       lesson predates the metadata addition and we'd rather show a
       slightly less-targeted lesson than silently drop it.
    """
    metadata = getattr(entry, "metadata", None) or {}
    entry_task = metadata.get("task_id")
    if isinstance(entry_task, str) and entry_task == task_id:
        return True
    entry_sig = metadata.get("task_signature")
    if isinstance(entry_sig, str):
        # Signature present — only match when both sides agree. This
        # keeps the helper specific (a stale entry from a different
        # task signature won't pollute the prompt).
        return False
    # No identifying metadata at all — treat as universal.
    return True


def _summarize(entry: "KnowledgeEntry") -> str:
    """Render an entry as a single ≤200-char line."""
    text = getattr(entry, "text", "")
    if not isinstance(text, str):
        return ""
    text = text.strip()
    if not text:
        return ""
    flat = " ".join(text.split())
    return flat[:200]


__all__ = ["lookup_recent_failures"]
