"""Idempotent seed-pack loader for hive-tier knowledge entries.

Seeded entries land directly in the hive tier alongside organic lessons
that were promoted there via :meth:`KnowledgeStore._promote_if_qualified`.
Re-runs are safe via two mechanisms:

1. A marker file at ``<cwd>/.autodev/seed_packs.json`` records which packs
   have been seeded for the project; a pack listed there is short-circuited.
2. The same bigram-Jaccard dedup
   (:attr:`config.schema.KnowledgeConfig.dedup_threshold`, default 0.6)
   that gates promotion is re-applied here, so even if the marker is lost
   a re-load cannot produce duplicates.

Why we write the hive file directly instead of going through
:meth:`KnowledgeStore.record`: ``record()`` only writes to the per-project
swarm tier (an entry only reaches the hive after enough confirmations
trigger ``_promote_if_qualified``). Seed packs are by definition
"already-vetted hive lessons", so we splice them into the hive file
under the same lock + dedup contract that promotion uses.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autologging import get_logger
from state.knowledge import (
    KnowledgeEntry,
    _hive_lock,
    _read_jsonl,
    _write_jsonl,
    jaccard_bigrams,
)

if TYPE_CHECKING:
    from state.knowledge import KnowledgeStore


logger = get_logger(__name__)


async def seed_pack_if_missing(
    store: "KnowledgeStore",
    pack_path: Path,
    pack_name: str,
    *,
    marker_dir: Path,
) -> int:
    """Insert pack entries into the hive tier if not already seeded.

    Returns the number of entries newly inserted (0 if the pack was already
    seeded, the file is missing, or every entry was deduped against an
    existing hive entry).

    Idempotency
    -----------
    * Marker file: ``marker_dir / "seed_packs.json"`` records seeded packs
      keyed by ``pack_name``. If ``pack_name`` is present, returns 0 immediately.
    * Jaccard dedup: each candidate entry is compared against existing hive
      entries at the configured ``KnowledgeConfig.dedup_threshold``; duplicates
      are skipped silently.

    Behavior matrix
    ---------------
    * ``store.hive_enabled is False`` -> short-circuit to 0 (writing the hive
      file would be a behavior change the operator opted out of).
    * ``pack_path`` missing -> short-circuit to 0 (no error).
    * Marker exists but unreadable -> treated as "no marker", proceeds with
      Jaccard dedup as the safety net.
    """
    if not store.hive_enabled:
        logger.debug("seed_packs.skip_hive_disabled", pack=pack_name)
        return 0
    if not pack_path.exists():
        logger.debug("seed_packs.skip_missing_file", pack=pack_name, path=str(pack_path))
        return 0

    marker_path = marker_dir / "seed_packs.json"
    seeded: dict[str, str] = {}
    if marker_path.exists():
        try:
            raw = await asyncio.to_thread(marker_path.read_text)
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                seeded = {str(k): str(v) for k, v in parsed.items()}
        except (json.JSONDecodeError, OSError):
            logger.warning("seed_packs.marker_unreadable", path=str(marker_path))
            seeded = {}
    if pack_name in seeded:
        logger.debug("seed_packs.skip_already_seeded", pack=pack_name)
        return 0

    raw_text = await asyncio.to_thread(pack_path.read_text)
    candidates: list[dict[str, Any]] = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("seed_packs.bad_jsonl_line", pack=pack_name)
            continue
        if not isinstance(candidate, dict):
            continue
        text = candidate.get("text", "")
        if not isinstance(text, str) or not text.strip():
            continue
        candidates.append(candidate)

    if not candidates:
        # Still mark the pack as seeded so we don't keep re-reading an
        # empty / malformed file on every orchestrator entry.
        await _write_marker(marker_dir, marker_path, seeded, pack_name)
        return 0

    kcfg = store.knowledge_config
    threshold = float(kcfg.dedup_threshold)
    hive_file = store._hive_path  # noqa: SLF001 — intentional: we mirror
                                  # the promote_if_qualified write path.

    inserted = 0
    async with _hive_lock(hive_file):
        hive_raw = await asyncio.to_thread(_read_jsonl, hive_file)
        existing_texts: list[str] = [
            str(d.get("text", "")) for d in hive_raw if d.get("text")
        ]

        # Build the new entries (with hive-side Jaccard dedup against
        # already-seeded text and the existing hive contents).
        new_dicts: list[dict[str, Any]] = []
        for candidate in candidates:
            text = str(candidate["text"]).strip()
            if any(jaccard_bigrams(text, e) >= threshold for e in existing_texts):
                continue
            entry = KnowledgeEntry(
                id=str(candidate.get("id") or _fallback_id(pack_name, text)),
                timestamp=str(candidate.get("timestamp") or _now_iso()),
                role_source=str(
                    candidate.get("role_source") or f"seed_pack:{pack_name}"
                ),
                tier="hive",
                text=text,
                confidence=float(candidate.get("confidence", 0.85)),
                applied_count=int(candidate.get("applied_count", 0)),
                succeeded_after_count=int(
                    candidate.get("succeeded_after_count", 0)
                ),
                confirmations=int(candidate.get("confirmations", 0)),
                metadata=dict(candidate.get("metadata", {})),
            )
            new_dicts.append(entry.model_dump(mode="json"))
            existing_texts.append(text)
            inserted += 1

        if new_dicts:
            hive_raw.extend(new_dicts)
            # Enforce hive cap by lowest-ranked eviction (mirror promote path).
            entries_for_cap = [KnowledgeEntry.model_validate(d) for d in hive_raw]
            if len(entries_for_cap) > kcfg.hive_max_entries:
                entries_for_cap = store._evict_to_cap(  # noqa: SLF001
                    entries_for_cap, kcfg.hive_max_entries
                )
            await asyncio.to_thread(
                _write_jsonl,
                hive_file,
                [e.model_dump(mode="json") for e in entries_for_cap],
            )

    await _write_marker(marker_dir, marker_path, seeded, pack_name)
    logger.info(
        "seed_packs.loaded",
        pack=pack_name,
        candidates=len(candidates),
        inserted=inserted,
    )
    return inserted


async def _write_marker(
    marker_dir: Path,
    marker_path: Path,
    seeded: dict[str, str],
    pack_name: str,
) -> None:
    """Persist the updated marker dict atomically (best-effort)."""
    seeded[pack_name] = _now_iso()
    try:
        await asyncio.to_thread(marker_dir.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(
            marker_path.write_text, json.dumps(seeded, indent=2, sort_keys=True)
        )
    except OSError as exc:
        # Marker write failures are non-fatal — Jaccard dedup will still
        # prevent duplicates on the next pass; we only lose the cheap
        # short-circuit.
        logger.warning(
            "seed_packs.marker_write_failed", path=str(marker_path), error=str(exc)
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fallback_id(pack_name: str, text: str) -> str:
    """Fallback id generator — only fires when a pack omits ``id``."""
    import hashlib

    h = hashlib.sha256(f"{pack_name}|{text}".encode("utf-8")).hexdigest()[:10]
    return f"seed-{pack_name[:12]}-{h}"


__all__ = ["seed_pack_if_missing"]
