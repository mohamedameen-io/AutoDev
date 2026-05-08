"""LLM-backed semantic equivalence judge for surviving mutants (v0.19.0 — Stage 2).

When :mod:`qa.equivalence_filter` (Stage 1, static AST) cannot rule a
survivor equivalent, an LLM judge inspects the original/mutant pair and
returns a YES/NO verdict with a confidence score. Used to inflate the
mutation-test kill rate when the survivor is *semantically* equivalent
(``a + 0 == a``, ``not not x == bool(x)``, …) — gaps that look like
test sufficiency holes but aren't.

Cost containment:

  * Cache via ``.autodev/mutation_cache.jsonl`` keyed by
    ``sha256(original + mutant)``. A cache hit is free.
  * Use the cheapest available model (Haiku-class) — accept 5-10%
    accuracy loss for 30× cost savings.
  * Confidence threshold (default 0.7): only adjust kill-rate when the
    judge is confident.

Failure modes the judge tolerates:

  * Anthropic SDK not installed → :class:`LLMEquivalenceJudge` constructs
    OK and returns ``(False, 0.0)`` for every call. Caller treats as
    "not equivalent" — same as Stage 1 fallback.
  * No API key → same fallback behavior.
  * API error → cached as a non-equivalence (caller may invalidate
    later). Tolerates network blips without breaking the gate.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_CACHE_REL = Path(".autodev") / "mutation_cache.jsonl"
_DEFAULT_MODEL = "claude-haiku-4-5"
_DEFAULT_CONFIDENCE_THRESHOLD = 0.7


# Probe at import — keeps the rest of the gate honest about what's
# available. Never raises.
try:  # pragma: no cover - SDK presence varies by environment
    import anthropic  # type: ignore[import-not-found,import-untyped]

    _ANTHROPIC_AVAILABLE = True
except Exception:  # noqa: BLE001
    _ANTHROPIC_AVAILABLE = False


def _cache_key(original_code: str, mutant_code: str) -> str:
    payload = (original_code + "\x1f" + mutant_code).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_cache(cwd: Path) -> dict[str, dict[str, Any]]:
    """Read the cache JSONL into a key→record dict.

    Each line is ``{"key": str, "verdict": bool, "confidence": float}``.
    Malformed lines are silently dropped.
    """
    path = cwd / _CACHE_REL
    if not path.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    try:
        with path.open(encoding="utf-8") as fp:
            for raw in fp:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                key = rec.get("key")
                if isinstance(key, str):
                    out[key] = rec
    except OSError:
        return {}
    return out


def _append_cache(cwd: Path, key: str, verdict: bool, confidence: float) -> None:
    path = cwd / _CACHE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"key": key, "verdict": verdict, "confidence": confidence}
    try:
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.warning("llm_equivalence_cache_write_failed: %s", exc)


_PROMPT_TEMPLATE = """You are evaluating whether two snippets of code are SEMANTICALLY equivalent.

Original:
```
{original}
```

Mutant (a single small change relative to the original):
```
{mutant}
```

Decide: are these snippets guaranteed to produce identical observable behavior \
for ALL inputs? A trivial no-op mutation (e.g. ``a + 0`` swapped for ``a``) \
counts as equivalent. Any change that COULD differ on some input is \
NOT equivalent.

Respond with EXACTLY ONE word on the first line: YES or NO.
On the second line, give a confidence score between 0.0 and 1.0.
Nothing else."""


class LLMEquivalenceJudge:
    """LLM-backed semantic equivalence judge.

    Args:
        cwd: Repository root, used to locate the on-disk cache.
        model: Anthropic model id. Default Haiku-class for cost.
        confidence_threshold: Minimum confidence required before the
            verdict counts. Below the threshold, the caller treats the
            pair as not equivalent.
    """

    def __init__(
        self,
        cwd: Path,
        model: str = _DEFAULT_MODEL,
        confidence_threshold: float = _DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> None:
        self.cwd = cwd
        self.model = model
        self.confidence_threshold = confidence_threshold
        self._client: Any = None
        if _ANTHROPIC_AVAILABLE and os.environ.get("ANTHROPIC_API_KEY"):
            try:  # pragma: no cover - SDK init network-free, error-tolerant
                self._client = anthropic.AsyncAnthropic()
            except Exception:  # noqa: BLE001
                self._client = None

    async def is_equivalent(
        self,
        original_code: str,
        mutant_code: str,
        test_paths: list[Path] | None = None,
    ) -> tuple[bool, float]:
        """Return ``(verdict, confidence)`` for *original_code* vs *mutant_code*.

        Cached results (keyed by sha256 of the pair) bypass the API.
        Failure modes return ``(False, 0.0)``.

        *test_paths* is currently unused — reserved for B3 follow-up
        where the prompt may incorporate test code excerpts.
        """
        if original_code == mutant_code:
            return True, 1.0

        key = _cache_key(original_code, mutant_code)
        cache = _load_cache(self.cwd)
        if key in cache:
            rec = cache[key]
            return bool(rec.get("verdict", False)), float(rec.get("confidence", 0.0))

        if self._client is None:
            return False, 0.0

        prompt = _PROMPT_TEMPLATE.format(original=original_code, mutant=mutant_code)
        try:  # pragma: no cover - exercised only when SDK present
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=32,
                messages=[{"role": "user", "content": prompt}],
            )
            text = ""
            for block in getattr(resp, "content", []) or []:
                if hasattr(block, "text") and isinstance(block.text, str):
                    text += block.text
                elif isinstance(block, dict):
                    text += block.get("text", "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_equivalence_api_error: %s", exc)
            return False, 0.0

        verdict, confidence = _parse_response(text)
        _append_cache(self.cwd, key, verdict, confidence)
        return verdict, confidence


def _parse_response(text: str) -> tuple[bool, float]:
    """Parse ``YES/NO\\n0.92`` style responses."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return False, 0.0
    head = lines[0].upper()
    verdict = head.startswith("YES")
    confidence = 0.0
    if len(lines) > 1:
        try:
            confidence = max(0.0, min(1.0, float(lines[1])))
        except (TypeError, ValueError):
            confidence = 0.0
    return verdict, confidence


__all__ = [
    "LLMEquivalenceJudge",
]
