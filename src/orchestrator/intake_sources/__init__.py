"""Pluggable, agent-driven context-gather sources for the intake phase (ADR-0045).

The intake phase (`run_intake_phase`, owned by ``intake_phase.py``) calls
:func:`gather_facts` once, on the *gap path only*, to pull the facts a senior
engineer would read around a thin ticket before committing to a plan: the repo
(reusing the explorer pass), the canonical linked GitHub issue/PR, a referenced
Jira issue, and prior AutoDev sessions on the same files.

Design (ADR-0005 protocol-based plugins + the framing specialist-dispatch
pattern):

- :class:`GatherSource` is a ``Protocol`` — each module (:mod:`repo`,
  :mod:`github`, :mod:`jira`, :mod:`sessions`) implements it. A source is asked
  whether it is :meth:`~GatherSource.available` for a given intent/cwd, then
  contributes a :meth:`~GatherSource.prepare_prompt` fragment, then
  :meth:`~GatherSource.parse` is handed the agent's response to extract its own
  facts.
- **Gather is agent-driven.** Rather than each source shelling out itself, the
  ``intake_enricher`` role is dispatched ONCE (via the orchestrator's adapter)
  with the union of every available source's prompt fragment. The dispatched
  subprocess inherits the user's tool/MCP config, so it can run ``gh issue
  view`` (Bash), ``WebFetch`` a URL, or call the Jira MCP tools as instructed by
  the per-source fragments. The source modules only PREPARE the instructions and
  PARSE the structured facts the agent emits back.
- **Degrade gracefully.** :func:`gather_facts` never raises and never blocks: an
  unavailable source (no ``#NNN`` reference, no Jira key, no explorer evidence,
  Jira-MCP absent in a headless run) is silently skipped with a logged note; a
  failed dispatch yields an empty fact list. Intake must never block planning.

The single public entrypoint downstream code (``intake_phase.py``) imports and
calls is :func:`gather_facts`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from autologging import get_logger
from pydantic import ValidationError

from agents import load_prompt
from config.schema import IntakePhaseConfig
from state.schemas import GatheredFact, SpecGaps

if TYPE_CHECKING:
    from orchestrator import Orchestrator

logger = get_logger()

# The role dispatched to run the agent-driven gather (Bash/WebFetch/Jira-MCP).
# Reuses the enricher role's prompt + model config; the same role then enriches.
_GATHER_ROLE = "intake_enricher"

# Hard ceiling on facts threaded into the enriched spec — external sources are
# untrusted input (design doc §8 "bound sizes"). Excess is truncated, not raised.
_MAX_FACTS = 40

# Cap on a single fact's summary so a runaway agent line cannot bloat the spec.
_MAX_SUMMARY_CHARS = 600

_FACTS_BLOCK_RE = re.compile(r"```facts\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_facts_block(response: str) -> str | None:
    m = _FACTS_BLOCK_RE.search(response)
    return m.group(1) if m else None


def parse_facts_for(response: str, source_name: str) -> list[GatheredFact]:
    """Parse the agent's ```facts block, returning only ``source_name`` rows.

    Each row is ``<source> | <ref> | <summary>``. Malformed rows (wrong column
    count, empty ref, wrong/invalid source) are skipped — NEVER raises (the
    gather contract). Shared by every :class:`GatherSource.parse`.
    """
    block = _extract_facts_block(response)
    if block is None:
        return []
    out: list[GatheredFact] = []
    for line in block.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 3:
            continue
        source, ref, summary = parts
        if source.lower() != source_name:
            continue
        if not ref or not summary:
            continue
        try:
            out.append(
                GatheredFact(
                    source=source.lower(),  # type: ignore[arg-type]
                    ref=ref,
                    summary=summary[:_MAX_SUMMARY_CHARS],
                )
            )
        except ValidationError:
            continue
    return out


@runtime_checkable
class GatherSource(Protocol):
    """A pluggable intake gather adapter (ADR-0005 protocol-based plugin).

    Implementations live in :mod:`repo`, :mod:`github`, :mod:`jira`,
    :mod:`sessions`. They MUST be cheap, side-effect-free, and synchronous: the
    actual I/O (``gh``/``WebFetch``/Jira-MCP) is performed by the dispatched
    agent, not the source itself.
    """

    #: Stable source key — matches the ``GatheredFact.source`` Literal and the
    #: ``cfg.sources`` allowlist entry.
    name: str

    async def available(self, *, cwd: Path, intent: str, cfg: IntakePhaseConfig) -> bool:
        """Return whether this source has anything to gather for ``intent``.

        MUST NOT raise. A source with no referenced issue / no explorer evidence
        / no prior sessions returns ``False`` so it is skipped without a wasted
        agent turn.
        """
        ...

    async def prepare_prompt(
        self, *, cwd: Path, intent: str, cfg: IntakePhaseConfig
    ) -> str:
        """Return the instruction fragment for this source (assumes available).

        The fragment tells the dispatched agent exactly what to fetch and how
        (e.g. ``gh issue view 199``), honoring ``cfg.exclude_globs``.
        """
        ...

    def parse(self, response: str) -> list[GatheredFact]:
        """Extract THIS source's facts from the agent's response. MUST NOT raise."""
        ...


# ``cfg.sources`` uses the human-facing names; ``session`` is spelled
# ``"sessions"`` there (plural) but the ``GatheredFact.source`` Literal is the
# singular ``"session"``. Map the config spelling to the registry key.
_CFG_ALIAS: dict[str, str] = {"sessions": "session"}


def _build_registry() -> dict[str, GatherSource]:
    """Construct the source registry, keyed by the ``GatheredFact.source`` Literal.

    Imported lazily so the source modules (which import :func:`parse_facts_for`
    from this package) do not create a load-time import cycle. ``repo`` first so
    its cheap, always-safe facts lead the enriched spec.
    """
    from orchestrator.intake_sources import github, jira, repo, sessions

    return {
        "repo": repo.RepoSource(),
        "github": github.GitHubSource(),
        "jira": jira.JiraSource(),
        "session": sessions.SessionSource(),
    }


def _selected_sources(cfg: IntakePhaseConfig) -> list[GatherSource]:
    """Resolve ``cfg.sources`` to registered :class:`GatherSource` instances.

    Unknown source names are skipped with a logged note (never raise).
    """
    registry = _build_registry()
    out: list[GatherSource] = []
    for raw in cfg.sources:
        key = _CFG_ALIAS.get(raw, raw)
        src = registry.get(key)
        if src is None:
            logger.info("intake.gather.unknown_source", source=raw)
            continue
        out.append(src)
    return out


async def _dispatch_gather(orch: "Orchestrator", prompt: str) -> str:
    """Dispatch the agent-driven gather via the orchestrator's adapter.

    Mirrors ``framing_phase._invoke_framing_role``: specialist dispatch via the
    ``load_prompt`` path (NEVER ``_delegate`` — the role is not in the registry).
    Honors ``cfg.intake.enricher_model``. Returns ``""`` on any failure so the
    caller degrades to an empty fact list.
    """
    from adapters.types import AgentInvocation

    in_cfg = orch.cfg.intake
    agent_cfg = orch.cfg.agents[_GATHER_ROLE]
    raw_prompt = load_prompt(_GATHER_ROLE)
    full_prompt = "\n\n---\n".join([raw_prompt.strip(), prompt])
    inv = AgentInvocation(
        role=_GATHER_ROLE,
        prompt=full_prompt,
        cwd=orch.cwd,
        model=in_cfg.enricher_model or agent_cfg.model,
        max_turns=agent_cfg.max_turns or 1,
    )
    try:
        result = await orch.adapter.execute(inv)
    except Exception as exc:  # noqa: BLE001 - gather must never raise
        logger.warning("intake.gather.dispatch_failed", err=str(exc))
        return ""
    return result.text or ""


def _render_gather_prompt(fragments: list[tuple[str, str]], intent: str) -> str:
    """Assemble the union gather prompt from per-source fragments.

    ``fragments`` is ``[(source_name, fragment_text), ...]``. The shared header
    pins the output contract every source's :meth:`~GatherSource.parse` reads.
    """
    header = (
        "## INTAKE GATHER\n"
        "You are gathering context for an under-specified task. Use ONLY the\n"
        "tools and references named in each SOURCE block below. For each fact you\n"
        "find, emit one line inside a single fenced ```facts block, formatted\n"
        "EXACTLY as:\n\n"
        "    <source> | <ref> | <one-line summary>\n\n"
        "where <source> is one of repo|github|jira|session and <ref> is a\n"
        "concrete locator (file.py:120-134 | github:org/repo#199 | PROJ-123 |\n"
        "session-id). Do NOT invent facts; omit a source if its reference is\n"
        "unreachable. Do NOT include any fact whose ref you could not actually\n"
        "open.\n\n"
        f"### TASK INTENT\n{intent}\n"
    )
    blocks = [header]
    for name, frag in fragments:
        blocks.append(f"### SOURCE: {name}\n{frag}")
    blocks.append(
        "### OUTPUT\nEmit exactly one fenced block:\n\n"
        "```facts\nrepo | src/foo.py:10-20 | bar() drops the trailing token\n"
        "github | github:org/repo#199 | issue names three failure mechanisms\n"
        "```\n"
    )
    return "\n\n".join(blocks)


async def gather_facts(
    orch: "Orchestrator",
    *,
    cwd: Path,
    intent: str,
    gaps: SpecGaps,
    cfg: IntakePhaseConfig,
) -> list[GatheredFact]:
    """Gather provenance-carrying facts for an under-specified ``intent``.

    Selects sources per ``cfg.sources``, skips unavailable ones (no referenced
    issue / no explorer evidence / Jira-MCP absent in headless), dispatches the
    single agent-driven gather (Bash ``gh``, ``WebFetch``, Jira-MCP tools), and
    parses each source's facts from the response. DEGRADES GRACEFULLY: a source
    that is unavailable or unreachable is logged + skipped; a failed dispatch
    yields ``[]``. NEVER raises and NEVER blocks (intake must not block planning).

    ``gaps`` is accepted for symmetry with the phase FSM (and to let a future
    source scope its fragment to the missing dimensions) but the current sources
    gather unconditionally when available.
    """
    sources = _selected_sources(cfg)
    if not sources:
        logger.info("intake.gather.no_sources")
        return []

    # 1. Availability probe (cheap, never raises) + collect prompt fragments.
    fragments: list[tuple[str, str]] = []
    active: list[GatherSource] = []
    for src in sources:
        try:
            if not await src.available(cwd=cwd, intent=intent, cfg=cfg):
                logger.info("intake.gather.source_skipped", source=src.name)
                continue
            frag = await src.prepare_prompt(cwd=cwd, intent=intent, cfg=cfg)
        except Exception as exc:  # noqa: BLE001 - one bad source never sinks gather
            logger.warning(
                "intake.gather.source_prepare_failed", source=src.name, err=str(exc)
            )
            continue
        if frag.strip():
            fragments.append((src.name, frag))
            active.append(src)

    if not fragments:
        logger.info("intake.gather.nothing_available")
        return []

    # 2. Single agent-driven gather dispatch (the union of all fragments).
    prompt = _render_gather_prompt(fragments, intent)
    response = await _dispatch_gather(orch, prompt)
    if not response.strip():
        logger.info("intake.gather.empty_response")
        return []

    # 3. Each active source parses its OWN facts (never raises).
    facts: list[GatheredFact] = []
    for src in active:
        try:
            parsed = src.parse(response)
        except Exception as exc:  # noqa: BLE001 - one bad parser never sinks gather
            logger.warning(
                "intake.gather.source_parse_failed", source=src.name, err=str(exc)
            )
            continue
        facts.extend(parsed)

    if len(facts) > _MAX_FACTS:
        logger.info("intake.gather.truncated", total=len(facts), kept=_MAX_FACTS)
        facts = facts[:_MAX_FACTS]

    logger.info(
        "intake.gather.complete",
        n_facts=len(facts),
        sources=[s.name for s in active],
    )
    return facts


__all__ = [
    "GatherSource",
    "gather_facts",
    "parse_facts_for",
]
