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
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field

from config.schema import AutodevConfig, DecayCurveConfig, KnowledgeConfig
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

# v0.35.0 C1: low-yield quarantine thresholds. An entry whose injected
# count exceeds the floor while its observed success ratio stays below
# the ceiling is taken out of the injection rotation. Soft-flagged
# (``quarantined=True``) — the entry stays on disk so forensics can
# replay the decision; a sibling JSONL audit records the counts at
# decision time. Constants live at module scope so tests can shadow
# them without touching the algorithm.
_QUARANTINE_APPLIED_MIN: int = 10
_QUARANTINE_SUCCESS_RATIO_MAX: float = 0.1

# v0.35.0 C2: critic-evidence quality markers. Strings that critics emit
# when they are essentially abstaining — they should not bump
# confirmations. Match is prefix-line OR contains, lower-cased.
_CRITIC_THIN_EVIDENCE_MARKERS: tuple[str, ...] = (
    "The evidence provided is critically thin",
    "The evidence is critically thin",
    "evidence is critically thin",
)
_CRITIC_NOISE_MARKERS: tuple[str, ...] = (
    "coder adapter failure",
    "infrastructure noise",
)
_CRITIC_MIN_EVIDENCE_CHARS: int = 80

# v0.35.0 C3: 7-day confirmations decay curve key. Used by
# :meth:`KnowledgeStore._decay_curve_for` to bias injection ranking
# toward recently-applied entries without affecting raw counts the
# quarantine evaluator inspects.
_CONFIRMATIONS_DECAY_CURVE_KEY: str = "confirmations_7d"

Tier = Literal["swarm", "hive"]


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class KnowledgeEntry(BaseModel):
    """A single lesson persisted in either the swarm or hive tier.

    The ``metadata`` dict is intentionally schema-less so emitters can
    attach context without forcing a model migration. v0.32.0 Phase 4.3
    standardises a small set of optional keys consumed by the
    knowledge-aware retry path; all are optional with no defaults so
    older lessons stay readable:

    * ``event_type`` — :data:`TournamentEventType`. Set by
      :meth:`KnowledgeStore.record_tournament_event`.
    * ``family`` — short subsystem identifier (e.g. ``"execute-phase"``).
    * ``task_id`` — the AutoDev task that produced the lesson, when
      known. Used by :func:`orchestrator.knowledge_lookup.lookup_recent_failures`
      for direct-id similarity.
    * ``task_signature`` — sha256 hex digest from
      :func:`compute_task_signature`. Used for cross-reset similarity
      (the same logical task that was recreated will match).
    * ``kb_entry_type`` — one of
      ``"repetition_loop" | "thin_evidence" | "course_correction" |
      "soft_block" | "autoreason_converged"``. Lets the lookup helper
      filter for genuinely informative entries.
    * ``tactic_tried`` — short label (``"refine_x"``, ``"pivot_y"``,
      ``"web_search"``, etc.) so the next attempt can pick a *different*
      tactic.
    * ``resolution`` — one of ``"human_required" | "worked" |
      "failed_again"`` so retroactive learning can reinforce the
      tactic / suggestion that actually unblocked the task.
    """

    # v0.35.0 C1: ``extra="ignore"`` is explicit so legacy JSONL lines
    # carrying fields we have since dropped continue to deserialize
    # cleanly (additive-only schema policy).
    model_config = ConfigDict(extra="ignore")

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
    # v0.35.0 C1: soft-flag quarantine — entries with a high
    # applied_count and near-zero success ratio stop being injected.
    # Default False so legacy JSONL deserializes as not-quarantined.
    quarantined: bool = False
    # v0.35.0 C1 / C3: ISO-8601 UTC timestamp of the most recent
    # injection. Drives the ``confirmations_7d`` decay curve and is
    # written under the same lock as the applied_count increment.
    # ``None`` on legacy entries and on entries that have never been
    # injected since the v0.35.0 upgrade.
    last_applied_at: datetime | None = None


class RejectedLesson(BaseModel):
    """An entry moved out of the knowledge store — blocks re-learning."""

    id: str
    text: str
    reason: str
    rejected_at: str


@dataclass(frozen=True)
class QuarantineAuditEntry:
    """v0.35.0 C1: one decision in the parallel quarantine audit trail.

    Persisted as a single JSONL line to
    ``<autodev_dir>/quarantine_audit.jsonl`` whenever an entry is
    flipped to ``quarantined=True``. Immutable (frozen) so the audit
    file is purely append-only — replay logic can reconstruct the
    full decision history.
    """

    entry_id: str
    applied_count: int
    succeeded_after_count: int
    ratio: float
    reason: str
    decided_at: str  # ISO-8601 UTC


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


def _recency_factor(
    ts_iso: str,
    now_epoch: float,
    *,
    curve: "DecayCurveConfig | None" = None,
) -> float:
    """Decay factor for a lesson's confidence based on its age.

    Default behavior (when ``curve`` is None): linear decay from 1.0
    (now) to 0.5 (30d old), floor at 0.5. This is the byte-identical
    legacy curve and MUST remain unchanged for backward-compat.

    v0.20.0 B1: when a :class:`config.schema.DecayCurveConfig` is
    supplied, the curve is parameterized by ``half_life_days`` and
    ``floor``. The implementation chooses a piecewise-linear decay so
    the curve hits ``floor + (1 - floor) / 2`` at exactly
    ``half_life_days`` and reaches ``floor`` at ``2 * half_life_days``,
    after which it stays at the floor.

    With ``half_life_days=15`` and ``floor=0.5`` (the default
    :class:`DecayCurveConfig` values), the curve is byte-identical to
    the legacy linear decay — the half-life of 15d (where the legacy
    curve passes through 0.75) puts the new curve through the same
    point, and ``2 * 15 = 30`` matches the legacy 30-day window.
    """
    ts_epoch = _timestamp_to_epoch(ts_iso)
    if curve is None:
        # Legacy path: byte-identical to pre-v0.20.0 behavior.
        if ts_epoch <= 0.0:
            return 0.5
        age = max(0.0, now_epoch - ts_epoch)
        if age >= _RECENCY_WINDOW_S:
            return 0.5
        return 1.0 - 0.5 * (age / _RECENCY_WINDOW_S)

    floor = float(curve.floor)
    if ts_epoch <= 0.0:
        return floor
    age = max(0.0, now_epoch - ts_epoch)
    window = float(curve.half_life_days) * 2.0 * 86400.0
    if window <= 0.0 or age >= window:
        return floor
    return 1.0 - (1.0 - floor) * (age / window)


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


def _append_quarantine_audit(
    autodev_dir: Path, audit: QuarantineAuditEntry
) -> None:
    """v0.35.0 C1: append one line to ``quarantine_audit.jsonl``.

    The audit file is per-project (lives next to ``knowledge.jsonl`` in
    ``<cwd>/.autodev/``) and append-only; readers reconstruct the full
    history by streaming the file. Caller must hold the same lock as
    the count-mutation site so the audit line and the entry's flag
    flip land atomically.
    """
    autodev_dir.mkdir(parents=True, exist_ok=True)
    audit_path = autodev_dir / "quarantine_audit.jsonl"
    line = json.dumps(dataclasses.asdict(audit), sort_keys=True) + "\n"
    with audit_path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _evaluate_quarantine(entry: KnowledgeEntry) -> tuple[bool, str | None]:
    """v0.35.0 C1: decide whether ``entry`` should be quarantined.

    Returns ``(True, reason)`` when the entry has been injected enough
    times to have a meaningful success ratio AND that ratio is below the
    floor. Returns ``(False, None)`` otherwise. The single reason
    ``"applied_threshold_no_success"`` covers both literal zero and the
    sub-10% case — it's the same operational signal.
    """
    if entry.applied_count <= _QUARANTINE_APPLIED_MIN:
        return (False, None)
    ratio = entry.succeeded_after_count / entry.applied_count
    if ratio < _QUARANTINE_SUCCESS_RATIO_MAX:
        return (True, "applied_threshold_no_success")
    return (False, None)


def _critic_evidence_quality(text: str) -> Literal["ok", "thin", "noise"]:
    """v0.35.0 C2: classify a critic's evidence body.

    Critics that emit a literal "evidence is critically thin" preamble
    are essentially abstaining — their output should be logged but
    must not bump confirmations on a knowledge entry. Infrastructure-
    noise markers (adapter failures bleeding into the critic text)
    are likewise rejected when the body is short enough to be
    plausibly just the failure string.

    Deviation from the literal plan text: the original
    ``_CRITIC_MIN_EVIDENCE_CHARS = 80`` floor was demoted from a hard
    "thin" verdict to a "noise" qualifier. Hard < 80 rejection broke
    structurally-short tournament forensic events
    (e.g. ``spec_hash=... branch=... error=...`` evidence emitted by
    the multi-branch tournament's failed-branch summarizer) which
    don't fit the abstention pattern the gate exists to catch. The
    explicit thin/noise marker tuples — which are what the plan calls
    out as load-bearing — remain in force.
    """
    stripped = (text or "").strip()
    if not stripped:
        return "thin"
    lower = stripped.lower()
    for marker in _CRITIC_THIN_EVIDENCE_MARKERS:
        if lower.startswith(marker.lower()) or marker.lower() in lower.split("\n", 1)[0]:
            return "thin"
    # Short body PLUS a noise marker → noise (likely an echoed adapter
    # failure). Short body alone is not enough — many legitimate
    # forensic events are sub-80 chars by design.
    if len(stripped) < 200:
        for marker in _CRITIC_NOISE_MARKERS:
            if marker.lower() in lower:
                return "noise"
    return "ok"


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
        *,
        session_id: str | None = None,
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
        # v0.35.0: optional session_id wired by the Orchestrator so the
        # quarantine / critic-gate / promotion-rejection paths can emit
        # plan-ledger ops. Read-only CLI surfaces (status etc.) pass no
        # session_id; in that mode emission is silently skipped.
        self._session_id = session_id

    async def _emit_ledger(self, op: str, payload: dict[str, Any]) -> None:
        """v0.35.0: best-effort ledger emission for knowledge-tier events.

        Skips when no session_id is set (read-only CLI usage). Errors
        are swallowed at WARNING level — a ledger write failure must
        never block a knowledge-store operation.
        """
        if not self._session_id:
            return
        try:
            from state.ledger import append_entry as _append_entry

            await _append_entry(
                self._cwd,
                op=op,  # type: ignore[arg-type]
                payload=payload,
                session_id=self._session_id,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("knowledge.ledger_emit_failed", op=op, err=str(exc))

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
        # v0.35.0 C2: critic-evidence quality gate. The classifier reads
        # the evidence body (which is the load-bearing part of the
        # rendered lesson) and rejects critic outputs that are
        # essentially abstentions before they bump confirmations. Only
        # gate critic-sourced events whose evidence is what's being
        # screened — every TournamentEvent passes through here today but
        # the marker tuple is conservative enough to leave winners /
        # course-corrections untouched.
        quality = _critic_evidence_quality(event.evidence or "")
        if quality != "ok":
            await self._emit_ledger(
                "critic_evidence_rejected",
                {
                    "role": "critic_t",
                    "reason": quality,
                    "family": event.family,
                    "event_type": event.event_type,
                },
            )
            self._log.info(
                "knowledge.record.critic_evidence_rejected",
                reason=quality,
                family=event.family,
            )
            return None
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

        v0.35.0 C1: quarantined entries are silently skipped before
        ranking, so they neither appear in the block nor have their
        applied_count incremented. v0.35.0 also writes
        ``last_applied_at`` alongside each applied_count increment so
        the 7-day decay curve (``confirmations_7d``) has a per-entry
        anchor.
        """
        block, _ids = await self.inject_block_with_ids(
            role, limit=limit, task_id=task_id, lane=lane
        )
        return block

    async def inject_block_with_ids(
        self,
        role: str,
        limit: int | None = None,
        *,
        task_id: str | None = None,
        lane: str | None = None,
    ) -> tuple[str, list[str]]:
        """v0.35.0 C1 prerequisite: ``inject_block`` plus the IDs that landed.

        Used by the Orchestrator to populate the per-task correlation
        map so a successful task completion can credit
        ``succeeded_after_count`` against the exact entries that
        contributed to that task's prompt. Public — orchestrator-facing
        only; existing test fakes that stub ``inject_block`` remain
        valid because the legacy ``str``-returning method delegates here.
        """
        if not self.enabled:
            return ("", [])
        if role in self._denylist():
            self._log.debug("knowledge.inject.skip_denylist", role=role)
            return ("", [])

        kcfg = self.knowledge_config
        cap = limit if limit is not None else kcfg.max_inject_count
        if cap <= 0:
            return ("", [])

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
        # v0.35.0 C1: quarantined entries do not participate in injection.
        swarm = [e for e in swarm if not e.quarantined]
        hive = [e for e in hive if not e.quarantined]
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
            return ("", [])

        selected = merged[:cap]

        # v0.35.0 C1: record post-increment quarantine flips so we can
        # emit ledger ops outside the write lock.
        quarantine_flips: list[tuple[str, int, int, str]] = []

        # Increment applied_count for each selected swarm entry (read-modify-write).
        # v0.35.0 C1: same lock also writes ``last_applied_at`` and may
        # flip ``quarantined`` to True, plus appends a JSONL audit line.
        swarm_ids = {e.id for e in selected if e.tier == "swarm"}
        if swarm_ids:
            try:
                swarm_file = knowledge_path(self._cwd)
                autodev_dir = swarm_file.parent
                now_iso = _now_iso()
                async with plan_lock(self._cwd):
                    swarm_raw = await asyncio.to_thread(_read_jsonl, swarm_file)
                    updated = False
                    for d in swarm_raw:
                        if d.get("id") not in swarm_ids:
                            continue
                        d["applied_count"] = int(d.get("applied_count", 0)) + 1
                        d["last_applied_at"] = now_iso
                        # v0.35.0 C1: atomic evaluate-and-flip inside the
                        # same lock that mutated the count.
                        post = KnowledgeEntry.model_validate(d)
                        should_q, reason = _evaluate_quarantine(post)
                        if should_q and not post.quarantined:
                            d["quarantined"] = True
                            ratio = (
                                post.succeeded_after_count / post.applied_count
                                if post.applied_count
                                else 0.0
                            )
                            _append_quarantine_audit(
                                autodev_dir,
                                QuarantineAuditEntry(
                                    entry_id=post.id,
                                    applied_count=post.applied_count,
                                    succeeded_after_count=post.succeeded_after_count,
                                    ratio=ratio,
                                    reason=reason or "applied_threshold_no_success",
                                    decided_at=now_iso,
                                ),
                            )
                            quarantine_flips.append(
                                (
                                    post.id,
                                    post.applied_count,
                                    post.succeeded_after_count,
                                    reason or "applied_threshold_no_success",
                                )
                            )
                        updated = True
                    if updated:
                        await asyncio.to_thread(_write_jsonl, swarm_file, swarm_raw)
            except Exception:  # noqa: BLE001
                self._log.warning("knowledge.inject.applied_count_update_failed")

        for entry_id, applied, succeeded, reason in quarantine_flips:
            await self._emit_ledger(
                "knowledge_entry_quarantined",
                {
                    "entry_id": entry_id,
                    "applied_count": applied,
                    "succeeded_after_count": succeeded,
                    "reason": reason,
                },
            )

        lines = ["Lessons learned from prior work:"]
        for e in selected:
            lines.append(f"- [conf:{e.confidence:.2f}] {_one_line(e.text)}")
        return ("\n".join(lines), [e.id for e in selected])

    async def credit_task_success(
        self,
        entry_ids: list[str],
        *,
        task_id: str,
        role: str,
    ) -> int:
        """v0.35.0 C1 prerequisite: bump ``succeeded_after_count`` per entry.

        Called by the Orchestrator when a task transitions to
        ``complete``. Each entry id in ``entry_ids`` is matched against
        both tiers; the increment lands under the same lock that
        protects the count's mutation site (swarm: ``plan_lock``;
        hive: ``_hive_lock``). One ledger op
        ``knowledge_lesson_credited`` is emitted per successful
        increment. Returns the number of entries that were actually
        incremented.

        Idempotency: the caller is expected to drain the correlation
        entry from its own map after invoking this method — re-running
        with the same list will double-credit. The store does not
        track which (task, entry) pairs have been credited.
        """
        if not entry_ids:
            return 0
        unique_ids = set(entry_ids)
        credited: list[tuple[str, Tier]] = []

        # Swarm tier under plan_lock.
        try:
            swarm_file = knowledge_path(self._cwd)
            async with plan_lock(self._cwd):
                swarm_raw = await asyncio.to_thread(_read_jsonl, swarm_file)
                updated = False
                for d in swarm_raw:
                    if d.get("id") in unique_ids:
                        d["succeeded_after_count"] = (
                            int(d.get("succeeded_after_count", 0)) + 1
                        )
                        credited.append((str(d.get("id", "")), "swarm"))
                        updated = True
                if updated:
                    await asyncio.to_thread(_write_jsonl, swarm_file, swarm_raw)
        except Exception:  # noqa: BLE001
            self._log.warning("knowledge.credit.swarm_update_failed", task_id=task_id)

        # Hive tier under hive lock.
        if self.hive_enabled:
            try:
                hive_file = self._hive_path
                async with _hive_lock(hive_file):
                    hive_raw = await asyncio.to_thread(_read_jsonl, hive_file)
                    updated = False
                    for d in hive_raw:
                        if d.get("id") in unique_ids:
                            d["succeeded_after_count"] = (
                                int(d.get("succeeded_after_count", 0)) + 1
                            )
                            credited.append((str(d.get("id", "")), "hive"))
                            updated = True
                    if updated:
                        await asyncio.to_thread(_write_jsonl, hive_file, hive_raw)
            except Exception:  # noqa: BLE001
                self._log.warning("knowledge.credit.hive_update_failed", task_id=task_id)

        for entry_id, tier in credited:
            await self._emit_ledger(
                "knowledge_lesson_credited",
                {
                    "entry_id": entry_id,
                    "task_id": task_id,
                    "role": role,
                    "tier": tier,
                },
            )
        return len(credited)

    # --- Internals ---------------------------------------------------

    def _rank(self, entry: KnowledgeEntry) -> float:
        return self._rank_with_ts(entry, time.time())

    def _rank_with_ts(self, entry: KnowledgeEntry, now_epoch: float) -> float:
        """``confidence * recency_factor * (1 + log(applied_count + 1))``.

        v0.20.0 B1: when :attr:`KnowledgeConfig.decay_curves` is set,
        looks up an entry-type-specific curve via
        ``entry.metadata["event_type"]`` and forwards it to
        :func:`_recency_factor`. When the map is None (default), the
        entry has no ``event_type``, or the type has no matching curve,
        the legacy 30-day linear decay applies (byte-identical to the
        pre-v0.20.0 path).

        v0.35.0 C3: when an entry carries ``last_applied_at`` and no
        explicit per-event-type curve is configured, the
        ``confirmations_7d`` curve is applied against
        ``last_applied_at`` instead of ``timestamp``. The curve halves
        the contribution every 7 days, so a stale entry that nobody has
        injected lately ranks below a fresh peer of equal raw
        confirmations. Quarantine evaluation (C1) still consumes raw
        counts; only the injection ranker sees the decayed weight.
        """
        curve = self._decay_curve_for(entry)
        recency_anchor = entry.timestamp
        if curve is None and entry.last_applied_at is not None:
            curve = DecayCurveConfig(half_life_days=7.0, floor=0.5)
            recency_anchor = entry.last_applied_at.isoformat()
        recency = _recency_factor(recency_anchor, now_epoch, curve=curve)
        applied_boost = 1.0 + math.log(max(0, entry.applied_count) + 1)
        return float(entry.confidence) * recency * applied_boost

    def _decay_curve_for(
        self, entry: KnowledgeEntry
    ) -> "DecayCurveConfig | None":
        """Resolve the per-event-type decay curve, if any.

        Returns ``None`` (legacy path) when:

        * :attr:`KnowledgeConfig.decay_curves` is not configured;
        * ``entry.metadata`` lacks an ``event_type`` key;
        * the configured map has no entry for that ``event_type``.

        v0.35.0 C3: the special key ``confirmations_7d`` is recognized
        even when no explicit map entry matches the event type — it is
        returned as ``DecayCurveConfig(half_life_days=7, floor=0.5)``
        so callers wiring a ``decay_curves={"confirmations_7d": ...}``
        opt into the same shape used implicitly by entries with
        ``last_applied_at`` set.
        """
        kcfg = self.knowledge_config
        curves = getattr(kcfg, "decay_curves", None)
        if curves and _CONFIRMATIONS_DECAY_CURVE_KEY in curves:
            return curves[_CONFIRMATIONS_DECAY_CURVE_KEY]
        if not curves:
            return None
        if not entry.metadata:
            return None
        event_type = entry.metadata.get("event_type")
        if not isinstance(event_type, str):
            return None
        return curves.get(event_type)

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
            * ``entry.succeeded_after_count > 0`` (v0.35.0 C3 — promotion
              now requires evidence the entry has actually preceded a
              successful task)
            * no near-duplicate already in the hive (idempotency)

        Returns True if the entry was newly promoted.
        """
        if not self.hive_enabled:
            return False
        kcfg = self.knowledge_config
        if entry.confirmations < kcfg.promotion_min_confirmations:
            await self._emit_ledger(
                "knowledge_entry_promotion_rejected",
                {"entry_id": entry.id, "reason": "min_confirmations"},
            )
            return False
        if entry.confidence < kcfg.promotion_min_confidence:
            return False
        # v0.35.0 C3: zero-success entries previously cleared the
        # confirmations bar at the old default (3) and polluted the
        # hive. The new conjunct gates promotion on at least one
        # observed success increment from C1's writer.
        if entry.succeeded_after_count <= 0:
            await self._emit_ledger(
                "knowledge_entry_promotion_rejected",
                {"entry_id": entry.id, "reason": "no_success"},
            )
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


def compute_task_signature(task: Any) -> str:
    """Stable signature for cross-reset task similarity (v0.32.0 Phase 4.3).

    Returns the hex digest of a sha256 over a canonical string built from:

    * the sorted set of files the task targets (``task.files`` or
      ``task.target_files``);
    * the error class name (when an evidence object with
      ``error_class`` / ``failure_class`` is attached);
    * the first 512 chars of the most recent diff (``task.last_diff``).

    The function is defensive: missing / empty fields are tolerated
    (rendered as the empty string) so a partially-populated task still
    produces a stable hash. Two tasks with the same file set, the same
    error class, and the same opening diff slice will collide — which
    is exactly the similarity signal :func:`orchestrator.knowledge_lookup.lookup_recent_failures`
    wants when filtering past failures.
    """
    files = _get_attr(task, ("files", "target_files"), default=())
    if isinstance(files, (list, tuple, set, frozenset)):
        sorted_files = sorted(str(p) for p in files)
    elif files:
        sorted_files = [str(files)]
    else:
        sorted_files = []

    error_class = _get_attr(
        task,
        ("error_class", "failure_class", "last_error_class"),
        default="",
    )
    if not isinstance(error_class, str):
        error_class = error_class.__class__.__name__ if error_class else ""

    last_diff = _get_attr(task, ("last_diff", "diff", "evidence_diff"), default="")
    if not isinstance(last_diff, str):
        last_diff = ""
    diff_head = last_diff[:512]

    canonical = "\n".join(
        [
            "files=" + ",".join(sorted_files),
            "error_class=" + str(error_class),
            "diff_head=" + diff_head,
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _get_attr(obj: Any, keys: tuple[str, ...], default: Any) -> Any:
    """Look up the first present attribute / key on ``obj``.

    Supports both attribute-style access (dataclass / pydantic) and
    dict-style access. Returns ``default`` when none of ``keys`` resolve.
    """
    for key in keys:
        if isinstance(obj, dict):
            if key in obj and obj[key] is not None:
                return obj[key]
        else:
            value = getattr(obj, key, None)
            if value is not None:
                return value
    return default


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
    "compute_task_signature",
    "jaccard_bigrams",
]
