"""Framing/altitude phase (ADR-0044).

Inserted between exploration and planning in ``run_plan_phase``: classifies a
defect as ``local_defect`` vs ``realized_design_failure`` (deterministic signals
gate + one conservative LLM call) and, on the design path, generates altitude-
diverse strategies selected by the ``altitude_judge`` Borda panel with minimality
suspended. The winner is handed to the architect, where minimality resumes.

Phase 2: skeleton + signals + classifier with a PLACEHOLDER single approach.
Phase 3 adds real multi-approach generation; Phase 4 adds the altitude panel.

Dispatch note: ``framing``/``altitude_judge`` are NOT in ``REQUIRED_AGENT_ROLES`` and
therefore NOT in ``orch.registry``. They dispatch via the specialist path
(:func:`_invoke_framing_role`, mirroring ``review_tournament_runner._call_role_with_prompt``)
— never ``_delegate`` (which raises ``role not in registry``).
"""

from __future__ import annotations

import asyncio
import os
import random
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from autologging import get_logger
from pydantic import ValidationError

from adapters.types import AgentInvocation
from agents import load_prompt
from orchestrator.framing_signals import (
    compute_boundary_repeatedly_touched,
    compute_recurrence_at_seam,
)
from state.evidence import read_evidence, write_evidence
from state.file_index import CandidateDigest
from state.schemas import FramingEvidence, SolutionApproach
from tournament.core import parse_ranking
from tournament.voting import BordaAggregator

if TYPE_CHECKING:
    from orchestrator import Orchestrator

logger = get_logger()

_EVIDENCE_TASK_ID = "plan-framing"

# WS2-17 taxonomy. The two BUG classes (``local_defect`` /
# ``realized_design_failure``) drive the design-altitude tournament and the
# conservatism gate; ``local_defect`` is also the skeptical/fail-safe default.
# The three WORK-TYPE classes are NEW exits for feature / refactor / greenfield
# work that previously got mislabelled as ``local_defect``: on the non-design
# path these are preserved verbatim (a gated bug-class still collapses to the
# conservative ``local_defect`` — see the ``else`` branch in run_framing_phase).
_WORK_TYPE_CLASSES = ("feature", "refactor", "greenfield")

# WS2-17: the taxonomy is no longer binary+bug-shaped. The two original BUG
# classes (``local_defect`` / ``realized_design_failure``) stay, and three
# WORK-TYPE classes are added so feature / refactor / greenfield specs exit
# framing correctly labelled instead of being forced to ``local_defect``. The
# alternation is ordered longest-first so e.g. ``realized_design_failure`` wins
# over a hypothetical prefix; word-class strings are disjoint here so order is
# not load-bearing, but keep it explicit.
_CLASSIFICATION_RE = re.compile(
    r"CLASSIFICATION:\s*"
    r"(realized_design_failure|local_defect|feature|refactor|greenfield)",
    re.IGNORECASE,
)
_CONFIDENCE_RE = re.compile(r"CONFIDENCE:\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)

# Lexical scrutiny-only signal — NEVER structural, can never alone flip the class.
# Word-boundary anchored so it does not false-positive on substrings (e.g. "cut"
# inside "exeCUTing"); the trailing ``\w*`` still catches suffixed forms (trimming,
# reduces, removed).
_TRIM_RE = re.compile(
    r"\b(?:trim|shrink|reduce|remove|delete|strip|cut|drop)\w*",
    re.IGNORECASE,
)


@dataclass
class AltitudeDecision:
    """In-memory result handed to the call site (the durable record is FramingEvidence)."""

    classification: str
    confidence: float
    chosen_approach: SolutionApproach
    approaches: list[SolutionApproach]


def _check_framing_disabled() -> bool:
    """Honor the ``AUTODEV_FRAMING_DISABLED=1`` kill-switch (mirrors AUTODEV_INDEX_DISABLED)."""
    return os.environ.get("AUTODEV_FRAMING_DISABLED", "").strip() == "1"


# Scale thresholds (S4 framing-side). The scale agent (intake_phase) is the
# producer of ``is_large`` and always sets it, so the authoritative ``is_large``
# branch below short-circuits in the real pipeline. These thresholds only feed
# the DEFENSIVE fallback for a partial ``scale_context`` (raw shape but no
# ``is_large``); kept aligned with the producer's contract
# (``depth_max > 8`` OR ``avg_file_size_bytes > 50_000``) so the fallback can
# never disagree with the producer were it ever reached.
_SCALE_DEPTH_THRESHOLD = 8
_SCALE_AVG_FILE_BYTES_THRESHOLD = 50_000


def _scale_is_large(scale_context: dict | None) -> bool:
    """Read the (parallel) scale agent's ``scale_context`` (S4 framing-side).

    Consumed shape (must match the scale agent)::

        {'is_large': bool, 'depth_max': int, 'avg_file_size_bytes': int}

    ``is_large`` is authoritative when present; otherwise the raw shape signals
    are used as a fallback so a partial dict still works. Absent / non-dict /
    empty ``scale_context`` returns ``False`` — fully backward compatible (the
    altitude stays exactly as it was before this field existed).
    """
    if not isinstance(scale_context, dict) or not scale_context:
        return False
    flag = scale_context.get("is_large")
    if isinstance(flag, bool):
        return flag
    depth = scale_context.get("depth_max")
    avg = scale_context.get("avg_file_size_bytes")
    large = False
    if isinstance(depth, int) and depth > _SCALE_DEPTH_THRESHOLD:
        large = True
    if isinstance(avg, int) and avg > _SCALE_AVG_FILE_BYTES_THRESHOLD:
        large = True
    return large


def _scale_aware_approach() -> SolutionApproach:
    """A component-level (NOT local_patch) approach for large repos (S4).

    When the scale agent signals a large repo, framing must NOT force the lowest
    altitude. This is the conservative non-local default: one altitude above
    ``local_patch`` so a large-repo change is not pinned to a single function.
    """
    return SolutionApproach(
        name="scale_aware_component",
        altitude="component_refactor",
        summary="Component-scoped change sized to a large repository.",
        eliminates_failure_class=False,
        primary_tradeoff="Wider than a one-line patch; scoped to the affected component.",
        primary_risk="Touches more than one file within the component boundary.",
        integration_surface=[],
        est_blast_radius="single component",
    )


def _local_patch_approach() -> SolutionApproach:
    """The single conservative approach for the local-defect / fail-safe path."""
    return SolutionApproach(
        name="local_patch",
        altitude="local_patch",
        summary="Localized fix for the reported symptom.",
        eliminates_failure_class=False,
        primary_tradeoff="Minimal change; bounds the symptom rather than eliminating the class.",
        primary_risk="The underlying failure class may recur at the same seam.",
        integration_surface=[],
        est_blast_radius="single function",
    )


# Tolerant of an optional leading ``-`` bullet (flat one-dash-per-field and nested
# sub-bullet renderings) and optional ``**`` bold markers around the key, so all of
# ``foo:`` / ``- foo:`` / ``**foo**:`` / ``- **foo**:`` parse to the same ``(key, value)``.
_APPROACH_FIELD_RE = re.compile(r"^\s*-?\s*\*{0,2}([a-z_]+)\*{0,2}:\s*(.*)$")


def _strip_inline_comment(value: str) -> str:
    """Strip a trailing `` # ...`` comment from a field value.

    The prompt example renders e.g. ``eliminates_failure_class: <true|false>  # ...``,
    so a model that echoes the inline comment would otherwise make ``true`` parse as
    the literal ``"true # ..."``. Only a ``#`` that follows whitespace (or starts the
    value) is treated as a comment, so ``#`` inside a value (rare, but e.g. an anchor
    like ``foo#bar``) is preserved.
    """
    m = re.search(r"(?:^|\s)#", value)
    return value[: m.start()].rstrip() if m else value


def _extract_fenced_block(text: str, lang: str) -> str | None:
    m = re.search(rf"```{lang}\s*\n(.*?)```", text, re.DOTALL)
    return m.group(1) if m else None


# A field line, with leading whitespace + optional ``-`` bullet + optional ``**`` bold
# already stripped, that starts a NEW approach. Anchored on the ``name:`` key so
# segmentation is driven by the record boundary — NOT by every ``-`` (which shatters
# indented sub-bullets and flat one-dash-per-field renderings into one fragment each).
_APPROACH_START_RE = re.compile(r"^\s*-?\s*\*{0,2}name\*{0,2}:\s*", re.IGNORECASE)


def _split_approach_items(block: str) -> list[str]:
    """Split a YAML-ish approaches list into per-approach chunks.

    Segments on the ``name:`` field (the record boundary) rather than on ``- ``, so
    every dash/indent/bold rendering of the same approaches collapses to one chunk
    each: canonical two-space continuations, nested ``  - field:`` sub-bullets, flat
    one-dash-per-field, and ``**bold**`` keys. Lines before the first ``name:`` (e.g.
    a stray header) are ignored. Field-regex tolerance (see ``_APPROACH_FIELD_RE``)
    means a chunk's raw lines parse regardless of their bullet/bold decoration.
    """
    items: list[str] = []
    current: list[str] | None = None
    for line in block.splitlines():
        if _APPROACH_START_RE.match(line):
            if current is not None:
                items.append("\n".join(current))
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        items.append("\n".join(current))
    return items


def _parse_list(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()]
    return [value] if value else []


def _parse_approach_fields(item: str) -> dict[str, object]:
    fields: dict[str, object] = {}
    for line in item.splitlines():
        m = _APPROACH_FIELD_RE.match(line)
        if m is None:
            continue
        key = m.group(1)
        # Strip a trailing `` # ...`` comment from EVERY value before interpreting it
        # — the prompt example renders the boolean with an inline comment, so an
        # echoed comment must not poison the truthy check (or any other field).
        value = _strip_inline_comment(m.group(2).strip()).strip()
        if key == "eliminates_failure_class":
            fields[key] = value.lower() in ("true", "yes", "1")
        elif key == "integration_surface":
            fields[key] = _parse_list(value)
        else:
            fields[key] = value
    return fields


def parse_approaches(
    text: str, num_approaches: int = 3
) -> tuple[list[SolutionApproach], list[str]]:
    """Parse the ```approaches fenced block into SolutionApproaches (fail-safe).

    Per-approach validation errors (a missing required field, or an extra field
    under ``extra='forbid'``) skip that approach with a ``framing: malformed approach
    <n>`` diagnostic — this NEVER raises. Over-count truncates to ``num_approaches``
    with a ``framing: truncated approaches`` diagnostic. Empty / no block degrades to
    ``([], [...])`` so the caller can fall back to ``local_defect`` (FR-4).
    """
    if not text or not text.strip():
        return [], ["framing: empty response"]
    block = _extract_fenced_block(text, "approaches")
    if block is None:
        return [], ["framing: no approaches block"]
    approaches: list[SolutionApproach] = []
    failures: list[str] = []
    for idx, item in enumerate(_split_approach_items(block), 1):
        fields = _parse_approach_fields(item)
        try:
            approaches.append(SolutionApproach(**fields))  # type: ignore[arg-type]
        except ValidationError:
            failures.append(f"framing: malformed approach {idx}")
    if len(approaches) > num_approaches:
        approaches = approaches[:num_approaches]
        failures.append("framing: truncated approaches")
    return approaches, failures


def _extract_classification(text: str) -> tuple[str, float, list[str]]:
    """Parse ``(classification, confidence, diagnostics)`` skeptically.

    Empty/``None`` or a missing ``CLASSIFICATION:`` line degrades to ``local_defect``
    with ``confidence==0.0`` (mirrors the ``drift_verifier`` skeptical default).
    """
    if not text or not text.strip():
        return "local_defect", 0.0, ["framing: empty response"]
    cm = _CLASSIFICATION_RE.search(text)
    if cm is None:
        return (
            "local_defect",
            0.0,
            ["framing: missing CLASSIFICATION line (skeptical default)"],
        )
    classification = cm.group(1).lower()
    diagnostics: list[str] = []
    conf_m = _CONFIDENCE_RE.search(text)
    if conf_m is None:
        diagnostics.append("framing: missing CONFIDENCE line (defaulting to 0.0)")
        confidence = 0.0
    else:
        try:
            confidence = float(conf_m.group(1))
        except ValueError:  # pragma: no cover - regex guarantees a float-shaped match
            confidence = 0.0
            diagnostics.append("framing: unparseable CONFIDENCE")
    confidence = max(0.0, min(1.0, confidence))
    return classification, confidence, diagnostics


def _extract_line(text: str, label: str) -> str | None:
    m = re.search(rf"{re.escape(label)}:\s*(.+)", text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _hypothesis_is_a_trim(intent: str) -> bool:
    return bool(_TRIM_RE.search(intent))


def _format_signals(signals_fired: list[str]) -> str:
    return ", ".join(signals_fired) if signals_fired else "none fired"


def _render_context(envelope_context: dict[str, str], action: str) -> str:
    """Render the CONTEXT block appended to the role prompt.

    The prompt body references a ``CONTEXT`` block — NOT ``{{…}}`` placeholders
    (the specialist path does not call ``render_prompt``, so nothing substitutes
    double-brace tokens).
    """
    parts = ["## CONTEXT", f"action: {action}"]
    for key, value in envelope_context.items():
        parts.append(f"\n### {key}\n{value}")
    return "\n".join(parts)


async def _invoke_framing_role(
    orch: "Orchestrator",
    role: str,
    envelope_context: dict[str, str],
    action: str,
) -> str:
    """Dispatch an unregistered specialist role via the ``load_prompt`` path.

    Mirrors ``review_tournament_runner._call_role_with_prompt`` — NEVER ``_delegate``
    (which is registry-gated and would raise ``role not in registry``). Reads
    model/max-turns from ``cfg.agents[role]``; honors the framing model overrides.
    """
    raw_prompt = load_prompt(role)
    context_block = _render_context(envelope_context, action)
    full_prompt = "\n\n---\n".join([raw_prompt.strip(), context_block])

    fr_cfg = orch.cfg.framing
    agent_cfg = orch.cfg.agents[role]
    override = (
        fr_cfg.classifier_model if role == "framing" else fr_cfg.altitude_judge_model
    )
    inv = AgentInvocation(
        role=role,
        prompt=full_prompt,
        cwd=orch.cwd,
        model=override or agent_cfg.model,
        max_turns=agent_cfg.max_turns or 1,
    )
    result = await orch.adapter.execute(inv)
    return result.text or ""


async def _compute_signals(
    cwd, candidate_digest: CandidateDigest | None, intent: str
) -> tuple[list[str], bool]:
    """Return ``(signals_fired, structural_fired)``.

    Only ``recurrence_at_seam`` / ``boundary_repeatedly_touched`` are structural. The
    lexical ``hypothesis_is_a_trim`` is recorded name-only and is NEVER structural.
    """
    signals_fired: list[str] = []
    structural_fired = False
    if candidate_digest is not None:
        _, _, rec_sig = await compute_recurrence_at_seam(cwd, candidate_digest)
        if rec_sig.fired:
            signals_fired.append(rec_sig.name)
            structural_fired = True
        _, bnd_sig = await compute_boundary_repeatedly_touched(cwd, candidate_digest)
        if bnd_sig.fired:
            signals_fired.append(bnd_sig.name)
            structural_fired = True
    if _hypothesis_is_a_trim(intent):
        signals_fired.append("hypothesis_is_a_trim")
    return signals_fired, structural_fired


def _decision_from_evidence(ev: FramingEvidence) -> AltitudeDecision:
    chosen: SolutionApproach | None = None
    if ev.chosen_approach_name:
        chosen = next(
            (a for a in ev.approaches if a.name == ev.chosen_approach_name), None
        )
    if chosen is None:
        chosen = ev.approaches[0] if ev.approaches else _local_patch_approach()
    return AltitudeDecision(
        ev.classification, ev.confidence, chosen, list(ev.approaches)
    )


def _shuffle_approaches(labels: list[str], rng: random.Random) -> dict[int, str]:
    """Shuffle approach NAMES into a slot order; return ``{slot_1based: canonical_name}``.

    Replaces ``randomize_for_judge`` (hardcoded A/B/AB, exactly 3) — this is N-generic.
    """
    shuffled = list(labels)
    rng.shuffle(shuffled)
    return {i + 1: name for i, name in enumerate(shuffled)}


def _render_approaches_in_slot_order(
    approaches: list[SolutionApproach], order: dict[int, str]
) -> str:
    by_name = {a.name: a for a in approaches}
    blocks: list[str] = []
    for slot in sorted(order):
        a = by_name[order[slot]]
        blocks.append(
            f"### Candidate {slot}\n"
            f"altitude: {a.altitude}\n"
            f"summary: {a.summary}\n"
            f"eliminates_failure_class: {a.eliminates_failure_class}\n"
            f"primary_tradeoff: {a.primary_tradeoff}\n"
            f"primary_risk: {a.primary_risk}\n"
            f"est_blast_radius: {a.est_blast_radius}"
        )
    return "\n\n".join(blocks)


async def _run_altitude_judge_panel(
    orch: "Orchestrator",
    approaches: list[SolutionApproach],
    intent: str,
    candidate_digest: CandidateDigest | None,
    spec_hash: str,
) -> tuple[SolutionApproach, str]:
    """Single-pass N-judge Borda panel over altitude-diverse approaches (minimality
    suspended, scoped to THIS step).

    Suspension is by construction: dispatches ``altitude_judge`` ONLY (never
    ``minimality_judge``/``judge``), uses its own prompt (never JUDGE_RANK_3 / the
    length penalty), never demotes oversized winners, and relies on the denylist.
    Deterministic: a single local ``random.Random(int(spec_hash, 16))`` is advanced per
    judge. Borda aggregates over canonical NAMES — each judge's slot ranking is
    inverse-mapped via THAT judge's order before aggregation.
    """
    labels = [a.name for a in approaches]
    n = len(approaches)
    # Derived from the ACTUAL count — NEVER the literal "123". ``parse_ranking`` rejects
    # a ranking whose valid-digit count is below ``len(valid_labels)``, so a literal
    # "123" would reject every N!=3 panel; N=2 is a real, gated branch (valid_labels="12").
    valid_labels = "".join(str(i) for i in range(1, n + 1))
    rng = random.Random(int(spec_hash, 16))  # seeded ONCE; advanced per judge
    panel_size = orch.cfg.framing.altitude_judge_panel_size
    orders = [_shuffle_approaches(labels, rng) for _ in range(panel_size)]

    candidate_files = candidate_digest.render() if candidate_digest is not None else ""

    async def _one_judge(order: dict[int, str]) -> str:
        rendered = _render_approaches_in_slot_order(approaches, order)
        return await _invoke_framing_role(
            orch,
            "altitude_judge",
            {
                "spec": intent,
                "approaches": rendered,
                "candidate_files": candidate_files,
            },
            action="rank",
        )

    results = await asyncio.gather(
        *[_one_judge(order) for order in orders], return_exceptions=True
    )

    rankings: list[list[str] | None] = []
    for order, res in zip(orders, results):
        if not isinstance(res, str):
            rankings.append(None)
            continue
        slot_ranking = parse_ranking(res, valid_labels)
        if slot_ranking is None:
            rankings.append(None)
            continue
        try:
            rankings.append([order[int(s)] for s in slot_ranking])
        except (KeyError, ValueError):
            rankings.append(None)

    # Conservative tiebreak: the local_patch approach (guaranteed present on the design
    # path). Must be a canonical NAME for BordaAggregator's priority map to apply it.
    local_patch_name = next(
        (a.name for a in approaches if a.altitude == "local_patch"), labels[0]
    )
    winner_name, scores, n_valid = BordaAggregator().aggregate(
        rankings, labels=labels, tiebreak_winner=local_patch_name
    )
    chosen = next(a for a in approaches if a.name == winner_name)
    rationale = (
        f"altitude_judge panel ({n_valid}/{panel_size} valid rankings) selected "
        f"'{winner_name}' ({chosen.altitude}); scores={scores}"
    )
    return chosen, rationale


async def run_framing_phase(
    orch: "Orchestrator",
    intent: str,
    explorer_findings: str,
    domain_expert_findings: str,
    candidate_digest: CandidateDigest | None,
    spec_hash: str,
    diagnosis_signals: object | None = None,
    scale_context: dict | None = None,
) -> AltitudeDecision | None:
    """Classify the defect and select an altitude (ADR-0044).

    Returns ``None`` when disabled (kill-switch / config). Otherwise returns an
    :class:`AltitudeDecision`. Deterministic-on-resume: re-reads ``plan-framing``
    evidence FIRST and skips the classifier with zero LLM calls.

    S4 (framing-side): ``scale_context`` is the (parallel) scale agent's repo
    shape, threaded in from the intake/enricher output. Consumed shape (must
    match the scale agent)::

        {'is_large': bool, 'depth_max': int, 'avg_file_size_bytes': int}

    When scale signals are HIGH and the classifier did not already select a
    design altitude, framing does NOT force the lowest (``local_patch``)
    altitude — it picks a component-level default instead. Absent / non-dict
    ``scale_context`` is fully backward compatible (behaves as before).

    ADR-0046 integration: ``diagnosis_signals`` is an optional
    :class:`orchestrator.diagnosis_phase.DiagnosisOutcome` (typed loosely as
    ``object | None`` to avoid a circular import; duck-typed below). When the
    diagnosis phase ran and found NO correct seam, an additive structural
    signal (``diagnosis_no_correct_seam``) is appended and ``structural_fired``
    is set — this lets the conservatism gate ALLOW a design classification when
    the classifier ALSO says ``realized_design_failure``; it never FORCES one.
    A ``correct`` seam adds an informational ``diagnosis_correct_seam`` signal
    (local_defect remains the conservative default). ``None`` is a no-op.
    """
    cwd = orch.cwd
    fr_cfg = orch.cfg.framing

    # 1. Enable / kill-switch — both off-ramps return None (architect gets the default).
    if not fr_cfg.enabled or _check_framing_disabled():
        logger.info("framing_phase.disabled")
        return None

    logger.info("framing_phase.start", spec_hash=spec_hash)

    # 2. Resume re-read FIRST — before signals AND before the classifier (net-new).
    existing = await read_evidence(cwd, _EVIDENCE_TASK_ID, "framing")
    if isinstance(existing, FramingEvidence):
        logger.info("framing_phase.resumed", classification=existing.classification)
        return _decision_from_evidence(existing)

    # 3. Deterministic structural signals (disconfirming evidence for the classifier).
    signals_fired, structural_fired = await _compute_signals(
        cwd, candidate_digest, intent
    )

    # 3b. ADR-0046: additive diagnosis biasing. Duck-typed (no import of
    # DiagnosisOutcome to keep this module import-light). Only fires when the
    # diagnosis phase actually ran (``.ran``). This is structural-signal
    # biasing ONLY — it never touches the classifier's own logic, the
    # threshold, or the existing signals; the bias flows through the persisted
    # ``signals_fired`` and (for no-correct-seam) ``structural_fired``.
    diagnosis_summary = ""
    if diagnosis_signals is not None and getattr(diagnosis_signals, "ran", False):
        dx_seam = getattr(diagnosis_signals, "seam", "unknown")
        dx_cause = getattr(diagnosis_signals, "confirmed_cause", None)
        diagnosis_summary = f"confirmed_cause={dx_cause}; seam={dx_seam}"
        if dx_seam == "none" or getattr(
            diagnosis_signals, "no_correct_seam", False
        ):
            # No correct seam: let the conservatism gate ALLOW a design class
            # (does NOT force it — the classifier must still agree).
            signals_fired = signals_fired + ["diagnosis_no_correct_seam"]
            structural_fired = True
        elif dx_seam == "correct":
            # Informational only — local_defect stays the conservative default.
            signals_fired = signals_fired + ["diagnosis_correct_seam"]

    # 4. One conservative classifier call (specialist dispatch — never _delegate).
    raw = await _invoke_framing_role(
        orch,
        "framing",
        {
            "spec": intent,
            "explorer_findings": explorer_findings,
            "domain_expert_findings": domain_expert_findings,
            "candidate_files": (
                candidate_digest.render() if candidate_digest is not None else ""
            ),
            "signals_summary": _format_signals(signals_fired),
            "diagnosis_summary": diagnosis_summary,
            "num_approaches": str(fr_cfg.num_approaches),
        },
        action="classify",
    )

    # 5. Parse skeptically.
    classification, confidence, _diagnostics = _extract_classification(raw)
    hypothesis = _extract_line(raw, "HYPOTHESIS_CHALLENGED") or ""

    # 6. Conservatism gate — flip ONLY if all three hold (>=, AND, structural-gated).
    is_design = (
        classification == "realized_design_failure"
        and confidence >= fr_cfg.design_smell_threshold
        and (not fr_cfg.require_structural_signal or structural_fired)
    )

    rationale: str | None = None
    if is_design:
        approaches, parse_failures = parse_approaches(
            raw, num_approaches=fr_cfg.num_approaches
        )
        if parse_failures:
            logger.info("framing_phase.parse_failures", failures=parse_failures)
        non_local = [a for a in approaches if a.altitude != "local_patch"]
        if len(approaches) < 2 or not non_local:
            # Fail-safe degrade: generation did not yield a usable design option.
            signals_fired = signals_fired + ["parse_degraded"]
            chosen = _local_patch_approach()
            approaches = [chosen]
            final_classification: Literal[
                "local_defect",
                "realized_design_failure",
                "feature",
                "refactor",
                "greenfield",
            ] = "local_defect"
            is_design = False
            logger.info("framing_phase.local_defect_path", reason="parse_degraded")
            # v0.42.1 F1b (ADR-0047): the framing phase classified a
            # design-altitude failure but generation produced no usable design
            # option, so it silently falls back to a local patch. Make that
            # degrade EXPLICIT in the ledger via the resolver (observability
            # only — framing still returns its degraded local_defect outcome).
            try:
                from orchestrator.blocker_resolver import record_phase_degrade

                await record_phase_degrade(
                    orch,
                    "framing",
                    RuntimeError(
                        "framing parse_degraded: design classified but no "
                        "usable non-local approach generated"
                    ),
                )
            except Exception:  # noqa: BLE001 - observability must never break framing
                pass
        else:
            # Real selection: altitude_judge Borda panel (minimality suspended).
            chosen, rationale = await _run_altitude_judge_panel(
                orch, approaches, intent, candidate_digest, spec_hash
            )
            final_classification = "realized_design_failure"
            logger.info(
                "framing_phase.design_failure_path",
                confidence=confidence,
                n_approaches=len(approaches),
                chosen=chosen.name,
            )
    else:
        # Non-design path. WS2-17: preserve the classifier's WORK-TYPE class
        # (feature / refactor / greenfield) instead of forcing ``local_defect``
        # — work-type specs must exit framing correctly labelled. The BUG
        # classes stay conservative: a ``realized_design_failure`` that reached
        # this branch was GATED (low confidence / no structural signal), so it
        # must collapse to the conservative ``local_defect`` default — NOT
        # persist the ungated raw class. Anything unrecognised also defaults to
        # ``local_defect``.
        if classification in _WORK_TYPE_CLASSES:
            final_classification = classification  # type: ignore[assignment]
        else:
            final_classification = "local_defect"
        # S4 (framing-side): do NOT unconditionally pick the lowest altitude
        # when the scale agent signals a large repo. A large repo gets a
        # component-level default; small / absent scale stays local_patch
        # (fully backward compatible).
        large = _scale_is_large(scale_context)
        if large:
            chosen = _scale_aware_approach()
            signals_fired = signals_fired + ["scale_large_altitude_raised"]
            logger.info(
                "framing_phase.scale_aware_altitude",
                classification=final_classification,
                altitude=chosen.altitude,
                scale_is_large=True,
            )
        else:
            chosen = _local_patch_approach()
        approaches = [chosen]
        logger.info(
            "framing_phase.local_defect_path",
            confidence=confidence,
            classification=final_classification,
            scale_is_large=large,
        )

    # 7. Persist evidence BEFORE returning (crash-safety).
    ev = FramingEvidence(
        task_id=_EVIDENCE_TASK_ID,
        classification=final_classification,
        confidence=confidence,
        hypothesis_challenged=hypothesis,
        signals_fired=signals_fired,
        approaches=approaches,
        chosen_approach_name=chosen.name,
        altitude_rationale=rationale,
        # Preserve the classifier's raw text so a later parse_degraded is diagnosable.
        raw_response=raw,
    )
    await write_evidence(cwd, _EVIDENCE_TASK_ID, ev)
    logger.info(
        "framing_phase.classified",
        classification=final_classification,
        confidence=confidence,
        signals_fired=signals_fired,
        chosen=chosen.name,
    )

    # 8. Best-effort ledger breadcrumbs — framing must never block planning.
    try:
        await orch.plan_manager.ledger_append(
            "framing_classified",
            {
                "classification": final_classification,
                "confidence": confidence,
                "signals_fired": signals_fired,
            },
        )
    except Exception as exc:  # noqa: BLE001 - best-effort audit
        logger.warning("framing_phase.ledger_classified_failed", err=str(exc))
    if is_design:
        try:
            await orch.plan_manager.ledger_append(
                "framing_strategy_chosen",
                {"chosen_approach_name": chosen.name, "altitude": chosen.altitude},
            )
        except Exception as exc:  # noqa: BLE001 - best-effort audit
            logger.warning("framing_phase.ledger_strategy_failed", err=str(exc))

    logger.info(
        "framing_phase.complete",
        chosen_altitude=chosen.altitude,
        evidence_path=str(
            cwd / ".autodev" / "evidence" / "plan-framing-framing.json"
        ),
    )
    return AltitudeDecision(final_classification, confidence, chosen, approaches)
