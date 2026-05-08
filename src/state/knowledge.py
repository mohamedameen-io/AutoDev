"""Two-tier knowledge store (swarm + hive) with ranking, dedup, promotion.

Phase 9 replaces the Phase-4 stub. See the implementation plan Section E
for the on-disk layout and the reference algorithms this module implements.

On-disk layout
--------------
* Per-project **swarm** tier: ``<cwd>/.autodev/knowledge.jsonl``
* Global **hive** tier: ``~/.local/share/autodev/shared-learnings.jsonl``
  (override via config ``hive.path``)
* Per-project rejection list: ``<cwd>/.autodev/rejected_lessons.jsonl``

Key behaviors
-------------
1. **Deduplication** via bigram Jaccard similarity with a configurable
   threshold (default 0.6). Dedup is applied *within* a tier only: a
   swarm entry can have a near-duplicate in the hive (they're reconciled
   in :meth:`inject_block` when merging for injection).
2. **Capacity caps** — ``swarm_max_entries`` / ``hive_max_entries`` enforced
   on every write. Lowest-ranked entries evicted first.
3. **Ranking** — ``confidence * recency_factor * (1 + log(applied_count+1))``.
   ``recency_factor`` decays linearly over 30 days from 1.0 → 0.5.
4. **Injection** — :meth:`inject_block` returns a compact lessons string
   suitable for splicing into an agent prompt. Roles on the denylist
   receive an empty string (stateless/fact-finding roles must not be
   biased by prior lessons).
5. **Rejection log** — moved-out entries block re-learning via Jaccard
   similarity against every new candidate.
6. **Promotion** — swarm -> hive when an entry accumulates
   ``promotion_min_confirmations`` confirmations (merged duplicates) AND
   its confidence is ``>= promotion_min_confidence``.

Concurrency
-----------
All writes serialize through:
* :func:`state.lockfile.plan_lock` for the swarm (per-project)
* a hive-specific ``filelock`` under the hive parent dir for global state

All blocking I/O runs in :func:`asyncio.to_thread`. Files are written
atomically via ``tmp -> os.replace``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from filelock import FileLock, Timeout
from pydantic import BaseModel, Field

from config.schema import AutodevConfig, KnowledgeConfig
from autologging import get_logger
from state.lockfile import plan_lock
from state.paths import (
    knowledge_path,
    rejected_lessons_path,
)


logger = get_logger(__name__)


# Hard cap on a single JSONL line (64 KB). Lessons longer than this are
# truncated with a warning — the JSONL file stays parseable by downstream
# tools and older entries don't bloat the cache.
_MAX_LINE_BYTES: int = 64 * 1024
_RECENCY_WINDOW_S: float = 30 * 86400.0  # 30 days

Tier = Literal["swarm", "hive"]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class KnowledgeEntry(BaseModel):
    """A single lesson persisted in either the swarm or hive tier."""

    id: str
    timestamp: str
    role_source: str
    tier: Tier
    text: str
    confidence: float = 0.5
    applied_count: int = 0
    succeeded_after_count: int = 0
    confirmations: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class RejectedLesson(BaseModel):
    """An entry moved out of the knowledge store — blocks re-learning."""

    id: str
    text: str
    reason: str
    rejected_at: str


# v0.15.0: Tournament + escalation event types that drive cross-run lessons.
# Each event is converted into a structured ASI-style lesson string and
# persisted via :meth:`KnowledgeStore.record_tournament_event` so future
# tournament passes (and future runs of the same project) can consult prior
# wins, discards, escalations, course-corrections, and soft-blockers.
TournamentEventType = Literal[
    "winner_promoted",
    "discard",
    "escalation",
    "course_correction",
    "soft_blocker",
]


# Confidence map per event_type. Higher confidence => higher prompt weight via
# the existing ranking score. Tuned so winners outrank discards by ~1.7×, and
# soft-blockers (the strongest "do-not-repeat" signal we have) are highest of
# the failure events.
_EVENT_CONFIDENCE: dict[TournamentEventType, float] = {
    "winner_promoted": 0.85,
    "discard": 0.5,
    "escalation": 0.7,
    "course_correction": 0.6,
    "soft_blocker": 0.8,
}


@dataclass
class TournamentEvent:
    """A single tournament / escalation event suitable for lessons recording.

    The :class:`KnowledgeStore.record_tournament_event` helper converts an
    instance of this dataclass into a structured ASI-style lesson string
    and persists it via the regular :meth:`KnowledgeStore.record` write
    path.

    Attributes:
        event_type: One of the values in :data:`TournamentEventType` —
            governs both the structured prefix used in the lesson text
            and the confidence assigned to the entry.
        family: Short identifier for the source subsystem
            (e.g. ``"plan-tournament"``, ``"multi-branch-meta-merge"``,
            ``"execute-phase"``, ``"prm"``). Surfaces in the rendered
            lesson so future runs can attribute prior context.
        hypothesis: Compact natural-language summary of what was
            tried / found.
        evidence: Supporting evidence string (e.g. judge counts, file
            paths, error excerpts). Truncation is handled inside
            :meth:`KnowledgeStore.record` via ``_truncate``.
        rollback_reason: Optional rollback / discard reason — populated
            for ``discard`` events so future passes can avoid the same
            structural mistake.
        next_action_hint: Optional natural-language hint for the next
            attempt — recorded as part of the lesson body.
    """

    event_type: TournamentEventType
    family: str
    hypothesis: str
    evidence: str
    rollback_reason: str | None = None
    next_action_hint: str | None = None
    # v0.18.0 B1: branch lane label for lane-aware lesson injection. When
    # set (e.g. ``"distant-scout"``, ``"local-tweak"``), the entry is
    # tagged so :meth:`KnowledgeStore.inject_block` can filter to lessons
    # learned in matching lanes (or universal lane-less ones). ``None``
    # (default) marks the lesson as universal — injected regardless of
    # the consuming branch's lane.
    lane: str | None = None

    def to_lesson_text(self) -> str:
        """Render the event as a single ASI-style lesson string.

        The format is line-oriented and machine-greppable so test scaffolds
        can assert on stable substrings:

            EVENT: <event_type>
            FAMILY: <family>
            HYPOTHESIS: <hypothesis>
            EVIDENCE: <evidence>
            ROLLBACK: <rollback_reason>   # optional
            NEXT: <next_action_hint>      # optional
        """
        parts = [
            f"EVENT: {self.event_type}",
            f"FAMILY: {self.family}",
            f"HYPOTHESIS: {self.hypothesis}",
            f"EVIDENCE: {self.evidence}",
        ]
        if self.rollback_reason:
            parts.append(f"ROLLBACK: {self.rollback_reason}")
        if self.next_action_hint:
            parts.append(f"NEXT: {self.next_action_hint}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_to_epoch(ts: str) -> float:
    """Parse an ISO timestamp; return 0.0 on any failure (never raises)."""
    try:
        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0


def _recency_factor(ts_iso: str, now_epoch: float) -> float:
    """Linear decay from 1.0 (now) to 0.5 (30d old), floor at 0.5."""
    ts_epoch = _timestamp_to_epoch(ts_iso)
    if ts_epoch <= 0.0:
        return 0.5
    age = max(0.0, now_epoch - ts_epoch)
    if age >= _RECENCY_WINDOW_S:
        return 0.5
    return 1.0 - 0.5 * (age / _RECENCY_WINDOW_S)


def _bigrams(s: str) -> set[tuple[str, str]]:
    s = s.lower()
    return {(s[i], s[i + 1]) for i in range(len(s) - 1)}


def jaccard_bigrams(a: str, b: str) -> float:
    """Character-bigram Jaccard similarity.

    Returns 0.0 if either input has no bigrams (i.e. length < 2). Returns
    1.0 for identical inputs. Empty + empty -> 0.0 (treat them as
    incomparable rather than a perfect match).
    """
    A = _bigrams(a)
    B = _bigrams(b)
    if not A or not B:
        return 0.0
    union = A | B
    if not union:
        return 0.0
    return len(A & B) / len(union)


def _truncate(text: str) -> tuple[str, bool]:
    """Ensure a JSONL line for ``text`` fits under :data:`_MAX_LINE_BYTES`.

    Returns ``(maybe_truncated_text, was_truncated)``. We reserve some
    slack for the JSON overhead (keys + metadata); truncate ``text`` to
    roughly half the line cap so there's headroom. Downstream writers
    may still need to check raw JSON length (see :func:`_json_line`).
    """
    if len(text.encode("utf-8")) <= _MAX_LINE_BYTES // 2:
        return text, False
    cap = _MAX_LINE_BYTES // 2
    # Cut on code-point boundaries.
    encoded = text.encode("utf-8")[:cap]
    try:
        return encoded.decode("utf-8", errors="ignore"), True
    except UnicodeDecodeError:
        return encoded.decode("utf-8", errors="ignore"), True


def _default_hive_path() -> Path:
    """Default hive tier path: ``~/.local/share/autodev/shared-learnings.jsonl``."""
    return Path("~/.local/share/autodev/shared-learnings.jsonl").expanduser()


def _hive_lock_path(hive_file: Path) -> Path:
    """Return the hive lock file path (sibling of the hive JSONL)."""
    return hive_file.parent / ".lock"


@contextlib.asynccontextmanager
async def _hive_lock(hive_file: Path, timeout_s: float = 30.0) -> AsyncIterator[None]:
    """Cross-process lock over the hive tier's parent dir."""
    hive_file.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(
        str(_hive_lock_path(hive_file)), timeout=timeout_s, thread_local=False
    )
    try:
        await asyncio.to_thread(lock.acquire)
    except Timeout as exc:  # pragma: no cover - timing
        raise TimeoutError(f"could not acquire hive lock within {timeout_s}s") from exc
    try:
        yield
    finally:
        await asyncio.to_thread(lock.release)


# ---------------------------------------------------------------------------
# Low-level JSONL helpers
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, content: str) -> None:
    """Atomic ``tmp -> os.replace`` write, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex[:6]}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            logger.warning("knowledge.jsonl.skip_corrupt", path=str(path), line=s[:80])
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    """Rewrite ``path`` with ``entries`` atomically."""
    if not entries:
        # Preserve an empty file so readers don't see a missing path.
        _atomic_write(path, "")
        return
    lines: list[str] = []
    for obj in entries:
        line = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        if len(line.encode("utf-8")) > _MAX_LINE_BYTES:
            # Defensive: shouldn't happen (we truncate on record), but
            # never let a single oversized line wedge the file.
            logger.warning(
                "knowledge.jsonl.skip_oversized_line",
                path=str(path),
                bytes=len(line.encode("utf-8")),
            )
            continue
        lines.append(line)
    _atomic_write(path, "\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# KnowledgeStore
# ---------------------------------------------------------------------------


class KnowledgeStore:
    """Two-tier knowledge store. Safe to instantiate without a loaded config.

    When ``cfg`` is ``None``, defaults are used (useful for CLI-level
    read-only summaries). Full orchestrator integration passes the loaded
    :class:`AutodevConfig`.
    """

    def __init__(
        self,
        cwd: Path,
        cfg: AutodevConfig | None = None,
        hive_path: Path | None = None,
    ) -> None:
        self._cwd = Path(cwd)
        self._cfg = cfg
        # Resolve hive path precedence: explicit > cfg.hive.path > default.
        if hive_path is not None:
            self._hive_path = Path(hive_path).expanduser()
        elif cfg is not None:
            self._hive_path = Path(cfg.hive.path).expanduser()
        else:
            self._hive_path = _default_hive_path()
        self._log = logger.bind(component="knowledge")

    # --- Accessors ----------------------------------------------------

    @property
    def cwd(self) -> Path:
        return self._cwd

    @property
    def hive_path(self) -> Path:
        return self._hive_path

    @property
    def knowledge_config(self) -> KnowledgeConfig:
        """Return the effective KnowledgeConfig (default when cfg is None)."""
        if self._cfg is None:
            return KnowledgeConfig()
        return self._cfg.knowledge

    @property
    def hive_enabled(self) -> bool:
        """Effective hive enablement: both HiveConfig and KnowledgeConfig must agree."""
        kcfg = self.knowledge_config
        if not kcfg.hive_enabled:
            return False
        if self._cfg is None:
            return True
        return self._cfg.hive.enabled

    @property
    def enabled(self) -> bool:
        return self.knowledge_config.enabled

    # --- Compatibility shim for Phase-4 orchestrator callers --------

    def _denylist(self) -> set[str]:
        return set(self.knowledge_config.denylist_roles)

    # --- Public API --------------------------------------------------

    async def record(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> KnowledgeEntry | None:
        """Record a new lesson.

        Accepts two call styles to preserve the Phase-4 stub contract:

        1. ``record(text: str, role_source: str, confidence: float = 0.5,
           metadata: dict | None = None)`` — the full-fidelity Phase-9 form.
        2. ``record(lesson: dict)`` — the Phase-4 stub form, where the dict
           contains keys ``text`` / ``lesson`` / ``message`` for the body,
           ``role`` / ``role_source`` for the source, ``confidence``, and
           ``metadata``. Additional keys are merged into ``metadata``.

        Returns the persisted :class:`KnowledgeEntry`, the merged entry when
        the candidate was a duplicate, or ``None`` when the candidate was
        blocked by the rejection list or the store is disabled.
        """
        text, role_source, confidence, metadata = _normalize_record_args(
            *args, **kwargs
        )
        if not self.enabled:
            self._log.debug("knowledge.record.disabled")
            return None
        if not text or not text.strip():
            self._log.debug("knowledge.record.empty_text")
            return None

        text_trunc, was_trunc = _truncate(text.strip())
        if was_trunc:
            self._log.warning("knowledge.record.truncated", role=role_source)
        meta = dict(metadata or {})
        if was_trunc:
            meta.setdefault("truncated", True)

        kcfg = self.knowledge_config
        swarm_file = knowledge_path(self._cwd)
        rejected_file = rejected_lessons_path(self._cwd)

        async with plan_lock(self._cwd):
            # 1. Rejection guard.
            rejected = await asyncio.to_thread(_read_jsonl, rejected_file)
            for r in rejected:
                r_text = str(r.get("text", ""))
                if (
                    r_text
                    and jaccard_bigrams(text_trunc, r_text) >= kcfg.dedup_threshold
                ):
                    self._log.info(
                        "knowledge.record.rejected_duplicate",
                        reason=r.get("reason"),
                    )
                    return None

            # 2. Swarm dedup.
            swarm_raw = await asyncio.to_thread(_read_jsonl, swarm_file)
            entries = [KnowledgeEntry.model_validate(d) for d in swarm_raw]

            dup_index: int | None = None
            for i, existing in enumerate(entries):
                if jaccard_bigrams(text_trunc, existing.text) >= kcfg.dedup_threshold:
                    dup_index = i
                    break

            if dup_index is not None:
                existing = entries[dup_index]
                existing.confidence = min(1.0, existing.confidence + 0.1)
                existing.confirmations += 1
                existing.metadata.update(meta)
                existing.timestamp = _now_iso()
                entries[dup_index] = existing
                await asyncio.to_thread(
                    _write_jsonl,
                    swarm_file,
                    [e.model_dump(mode="json") for e in entries],
                )
                self._log.info(
                    "knowledge.record.merged",
                    id=existing.id,
                    confirmations=existing.confirmations,
                )
                merged = existing
                promoted = await self._promote_if_qualified(merged)
                if promoted:
                    self._log.info("knowledge.promoted", id=merged.id)
                return merged

            # 3. Fresh entry.
            new = KnowledgeEntry(
                id=_fresh_id(text_trunc, role_source),
                timestamp=_now_iso(),
                role_source=role_source,
                tier="swarm",
                text=text_trunc,
                confidence=max(0.0, min(1.0, confidence)),
                applied_count=0,
                succeeded_after_count=0,
                confirmations=1,
                metadata=meta,
            )
            entries.append(new)

            # 4. Enforce swarm cap (evict lowest-ranked).
            if len(entries) > kcfg.swarm_max_entries:
                entries = self._evict_to_cap(entries, kcfg.swarm_max_entries)

            await asyncio.to_thread(
                _write_jsonl,
                swarm_file,
                [e.model_dump(mode="json") for e in entries],
            )
            self._log.info(
                "knowledge.record.new",
                id=new.id,
                role=role_source,
                confidence=new.confidence,
            )

        # 5. Promotion is outside the swarm lock (uses hive lock).
        promoted = await self._promote_if_qualified(new)
        if promoted:
            self._log.info("knowledge.promoted", id=new.id)
        return new

    async def record_tournament_event(
        self,
        event: TournamentEvent,
    ) -> KnowledgeEntry | None:
        """Record a :class:`TournamentEvent` as a lesson in the swarm tier.

        Wraps :meth:`record` with structured ASI-style text built by
        :meth:`TournamentEvent.to_lesson_text`. Confidence is set per
        :data:`_EVENT_CONFIDENCE`. ``role_source`` is fixed to ``"critic_t"``
        so the entry surfaces to per-pass critics via the regular
        :meth:`inject_block` wiring at ``plan_phase.py`` /
        ``execute_phase.py`` call sites.

        Returns the persisted entry (or merged duplicate), or ``None`` when
        the store is disabled. Errors propagate to the caller — callers are
        encouraged to swallow knowledge errors with a WARNING since lessons
        recording must never block tournament progress.
        """
        confidence = _EVENT_CONFIDENCE.get(event.event_type, 0.5)
        text = event.to_lesson_text()
        metadata: dict[str, Any] = {
            "event_type": event.event_type,
            "family": event.family,
        }
        if event.rollback_reason is not None:
            metadata["rollback_reason"] = event.rollback_reason
        if event.next_action_hint is not None:
            metadata["next_action_hint"] = event.next_action_hint
        # v0.18.0 B1: persist the optional lane tag so lane-aware injection
        # (:meth:`inject_block`) can filter universal vs lane-specific
        # lessons. ``None`` (default) leaves the tag absent → universal.
        if event.lane is not None:
            metadata["lane"] = event.lane
        return await self.record(
            text,
            role_source="critic_t",
            confidence=confidence,
            metadata=metadata,
        )

    async def reject(self, lesson_id: str, reason: str) -> None:
        """Remove a lesson from the swarm and append it to rejected_lessons.jsonl."""
        swarm_file = knowledge_path(self._cwd)
        rejected_file = rejected_lessons_path(self._cwd)
        async with plan_lock(self._cwd):
            entries_raw = await asyncio.to_thread(_read_jsonl, swarm_file)
            entries = [KnowledgeEntry.model_validate(d) for d in entries_raw]
            target = next((e for e in entries if e.id == lesson_id), None)
            if target is None:
                self._log.info("knowledge.reject.not_found", id=lesson_id)
                return
            remaining = [e for e in entries if e.id != lesson_id]
            await asyncio.to_thread(
                _write_jsonl,
                swarm_file,
                [e.model_dump(mode="json") for e in remaining],
            )
            rejected_raw = await asyncio.to_thread(_read_jsonl, rejected_file)
            rejected_raw.append(
                RejectedLesson(
                    id=target.id,
                    text=target.text,
                    reason=reason,
                    rejected_at=_now_iso(),
                ).model_dump(mode="json")
            )
            await asyncio.to_thread(_write_jsonl, rejected_file, rejected_raw)
        self._log.info("knowledge.reject.applied", id=lesson_id, reason=reason)

    async def read_all(
        self, tier: Literal["swarm", "hive", "both"] = "both"
    ) -> list[KnowledgeEntry]:
        """Read and validate entries from one or both tiers."""
        out: list[KnowledgeEntry] = []
        if tier in ("swarm", "both"):
            swarm_raw = await asyncio.to_thread(_read_jsonl, knowledge_path(self._cwd))
            for d in swarm_raw:
                try:
                    out.append(KnowledgeEntry.model_validate(d))
                except Exception:
                    self._log.warning("knowledge.read.bad_swarm_entry", id=d.get("id"))
        if tier in ("hive", "both") and self.hive_enabled:
            hive_raw = await asyncio.to_thread(_read_jsonl, self._hive_path)
            for d in hive_raw:
                try:
                    out.append(KnowledgeEntry.model_validate(d))
                except Exception:
                    self._log.warning("knowledge.read.bad_hive_entry", id=d.get("id"))
        return out

    async def read_rejected(self) -> list[RejectedLesson]:
        raw = await asyncio.to_thread(_read_jsonl, rejected_lessons_path(self._cwd))
        out: list[RejectedLesson] = []
        for d in raw:
            try:
                out.append(RejectedLesson.model_validate(d))
            except Exception:
                self._log.warning("knowledge.read.bad_rejected_entry")
        return out

    async def inject_block(
        self,
        role: str,
        limit: int | None = None,
        *,
        task_id: str | None = None,  # preserved for Phase-4 caller compatibility
        lane: str | None = None,
    ) -> str:
        """Return the compact ``Lessons learned:`` block for a given role.

        Returns ``""`` when:
            * the role is on the denylist (stateless/fact-finding agents),
            * the knowledge system is disabled globally,
            * no lessons are available,
            * injection would be empty after ranking/merging.

        Otherwise returns a string of the form::

            Lessons learned from prior work:
            - [conf:0.80] <lesson text>
            - [conf:0.75] <lesson text>

        v0.18.0 B1 lane-aware filter: when ``lane`` is provided AND
        :attr:`KnowledgeConfig.lane_aware_injection_enabled` is True
        (default), entries are filtered so only lessons whose
        ``metadata["lane"]`` matches OR whose lane tag is absent (universal)
        survive. When ``lane`` is None or the toggle is False, no lane
        filter is applied — the legacy behavior is preserved.
        """
        if not self.enabled:
            return ""
        if role in self._denylist():
            self._log.debug("knowledge.inject.skip_denylist", role=role)
            return ""

        kcfg = self.knowledge_config
        cap = limit if limit is not None else kcfg.max_inject_count
        if cap <= 0:
            return ""

        # v0.18.0 B1: lane filter predicate. Universal lessons (no
        # ``metadata["lane"]``) are always included; lane-tagged lessons
        # match only when the consuming branch's lane equals the tag.
        lane_aware = lane is not None and getattr(
            kcfg, "lane_aware_injection_enabled", True
        )

        def _lane_match(entry: KnowledgeEntry) -> bool:
            if not lane_aware:
                return True
            entry_lane = entry.metadata.get("lane") if entry.metadata else None
            return entry_lane is None or entry_lane == lane

        # Rank each tier independently; merge with swarm-first priority.
        swarm = await self.read_all(tier="swarm")
        hive: list[KnowledgeEntry] = []
        if self.hive_enabled:
            hive = await self.read_all(tier="hive")
        if lane_aware:
            swarm = [e for e in swarm if _lane_match(e)]
            hive = [e for e in hive if _lane_match(e)]

        now = time.time()
        swarm_ranked = sorted(
            swarm,
            key=lambda e: self._rank_with_ts(e, now),
            reverse=True,
        )
        hive_ranked = sorted(
            hive,
            key=lambda e: self._rank_with_ts(e, now),
            reverse=True,
        )

        # Swarm-first merge with cross-tier Jaccard dedup (swarm wins).
        merged: list[KnowledgeEntry] = []
        for e in swarm_ranked:
            merged.append(e)
            if len(merged) >= cap:
                break
        for e in hive_ranked:
            if len(merged) >= cap:
                break
            if any(
                jaccard_bigrams(e.text, m.text) >= kcfg.dedup_threshold for m in merged
            ):
                continue
            merged.append(e)

        if not merged:
            return ""

        selected = merged[:cap]

        # Increment applied_count for each selected swarm entry (read-modify-write).
        swarm_ids = {e.id for e in selected if e.tier == "swarm"}
        if swarm_ids:
            try:
                swarm_file = knowledge_path(self._cwd)
                async with plan_lock(self._cwd):
                    swarm_raw = await asyncio.to_thread(_read_jsonl, swarm_file)
                    updated = False
                    for d in swarm_raw:
                        if d.get("id") in swarm_ids:
                            d["applied_count"] = int(d.get("applied_count", 0)) + 1
                            updated = True
                    if updated:
                        await asyncio.to_thread(_write_jsonl, swarm_file, swarm_raw)
            except Exception:  # noqa: BLE001
                self._log.warning("knowledge.inject.applied_count_update_failed")

        lines = ["Lessons learned from prior work:"]
        for e in selected:
            lines.append(f"- [conf:{e.confidence:.2f}] {_one_line(e.text)}")
        return "\n".join(lines)

    # --- Internals ---------------------------------------------------

    def _rank(self, entry: KnowledgeEntry) -> float:
        return self._rank_with_ts(entry, time.time())

    def _rank_with_ts(self, entry: KnowledgeEntry, now_epoch: float) -> float:
        """``confidence * recency_factor * (1 + log(applied_count + 1))``."""
        recency = _recency_factor(entry.timestamp, now_epoch)
        applied_boost = 1.0 + math.log(max(0, entry.applied_count) + 1)
        return float(entry.confidence) * recency * applied_boost

    def _evict_to_cap(
        self, entries: list[KnowledgeEntry], cap: int
    ) -> list[KnowledgeEntry]:
        if cap <= 0:
            return []
        if len(entries) <= cap:
            return list(entries)
        now = time.time()
        ranked = sorted(entries, key=lambda e: self._rank_with_ts(e, now), reverse=True)
        return ranked[:cap]

    async def _promote_if_qualified(self, entry: KnowledgeEntry) -> bool:
        """Copy an entry to the hive tier if it meets promotion criteria.

        Criteria:
            * hive enabled (both HiveConfig.enabled and KnowledgeConfig.hive_enabled)
            * ``entry.confirmations >= promotion_min_confirmations``
            * ``entry.confidence >= promotion_min_confidence``
            * no near-duplicate already in the hive (idempotency)

        Returns True if the entry was newly promoted.
        """
        if not self.hive_enabled:
            return False
        kcfg = self.knowledge_config
        if entry.confirmations < kcfg.promotion_min_confirmations:
            return False
        if entry.confidence < kcfg.promotion_min_confidence:
            return False

        hive_file = self._hive_path
        async with _hive_lock(hive_file):
            hive_raw = await asyncio.to_thread(_read_jsonl, hive_file)
            # Idempotency: skip if a near-duplicate already exists in the hive.
            for d in hive_raw:
                text = str(d.get("text", ""))
                if text and jaccard_bigrams(entry.text, text) >= kcfg.dedup_threshold:
                    return False
            promoted = entry.model_copy(
                update={
                    "id": _fresh_id(entry.text, entry.role_source, salt="hive"),
                    "tier": "hive",
                    "timestamp": _now_iso(),
                }
            )
            hive_raw.append(promoted.model_dump(mode="json"))

            # Enforce hive cap by lowest-ranked eviction.
            entries_for_cap = [KnowledgeEntry.model_validate(d) for d in hive_raw]
            if len(entries_for_cap) > kcfg.hive_max_entries:
                entries_for_cap = self._evict_to_cap(
                    entries_for_cap, kcfg.hive_max_entries
                )
            await asyncio.to_thread(
                _write_jsonl,
                hive_file,
                [e.model_dump(mode="json") for e in entries_for_cap],
            )
        return True


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _one_line(text: str) -> str:
    """Collapse newlines + excess whitespace for a single-line prompt slot."""
    return " ".join(text.split())


def _fresh_id(text: str, role: str, salt: str = "") -> str:
    """Deterministic-ish id; stable enough for logs, collision-safe via uuid tail."""
    h = hashlib.sha256(f"{role}|{salt}|{text}|{time.time_ns()}".encode("utf-8"))
    return f"{role[:8]}-{h.hexdigest()[:10]}-{uuid.uuid4().hex[:4]}"


def _normalize_record_args(
    *args: Any, **kwargs: Any
) -> tuple[str, str, float, dict[str, Any]]:
    """Accept both Phase-9 positional form and Phase-4 dict form.

    Returns ``(text, role_source, confidence, metadata)``.
    """
    # Phase-9 form: (text, role_source, confidence=0.5, metadata=None)
    if args and isinstance(args[0], str):
        text: str = args[0]
        role_source: str = (
            args[1] if len(args) > 1 else kwargs.get("role_source", "unknown")
        )
        confidence: float = (
            float(args[2]) if len(args) > 2 else float(kwargs.get("confidence", 0.5))
        )
        metadata = kwargs.get("metadata") or {}
        return text, role_source, confidence, dict(metadata)

    # Phase-4 form: a single dict argument (or keyword `lesson=...`).
    payload: dict[str, Any] | None = None
    if args and isinstance(args[0], dict):
        payload = dict(args[0])
    elif "lesson" in kwargs and isinstance(kwargs["lesson"], dict):
        payload = dict(kwargs["lesson"])
    if payload is None:
        # Fall back to kwargs-only: build a pseudo-payload.
        payload = dict(kwargs)

    text = (
        payload.pop("text", None)
        or payload.pop("lesson", None)
        or payload.pop("message", None)
        or ""
    )
    role_source = payload.pop("role_source", None) or payload.pop("role", "unknown")
    confidence = float(payload.pop("confidence", 0.5))
    metadata = payload.pop("metadata", None) or {}
    # Anything left in payload becomes extra metadata.
    if payload:
        metadata = {**payload, **dict(metadata)}
    return str(text), str(role_source), confidence, dict(metadata)


__all__ = [
    "KnowledgeEntry",
    "KnowledgeStore",
    "RejectedLesson",
    "TournamentEvent",
    "TournamentEventType",
    "jaccard_bigrams",
]
