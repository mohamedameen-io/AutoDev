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

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from orchestrator import Orchestrator

logger = get_logger()

_EVIDENCE_TASK_ID = "plan-framing"

_CLASSIFICATION_RE = re.compile(
    r"CLASSIFICATION:\s*(local_defect|realized_design_failure)", re.IGNORECASE
)
_CONFIDENCE_RE = re.compile(r"CONFIDENCE:\s*([0-9]*\.?[0-9]+)", re.IGNORECASE)

# Lexical scrutiny-only signal — NEVER structural, can never alone flip the class.
_TRIM_WORDS = ("trim", "shrink", "reduce", "remove", "delete", "strip", "cut", "drop")


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


_APPROACH_FIELD_RE = re.compile(r"^\s*([a-z_]+):\s*(.*)$")


def _extract_fenced_block(text: str, lang: str) -> str | None:
    m = re.search(rf"```{lang}\s*\n(.*?)```", text, re.DOTALL)
    return m.group(1) if m else None


def _split_approach_items(block: str) -> list[str]:
    """Split a YAML-ish list into per-approach chunks on ``- `` boundaries."""
    items: list[str] = []
    current: list[str] | None = None
    for line in block.splitlines():
        if re.match(r"^\s*-\s+", line):
            if current is not None:
                items.append("\n".join(current))
            current = [re.sub(r"^\s*-\s+", "", line)]
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
        key, value = m.group(1), m.group(2).strip()
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
    low = intent.lower()
    return any(word in low for word in _TRIM_WORDS)


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


async def run_framing_phase(
    orch: "Orchestrator",
    intent: str,
    explorer_findings: str,
    domain_expert_findings: str,
    candidate_digest: CandidateDigest | None,
    spec_hash: str,
) -> AltitudeDecision | None:
    """Classify the defect and select an altitude (ADR-0044).

    Returns ``None`` when disabled (kill-switch / config). Otherwise returns an
    :class:`AltitudeDecision`. Deterministic-on-resume: re-reads ``plan-framing``
    evidence FIRST and skips the classifier with zero LLM calls.
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
            final_classification = "local_defect"
            is_design = False
            logger.info("framing_phase.local_defect_path", reason="parse_degraded")
        else:
            # Placeholder selection (Phase 3): first design_fix, else first approach.
            # Replaced by the altitude_judge Borda panel in Phase 4.
            chosen = next(
                (a for a in approaches if a.altitude == "design_fix"), approaches[0]
            )
            final_classification = "realized_design_failure"
            rationale = "placeholder selection (Phase 3): first design_fix"
            logger.info(
                "framing_phase.design_failure_path",
                confidence=confidence,
                n_approaches=len(approaches),
            )
    else:
        chosen = _local_patch_approach()
        approaches = [chosen]
        final_classification = "local_defect"
        logger.info("framing_phase.local_defect_path", confidence=confidence)

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
