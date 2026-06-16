"""Repo gather source — reuses the explorer pass (ADR-0045, KD5).

The repo source NEVER runs a second exploration. It re-reads the explorer's
``plan-explore`` evidence (``state.evidence.read_evidence``) and hands those
findings to the dispatched agent, asking it to distill the bug-relevant
call-path / contract facts into ``repo``-sourced :class:`GatheredFact`s carrying
``file.py:line`` refs. This is the cheap, always-safe, no-network source — it is
available iff explorer evidence exists and ``cfg.reuse_explorer_evidence`` is on.
"""

from __future__ import annotations

from pathlib import Path

from autologging import get_logger

from config.schema import IntakePhaseConfig
from state.evidence import read_evidence
from state.schemas import ExploreEvidence, GatheredFact

logger = get_logger()

# Bound the explorer findings spliced into the prompt (untrusted-input ceiling).
_MAX_FINDINGS_CHARS = 8000


class RepoSource:
    """:class:`~orchestrator.intake_sources.GatherSource` over explorer evidence."""

    name = "repo"

    async def _explore_evidence(self, cwd: Path) -> ExploreEvidence | None:
        ev = await read_evidence(cwd, "plan-explore", "explore")
        return ev if isinstance(ev, ExploreEvidence) else None

    async def available(
        self, *, cwd: Path, intent: str, cfg: IntakePhaseConfig
    ) -> bool:
        """Available iff explorer evidence with non-empty findings exists and
        ``cfg.reuse_explorer_evidence`` is on. NEVER raises.

        The repo source is network-free and the highest-value enrichment, so a
        skip is logged with a STRUCTURED reason (``reuse_disabled`` |
        ``no_evidence`` | ``empty_findings``) — a Run-5-style silent ``n_facts=0``
        must be auditable. ``reuse_explorer_evidence`` defaults ``True`` for any
        config (legacy or fresh) that does not explicitly set it ``false``.
        """
        if not cfg.reuse_explorer_evidence:
            logger.info("intake.gather.repo_skipped", reason="reuse_disabled")
            return False
        ev = await self._explore_evidence(cwd)
        if ev is None:
            logger.info("intake.gather.repo_skipped", reason="no_evidence")
            return False
        if not ev.findings.strip():
            # Explorer ran but produced no usable findings (e.g. rate-limited →
            # empty .text written as ExploreEvidence.findings at plan_phase.py).
            logger.info("intake.gather.repo_skipped", reason="empty_findings")
            return False
        return True

    async def prepare_prompt(
        self, *, cwd: Path, intent: str, cfg: IntakePhaseConfig
    ) -> str:
        ev = await self._explore_evidence(cwd)
        findings = (ev.findings if ev else "").strip()[:_MAX_FINDINGS_CHARS]
        refs = ", ".join((ev.files_referenced if ev else [])[:20]) or "(none recorded)"
        return (
            "Repo context has ALREADY been gathered by the explorer — do NOT\n"
            "re-explore or read more files. Distill the findings below into the\n"
            "few facts that bear on the task, each with a `file.py:line` ref drawn\n"
            "from the findings. Use source `repo`.\n\n"
            f"Files referenced by the explorer: {refs}\n\n"
            "EXPLORER FINDINGS:\n"
            f"{findings}\n"
        )

    def parse(self, response: str) -> list[GatheredFact]:
        from orchestrator.intake_sources import parse_facts_for

        return parse_facts_for(response, self.name)
