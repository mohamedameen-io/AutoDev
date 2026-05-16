"""Drift-verifier wiring (v0.16.0).

Glue between :class:`Orchestrator` and the existing
``critic_drift_verifier`` agent prompt. Builds a
:class:`DelegationEnvelope` carrying a ``DRIFT_VERIFY_CONTEXT`` block,
dispatches the critic via the platform adapter, parses the verdict, and
persists evidence under
``.autodev/evidence/{phase_id}-drift-verifier.json``.

The agent prompt at ``src/agents/prompts/critic_drift_verifier.md`` is
unchanged in v0.16.0 — this module ONLY wires it in.

Skeptical-by-default parsing: any response without an explicit ``VERDICT:
APPROVED`` line is treated as failure. The drift verifier is a final-
defense gate; the cost of a false negative (drift slipping through) is
strictly higher than a false positive (re-running phase review on a
clean phase).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from adapters.types import AgentInvocation
from autologging import get_logger


if TYPE_CHECKING:
    from state.schemas import Phase


logger = get_logger(__name__)


# Conservative parser. Matches the prompt's documented output format:
# ``VERDICT: APPROVED`` or ``VERDICT: NEEDS_REVISION``. Case-insensitive
# because some critic responses lowercase the verdict word.
_VERDICT_RE = re.compile(
    r"^\s*(?:#+\s*)?VERDICT\s*:\s*(APPROVED|NEEDS_REVISION)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Per-task line: ``TASK 2.1: VERIFIED|MISSING|DRIFTED``. Used for
# extracting drift findings out of the structured response.
_TASK_LINE_RE = re.compile(
    r"^\s*TASK\s+([\w.\-]+)\s*:\s*(VERIFIED|MISSING|DRIFTED)\b",
    re.IGNORECASE | re.MULTILINE,
)
# Best-effort capture for ``## DRIFT REPORT`` / ``## BASELINE DRIFT``
# blocks (everything between this header and the next ``## ``).
_DRIFT_REPORT_RE = re.compile(
    r"^\s*##\s+(?:DRIFT REPORT|BASELINE DRIFT)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _patch_similarity(prev_diff: str, curr_diff: str) -> float:
    """v0.34.0 B3: Jaccard similarity over the added/removed line set.

    Strips diff metadata (``diff --git``, ``index``, ``+++``, ``---``,
    ``@@`` hunk headers, blank lines) and the leading ``+``/``-``/`` ``
    marker so the comparison is over the actual edited content.
    Returns ``0.0`` when either input is empty.
    """
    if not prev_diff or not curr_diff:
        return 0.0
    prev = _diff_line_set(prev_diff)
    curr = _diff_line_set(curr_diff)
    if not prev or not curr:
        return 0.0
    inter = len(prev & curr)
    union = len(prev | curr)
    return inter / union if union else 0.0


def _diff_line_set(diff_text: str) -> set[str]:
    out: set[str] = set()
    for raw in diff_text.splitlines():
        if not raw:
            continue
        if raw.startswith(("diff --git", "index ", "+++ ", "--- ", "@@")):
            continue
        if raw[0] not in ("+", "-", " "):
            continue
        body = raw[1:].strip()
        if not body:
            continue
        out.add(body)
    return out


@dataclass
class DriftVerdict:
    """Structured outcome of one drift-verifier run.

    Attributes:
        passed: ``True`` iff the critic emitted a clean ``VERDICT:
            APPROVED`` and listed no MISSING/DRIFTED tasks. Skeptical
            default — unparseable responses are NOT passes.
        drift_findings: List of human-readable drift descriptions
            extracted from the response. Empty when ``passed=True``.
        evidence_path: On-disk path to the persisted evidence JSON.
        convergence_failure: v0.34.0 B3 — True when the runner exited
            early because the corrective patch was ≥90% identical to
            the prior patch. Drives escalation routing in the caller.
    """

    passed: bool
    drift_findings: list[str]
    evidence_path: Path
    convergence_failure: bool = False


def _build_drift_verify_prompt(
    phase: "Phase",
    diff_text: str,
) -> str:
    """Render the ``DRIFT_VERIFY_CONTEXT`` block + per-task acceptance list.

    Mirrors the v0.11.0 conflict-parser style: a single envelope-shaped
    text block the critic can read top-to-bottom. The ``DRIFT_VERIFY_CONTEXT``
    marker lets tests assert the wiring without coupling to the prompt's
    internal structure.
    """
    acceptance_lines = [
        f"  - {a.description}" for a in phase.acceptance
    ] or ["  (no acceptance criteria declared)"]
    task_lines = [
        f"  - {t.id}: {t.title} — {t.description}"
        for t in phase.tasks
    ] or ["  (no tasks)"]

    return (
        "DRIFT_VERIFY_CONTEXT:\n"
        f"PHASE: {phase.id} — {phase.title}\n"
        f"DESCRIPTION: {phase.description}\n"
        "\nTASKS:\n"
        + "\n".join(task_lines)
        + "\n\nACCEPTANCE:\n"
        + "\n".join(acceptance_lines)
        + "\n\nDIFF (as-implemented):\n"
        f"{diff_text or '(no diff)'}\n"
        "\nEND_DRIFT_VERIFY_CONTEXT\n"
        "\nVerify each task was implemented as specified. "
        "Emit a ``VERDICT:`` line per the standard prompt format."
    )


def _parse_drift_response(text: str) -> tuple[bool, list[str]]:
    """Extract ``(passed, findings)`` from a critic response.

    Skeptical default: missing ``VERDICT:`` line → ``passed=False`` with a
    fallback finding. APPROVED with MISSING/DRIFTED task lines also
    fails (the critic may approve the overall verdict by oversight while
    flagging specific tasks).
    """
    if not text or not text.strip():
        return False, ["drift_verifier: empty response"]

    verdict_match = _VERDICT_RE.search(text)
    findings: list[str] = []

    # Per-task drift findings (always collected — even on APPROVED
    # responses they may surface inconsistencies).
    for tm in _TASK_LINE_RE.finditer(text):
        task_id, status = tm.group(1), tm.group(2).upper()
        if status in ("MISSING", "DRIFTED"):
            findings.append(f"task {task_id}: {status}")

    # Capture the DRIFT REPORT / BASELINE DRIFT block bodies as a
    # single grouped finding so a human reading the evidence file can
    # see what the critic actually wrote. Lines whose value is
    # "none" / "(none)" / "n/a" are skipped — the prompt's standard
    # template uses these to mark explicit absence of drift, and we
    # don't want to surface them as findings on otherwise-clean runs.
    drift_block_lines: list[str] = []
    capture = False
    for line in text.splitlines():
        if _DRIFT_REPORT_RE.match(line):
            capture = True
            continue
        if capture:
            if line.strip().startswith("##"):
                # End of section.
                capture = False
                continue
            stripped = line.strip()
            if not stripped:
                continue
            # Heuristic: a "Header: none" / "Header: n/a" line carries no
            # drift signal — skip. Match the colon-separated value.
            colon_idx = stripped.find(":")
            if colon_idx >= 0:
                value = stripped[colon_idx + 1 :].strip().lower()
                if value in {"none", "(none)", "n/a", ""}:
                    continue
            drift_block_lines.append(stripped)
    if drift_block_lines:
        findings.append(
            "drift report: " + " | ".join(drift_block_lines[:8])
        )

    if verdict_match is None:
        # No structured verdict — skeptical fallback.
        findings.insert(
            0,
            "drift_verifier: response missing VERDICT line "
            "(skeptical fallback to failure)",
        )
        return False, findings

    verdict = verdict_match.group(1).upper()
    passed = (verdict == "APPROVED") and not findings
    return passed, findings


def _safe_phase_id(phase_id: str) -> str:
    """Sanitize a phase id for use as a filename."""
    return phase_id.replace("/", "_").replace(" ", "_")


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """Atomic write of a JSON payload (mirrors :mod:`tournament.state`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_CONVERGENCE_SIMILARITY_THRESHOLD = 0.90


async def run_drift_verifier(
    *,
    orch: object,
    phase: "Phase",
    evidence_dir: Path,
    diff_text: str = "",
    prior_corrective_diff: str | None = None,
    attempt: int = 0,
) -> DriftVerdict:
    """Dispatch the drift-verifier critic and return its parsed verdict.

    Args:
        orch: Orchestrator (or stub) exposing ``adapter``, ``cwd``, and
            a ``registry`` dict containing a ``critic_drift_verifier``
            :class:`AgentSpec`.
        phase: The phase being verified.
        evidence_dir: Directory under which ``{phase_id}-drift-verifier.json``
            is written.
        diff_text: As-implemented unified diff for the phase (rendered
            into the DRIFT_VERIFY_CONTEXT block). Empty string is fine —
            the critic will still receive the spec + acceptance criteria.

    Returns:
        :class:`DriftVerdict`. ``passed=True`` requires both an explicit
        ``VERDICT: APPROVED`` line AND zero MISSING/DRIFTED task entries.
    """
    # v0.34.0 B3: convergence exit BEFORE dispatching the agent. When
    # the corrective patch we're about to verify is ≥90% identical to
    # the prior patch, the corrective loop is not making progress and
    # we escalate via the caller's phase-review-decision path instead
    # of paying another critic dispatch.
    if prior_corrective_diff is not None and diff_text:
        sim = _patch_similarity(prior_corrective_diff, diff_text)
        if sim >= _CONVERGENCE_SIMILARITY_THRESHOLD:
            finding = (
                "drift_convergence_failure: corrective patch is "
                f"≥{int(_CONVERGENCE_SIMILARITY_THRESHOLD * 100)}% identical to "
                f"prior (similarity={sim:.2f}); escalating"
            )
            evidence_path = (
                evidence_dir / f"{_safe_phase_id(phase.id)}-drift-verifier.json"
            )
            _atomic_write_json(
                evidence_path,
                {
                    "phase_id": phase.id,
                    "passed": False,
                    "drift_findings": [finding],
                    "raw_response": "",
                    "convergence_failure": True,
                    "similarity": sim,
                    "attempt": attempt,
                },
            )
            logger.info(
                "drift_verifier.convergence_failure",
                phase_id=phase.id,
                similarity=sim,
                attempt=attempt,
            )
            return DriftVerdict(
                passed=False,
                drift_findings=[finding],
                evidence_path=evidence_path,
                convergence_failure=True,
            )

    spec = orch.registry.get("critic_drift_verifier")  # type: ignore[attr-defined]
    if spec is None:
        # Fail-safe: no agent spec → cannot run, mark as drifted with a
        # descriptive finding so the caller can surface a clean error.
        finding = (
            "drift_verifier: critic_drift_verifier agent not registered — "
            "cannot dispatch verification"
        )
        evidence_path = evidence_dir / f"{_safe_phase_id(phase.id)}-drift-verifier.json"
        _atomic_write_json(
            evidence_path,
            {
                "phase_id": phase.id,
                "passed": False,
                "drift_findings": [finding],
                "raw_response": "",
            },
        )
        return DriftVerdict(
            passed=False, drift_findings=[finding], evidence_path=evidence_path
        )

    prompt = _build_drift_verify_prompt(phase, diff_text)
    inv = AgentInvocation(
        role="critic_drift_verifier",
        prompt=prompt,
        cwd=orch.cwd,  # type: ignore[attr-defined]
        model=spec.model,
        allowed_tools=list(spec.tools) if spec.tools else None,
        max_turns=spec.max_turns or 3,
    )
    result = await orch.adapter.execute(inv)  # type: ignore[attr-defined]
    response_text = result.text or ""

    passed, findings = _parse_drift_response(response_text)

    evidence_path = evidence_dir / f"{_safe_phase_id(phase.id)}-drift-verifier.json"
    _atomic_write_json(
        evidence_path,
        {
            "phase_id": phase.id,
            "passed": passed,
            "drift_findings": findings,
            "raw_response": response_text,
        },
    )

    logger.info(
        "drift_verifier.complete",
        phase_id=phase.id,
        passed=passed,
        n_findings=len(findings),
        evidence_path=str(evidence_path),
    )

    return DriftVerdict(
        passed=passed, drift_findings=findings, evidence_path=evidence_path
    )


__all__ = [
    "DriftVerdict",
    "run_drift_verifier",
]
