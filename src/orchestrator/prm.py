"""v0.15.0 PRM (Process Reward Model) — trajectory pattern detection.

Records :class:`TrajectoryEvent` for every delegate dispatch and runs
five rule-based detectors against the trailing event window. When a
detector fires, the :class:`TrajectoryStore.analyze` helper returns the
matched :class:`Pattern` objects; the executor builds a
:class:`CourseCorrection` from the highest-severity match and splices it
into the next agent's prompt.

The patterns + taxonomy mapping captures the failure modes documented
in the v0.15.0 plan:

==================  ====================  ================================
Pattern             Taxonomy              What it catches
==================  ====================  ================================
repetition_loop     reasoning_error       Same (role, action, target_files)
                                          ≥3× in a row.
ping_pong           reasoning_error       Alternating between two distinct
                                          targets ≥4×.
expansion_drift     specification_error   target_files set growing without
                                          a successful event.
stuck_on_test       coordination_error    test_engineer role with ≥3
                                          consecutive failures.
context_thrash      coordination_error    Rapid switching between unrelated
                                          targets.
==================  ====================  ================================

Capacity: each task's event log is capped at
:data:`_MAX_EVENTS_PER_TASK` (50). Eviction is FIFO — oldest events
discarded first so the trailing window always reflects the freshest
signal. The store is in-memory only; a crash mid-run loses the
trajectory data, mirroring the rest of v0.15.0's stuck-state design.

Usage:

    store = TrajectoryStore()
    store.record(task_id, TrajectoryEvent(...))
    patterns = store.analyze(task_id)
    if patterns:
        cc = CourseCorrection.from_pattern(patterns[0])
        prompt += "\\n\\n" + cc.format_for_prompt()
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Literal


# Severity ordering for course-correction priority. Patterns with higher
# severity outrank others when multiple fire simultaneously.
_PATTERN_SEVERITY: dict[str, int] = {
    "stuck_on_test": 5,
    "expansion_drift": 4,
    "context_thrash": 3,
    "ping_pong": 2,
    "repetition_loop": 1,
}


# Pattern name → taxonomy mapping. See module docstring.
_PATTERN_TAXONOMY: dict[str, str] = {
    "repetition_loop": "reasoning_error",
    "ping_pong": "reasoning_error",
    "expansion_drift": "specification_error",
    "stuck_on_test": "coordination_error",
    "context_thrash": "coordination_error",
}


# Default suggestion text per pattern. Used by :meth:`CourseCorrection.from_pattern`.
_PATTERN_DEFAULT_SUGGESTION: dict[str, str] = {
    "repetition_loop": (
        "You have made the same edit on the same files three or more times "
        "in a row without success. Vary your approach: examine the failing "
        "test output more carefully, try a different file or function, or "
        "consider whether the underlying assumption is wrong."
    ),
    "ping_pong": (
        "You have alternated between two targets four or more times. Pick "
        "one and commit to a single coherent change before switching."
    ),
    "expansion_drift": (
        "Your target file set is growing without producing a successful "
        "result. Narrow your scope: identify the smallest single change "
        "that would unblock the next test, and stop adding new files."
    ),
    "stuck_on_test": (
        "test_engineer has failed three or more times in a row. Examine the "
        "actual failing test output (not just the summary), check whether "
        "the test assumptions match the implementation contract, and "
        "consider whether the test itself needs adjustment."
    ),
    "context_thrash": (
        "You have rapidly switched between unrelated file targets. This "
        "wastes context and produces shallow changes. Pick one coherent "
        "area and finish your work there before moving on."
    ),
}


PatternName = Literal[
    "repetition_loop",
    "ping_pong",
    "expansion_drift",
    "stuck_on_test",
    "context_thrash",
]


# Maximum events stored per task. Old events evict FIFO once the cap is
# exceeded. Keeps memory bounded on long-running tasks.
_MAX_EVENTS_PER_TASK: int = 50


@dataclass(frozen=True)
class TrajectoryEvent:
    """One delegate-dispatch event in a task's trajectory.

    Frozen dataclass so events can be safely held in lists / used as
    dict keys. Equality is structural — two events with the same
    fields are considered identical (this is what
    :func:`detect_repetition_loop` exploits).

    Attributes:
        timestamp: ``time.time()``-style epoch seconds. Used by
            :func:`detect_context_thrash` to bound "rapid" switching.
        role: Agent role name (``"developer"``, ``"reviewer"``,
            ``"test_engineer"``, etc.).
        action: Coarse action label (``"edit"``, ``"test"``,
            ``"review"``, etc.).
        target_files: Tuple of repo-relative file paths the dispatch
            is operating on. Tuple (immutable) so events stay hashable.
        success: Whether the dispatch succeeded.
        duration_s: Wall-clock duration of the dispatch.
    """

    timestamp: float
    role: str
    action: str
    target_files: tuple[str, ...]
    success: bool
    duration_s: float


@dataclass
class Pattern:
    """A detected trajectory pattern.

    The :attr:`taxonomy` is derived from :attr:`name` via the
    :data:`_PATTERN_TAXONOMY` table — kept as a property so callers
    can build a Pattern from a name and the mapping resolves
    deterministically.
    """

    name: str

    @property
    def taxonomy(self) -> str:
        return _PATTERN_TAXONOMY.get(self.name, "reasoning_error")

    @property
    def severity(self) -> int:
        return _PATTERN_SEVERITY.get(self.name, 0)


@dataclass
class CourseCorrection:
    """A course-correction message ready to splice into an agent prompt.

    Built from a :class:`Pattern` via :meth:`from_pattern` (which fills
    in a sensible default suggestion) or constructed directly with
    bespoke suggestion text. :meth:`format_for_prompt` returns the
    markdown block the executor injects into the next dispatch.
    """

    taxonomy: str
    pattern: str
    suggestion: str

    @classmethod
    def from_pattern(cls, pattern: Pattern) -> "CourseCorrection":
        """Build a :class:`CourseCorrection` with a default suggestion."""
        return cls(
            taxonomy=pattern.taxonomy,
            pattern=pattern.name,
            suggestion=_PATTERN_DEFAULT_SUGGESTION.get(
                pattern.name,
                "Re-examine the trajectory and adjust strategy.",
            ),
        )

    def format_for_prompt(self) -> str:
        """Render the correction as a markdown block.

        The shape is intentionally compact + machine-greppable so the
        injection footprint is minimal but the agent can still parse
        the taxonomy / pattern label out of the text.
        """
        return (
            "## COURSE CORRECTION\n\n"
            f"{self.taxonomy}: {self.pattern}\n\n"
            f"Suggested adjustment: {self.suggestion}\n"
        )

    def fingerprint(self) -> str:
        """Stable identity string for "have we already emitted this correction?".

        Used by the executor to avoid re-emitting the same correction
        for the same task on every subsequent dispatch.
        """
        return f"{self.taxonomy}:{self.pattern}"


class TrajectoryStore:
    """Per-task in-memory trajectory log + pattern analysis.

    Append-only with FIFO eviction when the per-task cap is exceeded.
    Not thread-safe — consumers should serialize via the
    PlanManager's lock if multiple workers might call concurrently.
    For the v0.15.0 single-task analysis path, the only caller is
    :func:`orchestrator.execute_phase.delegate`, which is already
    serialized per task (a worker handles its task end-to-end).
    """

    def __init__(self) -> None:
        self._events: dict[str, Deque[TrajectoryEvent]] = {}
        # Tracks whether a CourseCorrection has been emitted for a task
        # yet — used by the executor's "cap one correction per task"
        # contract (see plan section "PRM trajectory pattern detection").
        self._emitted_fingerprints: dict[str, set[str]] = {}

    def record(self, task_id: str, event: TrajectoryEvent) -> None:
        bucket = self._events.get(task_id)
        if bucket is None:
            bucket = deque(maxlen=_MAX_EVENTS_PER_TASK)
            self._events[task_id] = bucket
        bucket.append(event)

    def events_for(self, task_id: str) -> list[TrajectoryEvent]:
        """Return a snapshot copy of the events for ``task_id`` (oldest → newest)."""
        bucket = self._events.get(task_id)
        if bucket is None:
            return []
        return list(bucket)

    def analyze(self, task_id: str) -> list[Pattern]:
        """Run all detectors against the task's trailing window.

        Returns a list of matched :class:`Pattern` objects sorted by
        severity (highest first). Empty list when no patterns fire.
        """
        events = self.events_for(task_id)
        if not events:
            return []
        results: list[Pattern] = []
        for detector in (
            detect_repetition_loop,
            detect_ping_pong,
            detect_expansion_drift,
            detect_stuck_on_test,
            detect_context_thrash,
        ):
            p = detector(events)
            if p is not None:
                results.append(p)
        results.sort(key=lambda p: p.severity, reverse=True)
        return results

    def has_emitted(self, task_id: str, fingerprint: str) -> bool:
        return fingerprint in self._emitted_fingerprints.get(task_id, set())

    def mark_emitted(self, task_id: str, fingerprint: str) -> None:
        bucket = self._emitted_fingerprints.setdefault(task_id, set())
        bucket.add(fingerprint)


# ---------------------------------------------------------------------------
# Pattern detectors
# ---------------------------------------------------------------------------


_REPETITION_THRESHOLD: int = 3
_PING_PONG_THRESHOLD: int = 4
_EXPANSION_DRIFT_THRESHOLD: int = 3
_STUCK_ON_TEST_THRESHOLD: int = 3
_CONTEXT_THRASH_THRESHOLD: int = 5


def detect_repetition_loop(events: list[TrajectoryEvent]) -> Pattern | None:
    """Same ``(role, action, target_files)`` triple ≥3× in a row at the tail."""
    if len(events) < _REPETITION_THRESHOLD:
        return None
    tail = events[-_REPETITION_THRESHOLD:]
    first = (tail[0].role, tail[0].action, tail[0].target_files)
    for ev in tail[1:]:
        if (ev.role, ev.action, ev.target_files) != first:
            return None
    return Pattern(name="repetition_loop")


def detect_ping_pong(events: list[TrajectoryEvent]) -> Pattern | None:
    """Alternating between exactly two distinct ``target_files`` ≥4× at the tail."""
    if len(events) < _PING_PONG_THRESHOLD:
        return None
    tail = events[-_PING_PONG_THRESHOLD:]
    targets = [ev.target_files for ev in tail]
    unique_targets = set(targets)
    if len(unique_targets) != 2:
        return None
    # Verify alternation: positions 0,2,4,... share a target; 1,3,5,... share the other.
    a = targets[0]
    b = targets[1]
    if a == b:
        return None
    for i, t in enumerate(targets):
        expected = a if i % 2 == 0 else b
        if t != expected:
            return None
    return Pattern(name="ping_pong")


def detect_expansion_drift(events: list[TrajectoryEvent]) -> Pattern | None:
    """target_files set monotonically grows over the trailing window without success."""
    if len(events) < _EXPANSION_DRIFT_THRESHOLD:
        return None
    tail = events[-_EXPANSION_DRIFT_THRESHOLD:]
    if any(ev.success for ev in tail):
        return None
    prev_set: frozenset[str] = frozenset(tail[0].target_files)
    for ev in tail[1:]:
        cur = frozenset(ev.target_files)
        # Strictly grow: previous set must be a strict subset of current.
        if not (prev_set < cur):
            return None
        prev_set = cur
    return Pattern(name="expansion_drift")


def detect_stuck_on_test(events: list[TrajectoryEvent]) -> Pattern | None:
    """test_engineer role with ≥3 consecutive failures at the tail."""
    if len(events) < _STUCK_ON_TEST_THRESHOLD:
        return None
    tail = events[-_STUCK_ON_TEST_THRESHOLD:]
    for ev in tail:
        if ev.role != "test_engineer" or ev.success:
            return None
    return Pattern(name="stuck_on_test")


def detect_context_thrash(events: list[TrajectoryEvent]) -> Pattern | None:
    """Rapid switching between unrelated targets (≥5 distinct, no overlap)."""
    if len(events) < _CONTEXT_THRASH_THRESHOLD:
        return None
    tail = events[-_CONTEXT_THRASH_THRESHOLD:]
    target_sets = [frozenset(ev.target_files) for ev in tail]
    # Every consecutive pair must share NO files (truly unrelated).
    for a, b in zip(target_sets, target_sets[1:]):
        if a & b:
            return None
    # And the union must have at least N distinct sets.
    if len({s for s in target_sets}) < _CONTEXT_THRASH_THRESHOLD:
        return None
    return Pattern(name="context_thrash")


# ---------------------------------------------------------------------------
# v0.20.0 A1: LLM-based PRM classifier (augments rule-based detectors)
# ---------------------------------------------------------------------------

import json as _json
import re as _re
from typing import Awaitable, Callable, Protocol


# A pure-text completion function; pluggable so tests can stub deterministically
# without depending on a live API. The orchestrator wires a Haiku-class
# adapter call here (see :func:`orchestrator.execute_phase` integration).
LLMCompleter = Callable[[str], Awaitable[str]]


class _ClassifiableEvent(Protocol):
    """Protocol satisfied by :class:`TrajectoryEvent` for trajectory summary
    rendering — kept loose so tests can pass plain dataclass shims if needed."""

    role: str
    action: str
    target_files: tuple[str, ...]
    success: bool


def _summarize_events(events: list[TrajectoryEvent]) -> str:
    """Compact per-event summary for the LLM prompt.

    Each event renders as a single line ``[role:action] files=... ok=true|false``
    so the LLM sees a tight trajectory chronology without the raw timestamps
    (which would only inflate the prompt without adding signal).
    """
    out: list[str] = []
    for ev in events:
        files = ",".join(ev.target_files) if ev.target_files else "-"
        out.append(
            f"[{ev.role}:{ev.action}] files={files} ok={'true' if ev.success else 'false'}"
        )
    return "\n".join(out)


def _build_classify_prompt(events: list[TrajectoryEvent], threshold: float) -> str:
    """Render a Haiku-friendly classifier prompt.

    The expected response is a single JSON line of the form:

        {"patterns": [{"name": "stuck_on_test", "confidence": 0.85}]}

    The orchestrator parses any names that match the canonical
    :data:`_PATTERN_TAXONOMY` keys with confidence ≥ ``threshold``.
    """
    summary = _summarize_events(events)
    return (
        "You are a trajectory classifier. Given a chronological list of "
        "agent dispatches, identify any failure-mode patterns from this set:\n"
        f"{', '.join(_PATTERN_TAXONOMY)}\n\n"
        "Patterns may overlap. Respond with ONE JSON line ONLY (no prose), "
        "matching this schema:\n"
        '{"patterns": [{"name": "<pattern>", "confidence": 0.0-1.0}]}\n\n'
        f"Only include patterns whose confidence is >= {threshold:.2f}.\n\n"
        "Trajectory:\n"
        f"{summary}\n"
    )


_PATTERN_KEY_RE = _re.compile(r'"name"\s*:\s*"([^"]+)"')
_CONF_RE = _re.compile(r'"confidence"\s*:\s*([0-9.]+)')


def _parse_classify_response(text: str, threshold: float) -> list[Pattern]:
    """Parse the LLM's JSON response into a list of :class:`Pattern`.

    Defensive parser: tries strict JSON first, then falls back to regex
    extraction so a slightly-malformed LLM response (e.g. trailing prose)
    still yields useful patterns.
    """
    if not text or not text.strip():
        return []
    out: list[Pattern] = []
    try:
        # Locate first { ... } block to avoid pre/post prose.
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            obj = _json.loads(text[start : end + 1])
            for entry in obj.get("patterns", []):
                if not isinstance(entry, dict):
                    continue
                name = entry.get("name")
                conf = float(entry.get("confidence", 0.0))
                if (
                    isinstance(name, str)
                    and name in _PATTERN_TAXONOMY
                    and conf >= threshold
                ):
                    out.append(Pattern(name=name))
            return out
    except (ValueError, _json.JSONDecodeError):
        pass

    # Regex fallback: extract paired (name, confidence). Best-effort only.
    names = _PATTERN_KEY_RE.findall(text)
    confs = _CONF_RE.findall(text)
    for name, conf_s in zip(names, confs):
        try:
            conf = float(conf_s)
        except ValueError:
            continue
        if name in _PATTERN_TAXONOMY and conf >= threshold:
            out.append(Pattern(name=name))
    return out


class LLMTrajectoryClassifier:
    """v0.20.0 A1: LLM-augmented trajectory pattern classifier.

    Augments — does not replace — the rule-based detectors. The
    orchestrator runs both and merges results: rule-based patterns are
    primary (high precision), ML patterns are secondary (broader recall
    on novel failure modes the rules don't encode).

    The classifier is a thin wrapper around an :class:`LLMCompleter`
    callable. Tests stub the completer with a deterministic function;
    production wires through the existing adapter infrastructure
    (sonnet/haiku via the platform adapter).

    Usage:

        clf = LLMTrajectoryClassifier(completer=my_completer, threshold=0.7)
        patterns = await clf.classify(store.events_for(task_id))
    """

    def __init__(
        self,
        completer: LLMCompleter,
        threshold: float = 0.7,
        min_events: int = 3,
    ) -> None:
        self._completer = completer
        self._threshold = max(0.0, min(1.0, threshold))
        self._min_events = max(1, min_events)

    @property
    def threshold(self) -> float:
        return self._threshold

    async def classify(self, events: list[TrajectoryEvent]) -> list[Pattern]:
        """Classify the given event list. Returns ``[]`` on any error.

        Cold-start: when fewer than ``min_events`` events are available,
        skips the LLM call (the rule-based detectors handle short
        windows). Returning ``[]`` is the correct degradation — the
        executor falls through to rule-only patterns without surfacing
        a noisy "ML failed" warning.
        """
        if len(events) < self._min_events:
            return []
        prompt = _build_classify_prompt(events, self._threshold)
        try:
            response = await self._completer(prompt)
        except Exception:  # noqa: BLE001
            # Graceful fallback — never let LLM errors block the executor.
            return []
        return _parse_classify_response(response, self._threshold)


def merge_patterns(
    rules_patterns: list[Pattern],
    ml_patterns: list[Pattern],
) -> list[Pattern]:
    """Merge rules + ml pattern lists, deduplicate by name, sort by severity.

    Rules-first dedup: if a pattern is in both lists, the rule-based
    instance wins (canonical, no ML confidence to attach to a rule).
    Returns the union sorted by severity descending.
    """
    seen: set[str] = set()
    out: list[Pattern] = []
    for p in rules_patterns:
        if p.name in seen:
            continue
        seen.add(p.name)
        out.append(p)
    for p in ml_patterns:
        if p.name in seen:
            continue
        seen.add(p.name)
        out.append(p)
    out.sort(key=lambda p: p.severity, reverse=True)
    return out


__all__ = [
    "CourseCorrection",
    "LLMCompleter",
    "LLMTrajectoryClassifier",
    "Pattern",
    "PatternName",
    "TrajectoryEvent",
    "TrajectoryStore",
    "detect_context_thrash",
    "detect_expansion_drift",
    "detect_ping_pong",
    "detect_repetition_loop",
    "detect_stuck_on_test",
    "merge_patterns",
]
