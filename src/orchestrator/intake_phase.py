"""Intake & clarification phase (ADR-0045).

Inserted at the very front of ``run_plan_phase`` — before exploration's findings
feed framing — to do what a senior engineer does with a thin ticket: **assess →
gather → enrich → clarify (once) → lock → autonomous.** A well-formed spec is a
zero-cost pass-through (deterministic gate, no LLM/network); only the gap path
gathers facts, dispatches the ``intake_enricher`` to merge them into a
provenance-cited spec, dispatches the ``intake_clarifier`` for constraint-only
questions, applies the ``on_unanswered`` headless policy (so CI never hangs),
then locks the enriched, answered spec to ``.autodev/spec.md``.

Dispatch note: ``intake_enricher`` / ``intake_clarifier`` are NOT in
``REQUIRED_AGENT_ROLES`` and therefore NOT in ``orch.registry``. They dispatch
via the specialist path (:func:`_invoke_intake_role`, mirroring
``framing_phase._invoke_framing_role``) — never ``_delegate`` (which is
registry-gated and would raise ``role not in registry``).

Boundary discipline (KD1): intake elicits *constraints*, never *solutions* — the
altitude decision (patch-vs-architecture) stays with the framing phase (ADR-0044).
Fail-safe + flag-guarded: any uncaught error degrades to "use the raw intent and
proceed", exactly as framing degrades to ``local_patch``. Intake must NEVER block
planning. Deterministic-on-resume: if ``plan-intake`` evidence exists, re-read it
and return with zero dispatches.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from autologging import get_logger

from adapters.types import AgentInvocation
from agents import load_prompt
from orchestrator.intake_sources import gather_facts
from orchestrator.spec_validator import assess
from state.evidence import read_evidence, write_evidence
from state.paths import ensure_autodev_dir, spec_path
from state.schemas import (
    ClarifyingAnswer,
    ClarifyingQuestion,
    GatheredFact,
    IntakeEvidence,
    SpecGaps,
)

if TYPE_CHECKING:
    from orchestrator import Orchestrator

logger = get_logger()

_EVIDENCE_TASK_ID = "plan-intake"

# Bound the enriched spec so a runaway enricher cannot bloat spec.md / evidence.
_MAX_SPEC_CHARS = 24_000

# WS-SCALE-01 (gate S4): "large repo" thresholds for the scale_context that
# intake surfaces from the repo_probe snapshot. A repo is large iff it is deep
# (``depth_max > 8`` — heavy navigation cost) OR its files are big on average
# (``avg_file_size_bytes > 50_000`` — context-window pressure per read). These
# are independent of repo_probe's own ``is_huge`` (file-count / total-bytes),
# which framing also consumes; ``is_large`` targets the per-file/depth shape.
_LARGE_DEPTH_THRESHOLD = 8
_LARGE_AVG_FILE_BYTES_THRESHOLD = 50_000

# A ```questions field line, leading whitespace + optional ``-`` bullet stripped.
_Q_FIELD_RE = re.compile(r"^\s*-?\s*([a-z_]+):\s*(.*)$")
# A field line that STARTS a new question record (anchored on ``id:``).
_Q_START_RE = re.compile(r"^\s*-?\s*id:\s*", re.IGNORECASE)
_QUESTIONS_BLOCK_RE = re.compile(r"```questions\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass
class IntakeOutcome:
    """In-memory result handed to the call site (the durable record is IntakeEvidence).

    Integration threads :attr:`spec` into the explorer/domain_expert/framing/
    architect ``intent=`` and the architect context, and :attr:`assumptions` into
    the audit trail. :attr:`spec_hash` anchors the rest of the run.

    ``degraded`` is ``True`` when intake fell back to the raw intent (disabled,
    kill-switch, or an uncaught error) — the spec is the original, untouched.
    """

    spec: str
    spec_hash: str
    assumptions: list[str] = field(default_factory=list)
    degraded: bool = False
    passthrough: bool = False
    # WS-SCALE-01 (gate S4): repo-scale signals READ from the orchestrator's
    # :class:`~runtime.repo_probe.RepoCapacity` snapshot and surfaced for
    # downstream framing. Always populated (every path computes it); carries at
    # least ``{'is_large','depth_max','avg_file_size_bytes'}``.
    scale_context: dict[str, object] = field(default_factory=dict)


def _check_intake_disabled() -> bool:
    """Honor the ``AUTODEV_INTAKE_DISABLED=1`` kill-switch (mirrors AUTODEV_FRAMING_DISABLED)."""
    return os.environ.get("AUTODEV_INTAKE_DISABLED", "").strip() == "1"


def _spec_hash(text: str) -> str:
    """Compute the locked spec_hash (mirrors ``plan_phase._spec_hash``)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _build_scale_context(capacity: object) -> dict[str, object]:
    """WS-SCALE-01 (gate S4): derive ``scale_context`` from a repo_probe snapshot.

    Reads the shape signals computed by :func:`runtime.repo_probe.probe_repo`
    (``depth_max`` / ``avg_file_size_bytes`` / ``largest_dir`` / …) — historically
    NOTHING outside ``repo_probe`` consumed them, so "scale-aware" was vacuous.
    This is the production-side wiring: the intake phase surfaces them on its
    output so the downstream framing phase can size its strategy.

    ``is_large`` is True iff the repo is DEEP (``depth_max > 8``) OR its files are
    big on average (``avg_file_size_bytes > 50_000``). Duck-typed on *capacity*
    (typed ``object`` to avoid a hard import); missing attributes degrade to 0 so
    a malformed snapshot yields ``is_large=False`` rather than raising.

    Coordinated contract (with the framing agent): the dict carries at least
    ``{'is_large','depth_max','avg_file_size_bytes'}``; the additional keys are
    additive and harmless to consumers that ignore them.
    """
    depth_max = int(getattr(capacity, "depth_max", 0) or 0)
    avg_file_size_bytes = int(getattr(capacity, "avg_file_size_bytes", 0) or 0)
    is_large = (
        depth_max > _LARGE_DEPTH_THRESHOLD
        or avg_file_size_bytes > _LARGE_AVG_FILE_BYTES_THRESHOLD
    )
    return {
        "is_large": is_large,
        "depth_max": depth_max,
        "avg_file_size_bytes": avg_file_size_bytes,
        # Additive signals (framing may ignore them; useful for sparse-checkout
        # / navigation tuning). All READ from the same probe snapshot.
        "file_count": int(getattr(capacity, "file_count", 0) or 0),
        "total_bytes": int(getattr(capacity, "total_bytes", 0) or 0),
        "is_huge": bool(getattr(capacity, "is_huge", False)),
        "largest_dir": str(getattr(capacity, "largest_dir", "") or ""),
    }


def _scale_context_for(orch: "Orchestrator") -> dict[str, object]:
    """Read ``orch.repo_capacity`` and build the scale_context (fail-safe).

    The repo probe is lazy + cached on the orchestrator; any failure degrades to
    an empty-but-typed scale_context (``is_large=False``) so intake NEVER blocks
    planning on a probe hiccup.
    """
    try:
        return _build_scale_context(orch.repo_capacity)
    except Exception as exc:  # noqa: BLE001 - scale wiring must never block intake
        logger.warning("intake_phase.scale_context_failed", err=str(exc))
        return _build_scale_context(
            None  # duck-typed: all getattrs default → is_large False
        )


def _render_context(envelope_context: dict[str, str], action: str) -> str:
    """Render the CONTEXT block appended to the role prompt.

    Mirrors ``framing_phase._render_context``: the specialist path does not call
    ``render_prompt``, so the prompt body references a ``CONTEXT`` block (not
    ``{{…}}`` placeholders).
    """
    parts = ["## CONTEXT", f"action: {action}"]
    for key, value in envelope_context.items():
        parts.append(f"\n### {key}\n{value}")
    return "\n".join(parts)


async def _invoke_intake_role(
    orch: "Orchestrator",
    role: str,
    envelope_context: dict[str, str],
    action: str,
) -> str:
    """Dispatch an unregistered specialist role via the ``load_prompt`` path.

    Mirrors ``framing_phase._invoke_framing_role`` — NEVER ``_delegate`` (which is
    registry-gated). Reads model/max-turns from ``cfg.agents[role]``; honors the
    intake model overrides (``enricher_model`` / ``clarifier_model``).
    """
    raw_prompt = load_prompt(role)
    context_block = _render_context(envelope_context, action)
    full_prompt = "\n\n---\n".join([raw_prompt.strip(), context_block])

    in_cfg = orch.cfg.intake
    agent_cfg = orch.cfg.agents[role]
    override = (
        in_cfg.enricher_model if role == "intake_enricher" else in_cfg.clarifier_model
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


def _render_facts(facts: list[GatheredFact]) -> str:
    """Render gathered facts for the enricher's CONTEXT (one per line, with refs)."""
    if not facts:
        return "(no facts gathered)"
    return "\n".join(f"- {f.source} | {f.ref} | {f.summary}" for f in facts)


def _parse_questions(text: str, max_questions: int) -> list[ClarifyingQuestion]:
    """Parse the ```questions fenced block into ClarifyingQuestions (fail-safe).

    Per-question parse/validation errors (missing field, ``recommended`` not in
    ``options``, an extra field under ``extra='forbid'``) skip that question —
    this NEVER raises. Over-count truncates to ``max_questions``. Empty / no block
    yields ``[]`` (no questions worth asking).
    """
    if not text or not text.strip():
        return []
    m = _QUESTIONS_BLOCK_RE.search(text)
    if m is None:
        return []
    block = m.group(1)
    out: list[ClarifyingQuestion] = []
    for item in _split_question_items(block):
        fields = _parse_question_fields(item)
        if not fields:
            continue
        # ``recommended`` must be one of ``options`` (mirrors the prompt contract);
        # if the model violates it, fall back to the first option rather than drop.
        opts = fields.get("options") or []
        rec = fields.get("recommended", "")
        if isinstance(opts, list) and opts and rec not in opts:
            fields["recommended"] = opts[0]
        try:
            out.append(ClarifyingQuestion(**fields))  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - one malformed question never sinks intake
            continue
    return out[:max_questions]


def _split_question_items(block: str) -> list[str]:
    """Split a ```questions block into per-question chunks, anchored on ``id:``."""
    items: list[str] = []
    current: list[str] | None = None
    for line in block.splitlines():
        if _Q_START_RE.match(line):
            if current is not None:
                items.append("\n".join(current))
            current = [line]
        elif current is not None:
            current.append(line)
    if current is not None:
        items.append("\n".join(current))
    return items


def _parse_question_fields(item: str) -> dict[str, object]:
    fields: dict[str, object] = {}
    for line in item.splitlines():
        m = _Q_FIELD_RE.match(line)
        if m is None:
            continue
        key = m.group(1)
        value = m.group(2).strip()
        if key == "options":
            fields[key] = _parse_options(value)
        else:
            fields[key] = value
    return fields


def _parse_options(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()]
    return [value] if value else []


def _apply_headless_policy(
    questions: list[ClarifyingQuestion], on_unanswered: str
) -> tuple[list[ClarifyingAnswer], list[str], bool]:
    """Apply the ``on_unanswered`` policy in a non-interactive run.

    Returns ``(answers, assumptions, blocked)``. ``assume_defaults`` records each
    question's ``recommended`` as a ``default_assumed`` answer + a human-readable
    assumption line. ``block`` / ``fail`` return ``blocked=True`` with no answers
    (the caller emits the question set and stops / exits — but intake the *phase*
    still degrades to the enriched-or-raw spec so planning is never wedged here;
    the CLI standalone surface is where ``fail`` exits non-zero).
    """
    if on_unanswered == "assume_defaults":
        answers = [
            ClarifyingAnswer(
                question_id=q.id, answer=q.recommended, source="default_assumed"
            )
            for q in questions
        ]
        assumptions = [
            f"{q.id}: assumed '{q.recommended}' (headless default) — {q.question}"
            for q in questions
        ]
        return answers, assumptions, False
    # block / fail: no answers assumed; signal blocked so the caller emits the set.
    return [], [], True


def _emit_questions_block(
    questions: list[ClarifyingQuestion], max_questions: int, on_unanswered: str
) -> str:
    """Build the §3.5 machine-readable ``autodev_intake_questions`` JSON block.

    The host (e.g. Claude Code's question UI) renders this; absent a host the CLI
    applies ``on_unanswered``. UI-agnostic by design.
    """
    import json

    payload = {
        "autodev_intake_questions": [
            {
                "id": q.id,
                "question": q.question,
                "kind": q.kind,
                "options": list(q.options),
                "recommended": q.recommended,
            }
            for q in questions
        ],
        "max_questions": max_questions,
        "on_unanswered": on_unanswered,
    }
    return json.dumps(payload, indent=2)


def _render_locked_spec(
    enriched: str, answers: list[ClarifyingAnswer]
) -> str:
    """Render the final locked spec = enriched draft + an Answered constraints section."""
    spec = enriched.strip()
    if answers:
        lines = ["", "## Answered constraints", ""]
        for a in answers:
            tag = "operator" if a.source == "operator" else "assumed default"
            lines.append(f"- {a.question_id}: {a.answer} ({tag})")
        spec = spec + "\n" + "\n".join(lines) + "\n"
    return spec[:_MAX_SPEC_CHARS]


def _outcome_from_evidence(ev: IntakeEvidence) -> IntakeOutcome:
    """Reconstruct an IntakeOutcome from persisted evidence (resume path, 0 dispatches)."""
    return IntakeOutcome(
        spec=ev.enriched_spec,
        spec_hash=ev.locked_spec_hash,
        assumptions=list(ev.assumptions),
        degraded=False,
        passthrough=not ev.gathered and not ev.questions,
        # WS-SCALE-01: the persisted scale_context wins on resume (0 re-probe),
        # so resumed runs see the SAME scale signal the original run locked in.
        scale_context=dict(ev.scale_context),
    )


async def _ledger(orch: "Orchestrator", op: str, payload: dict) -> None:
    """Best-effort ledger breadcrumb — intake must never block planning on I/O."""
    try:
        await orch.plan_manager.ledger_append(op, payload)
    except Exception as exc:  # noqa: BLE001 - best-effort audit
        logger.warning("intake_phase.ledger_failed", op=op, err=str(exc))


def _lock_spec(cwd, spec: str) -> str:
    """Atomic-ish write of the locked spec to ``.autodev/spec.md``; return spec_hash."""
    ensure_autodev_dir(cwd)
    sp = spec_path(cwd)
    sp.write_text(spec.strip() + "\n", encoding="utf-8")
    return _spec_hash(spec)


async def _persist(
    orch: "Orchestrator",
    *,
    raw_intent: str,
    gaps: SpecGaps,
    gathered: list[GatheredFact],
    enriched: str,
    questions: list[ClarifyingQuestion],
    answers: list[ClarifyingAnswer],
    assumptions: list[str],
    spec_hash: str,
    scale_context: dict[str, object],
) -> None:
    """Write the IntakeEvidence bundle (crash-safe) BEFORE the outcome is returned."""
    ev = IntakeEvidence(
        task_id=_EVIDENCE_TASK_ID,
        raw_intent=raw_intent,
        gaps=gaps,
        gathered=gathered,
        enriched_spec=enriched,
        questions=questions,
        answers=answers,
        assumptions=assumptions,
        locked_spec_hash=spec_hash,
        sources_used=list(orch.cfg.intake.sources),
        excluded_globs=list(orch.cfg.intake.exclude_globs),
        scale_context=dict(scale_context),
    )
    await write_evidence(orch.cwd, _EVIDENCE_TASK_ID, ev)


async def _run_intake_phase_inner(
    orch: "Orchestrator", intent: str, *, interactive: bool
) -> IntakeOutcome:
    """The intake FSM (assess → gather → enrich → question → ask/default → lock).

    Raises on programmer error; the public :func:`run_intake_phase` wraps it in the
    fail-safe degrade. Separated so the wrapper stays a thin try/except.
    """
    cwd = orch.cwd

    # WS-SCALE-01 (gate S4): read the repo_probe scale signals ONCE and surface
    # them on every outcome (pass-through AND gap path). Computed up-front so the
    # downstream framing phase always receives a populated scale_context.
    scale_context = _scale_context_for(orch)

    # 1. ASSESS — deterministic, cheap. No gaps ⇒ pass-through fast path.
    gaps = assess(intent)
    await _ledger(orch, "intake_assessed", {"ok": gaps.ok, "missing": list(gaps.missing)})
    logger.info("intake_phase.assessed", ok=gaps.ok, missing=gaps.missing)

    if gaps.ok:
        # PASS_THROUGH: lock the raw intent as-is; minimal evidence; 0 LLM/network.
        spec_hash = _lock_spec(cwd, intent)
        await _persist(
            orch,
            raw_intent=intent,
            gaps=gaps,
            gathered=[],
            enriched=intent,
            questions=[],
            answers=[],
            assumptions=[],
            spec_hash=spec_hash,
            scale_context=scale_context,
        )
        await _ledger(orch, "spec_locked", {"spec_hash": spec_hash})
        logger.info(
            "intake_phase.passthrough",
            spec_hash=spec_hash,
            is_large=scale_context.get("is_large"),
        )
        return IntakeOutcome(
            spec=intent,
            spec_hash=spec_hash,
            assumptions=[],
            passthrough=True,
            scale_context=scale_context,
        )

    # 2. GATHER — non-LLM external + reuse the explorer pass (never raises).
    facts = await gather_facts(orch, cwd=cwd, intent=intent, gaps=gaps, cfg=orch.cfg.intake)
    await _ledger(
        orch,
        "intake_gathered",
        {"n_facts": len(facts), "sources": sorted({f.source for f in facts})},
    )

    # 3. ENRICH — merge intent + facts into a provenance-cited spec draft (+1 LLM).
    enriched_raw = await _invoke_intake_role(
        orch,
        "intake_enricher",
        {
            "raw_intent": intent,
            "gathered_facts": _render_facts(facts),
            "missing_dimensions": ", ".join(gaps.missing) or "none",
        },
        action="enrich",
    )
    enriched = (enriched_raw or "").strip() or intent
    enriched = enriched[:_MAX_SPEC_CHARS]
    await _ledger(orch, "intake_enriched", {"chars": len(enriched)})

    # 4. QUESTION — constraint-only clarifying questions (≤ max_questions).
    max_q = orch.cfg.intake.max_questions
    clar_raw = await _invoke_intake_role(
        orch,
        "intake_clarifier",
        {
            "enriched_spec": enriched,
            "residual_gaps": ", ".join(gaps.missing) or "none",
            "max_questions": str(max_q),
        },
        action="clarify",
    )
    questions = _parse_questions(clar_raw, max_q)
    await _ledger(orch, "intake_questions_posed", {"count": len(questions)})

    # 5/6. ASK / headless — emit the §3.5 block; honor on_unanswered.
    on_unanswered = orch.cfg.intake.on_unanswered
    answers: list[ClarifyingAnswer] = []
    assumptions: list[str] = []
    if questions:
        block = _emit_questions_block(questions, max_q, on_unanswered)
        logger.info("intake_phase.questions_emitted", block=block)
        # Interactive operator wiring is the CLI/host's job; the phase itself runs
        # headless under ``on_unanswered`` so the autonomous run never hangs.
        answers, assumptions, _blocked = _apply_headless_policy(questions, on_unanswered)
        if assumptions:
            await _ledger(orch, "intake_defaults_assumed", {"count": len(assumptions)})
        else:
            await _ledger(orch, "intake_answered", {"count": len(answers)})
    else:
        await _ledger(orch, "intake_answered", {"count": 0})

    # 7. LOCK + persist.
    locked = _render_locked_spec(enriched, answers)
    spec_hash = _lock_spec(cwd, locked)
    await _persist(
        orch,
        raw_intent=intent,
        gaps=gaps,
        gathered=facts,
        enriched=locked,
        questions=questions,
        answers=answers,
        assumptions=assumptions,
        spec_hash=spec_hash,
        scale_context=scale_context,
    )
    await _ledger(orch, "spec_locked", {"spec_hash": spec_hash})
    logger.info(
        "intake_phase.locked",
        spec_hash=spec_hash,
        n_facts=len(facts),
        n_questions=len(questions),
        n_assumptions=len(assumptions),
        is_large=scale_context.get("is_large"),
    )
    return IntakeOutcome(
        spec=locked,
        spec_hash=spec_hash,
        assumptions=assumptions,
        passthrough=False,
        scale_context=scale_context,
    )


async def run_intake_phase(
    orch: "Orchestrator", intent: str, *, interactive: bool = False
) -> IntakeOutcome:
    """Assess → gather → enrich → clarify (once) → lock the intent (ADR-0045).

    Flag-guarded (``cfg.intake.enabled`` + ``AUTODEV_INTAKE_DISABLED``) and
    fail-safe: any uncaught error degrades to the raw intent (a ``degraded``
    outcome) so intake NEVER blocks planning. Deterministic-on-resume: if
    ``plan-intake`` evidence exists, re-read it and return with ZERO dispatches.

    Returns an :class:`IntakeOutcome` whose ``spec`` Integration threads into the
    downstream envelopes. On the well-formed fast path the spec is the raw intent
    (``passthrough=True``) with +0 LLM/network cost.
    """
    cwd = orch.cwd

    # 1. Enable / kill-switch — degrade to the raw intent (no lock side-effects).
    # WS-SCALE-01: still surface the scale_context so a disabled-intake run does
    # not starve downstream framing of the repo-scale signal.
    if not orch.cfg.intake.enabled or _check_intake_disabled():
        logger.info("intake_phase.disabled")
        return IntakeOutcome(
            spec=intent,
            spec_hash=_spec_hash(intent),
            degraded=True,
            scale_context=_scale_context_for(orch),
        )

    # 2. Resume re-read FIRST — before assess and before any dispatch.
    existing = await read_evidence(cwd, _EVIDENCE_TASK_ID, "intake")
    if isinstance(existing, IntakeEvidence):
        logger.info("intake_phase.resumed", spec_hash=existing.locked_spec_hash)
        return _outcome_from_evidence(existing)

    logger.info("intake_phase.start")
    try:
        return await _run_intake_phase_inner(orch, intent, interactive=interactive)
    except Exception as exc:  # noqa: BLE001 - intake must never block planning
        logger.warning("intake_phase.degraded", err=str(exc))
        # ADR-0047 (B1): make the degrade EXPLICIT in the ledger instead of a
        # silent warning (the Run-4 DOA lesson). Observability-only — intake
        # still returns its degraded pass-through outcome.
        try:
            from orchestrator.blocker_resolver import record_phase_degrade

            await record_phase_degrade(orch, "intake", exc)
        except Exception:  # noqa: BLE001
            pass
        return IntakeOutcome(
            spec=intent,
            spec_hash=_spec_hash(intent),
            degraded=True,
            scale_context=_scale_context_for(orch),
        )


__all__ = ["IntakeOutcome", "run_intake_phase"]
