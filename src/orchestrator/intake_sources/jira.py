"""Jira gather source — pulls a referenced Jira issue via MCP (ADR-0045, FR2).

Detects Jira issue keys (``PROJ-123``) in the intent and instructs the
dispatched agent to fetch them with the Jira MCP tools (e.g. ``jira_get_issue``).
The claude_code subprocess inherits the user's MCP config, so the tools are
available iff the user has the Jira MCP server configured — a HEADLESS caveat:
in a network-less / MCP-less run the agent simply finds the tool unavailable and
emits no ``jira`` fact, and :func:`gather_facts` degrades gracefully (the empty
parse is a no-op). This module only PREPARES the instruction and PARSES the
``jira``-sourced facts; it never calls the MCP itself.
"""

from __future__ import annotations

import re
from pathlib import Path

from autologging import get_logger

from config.schema import IntakePhaseConfig
from state.schemas import GatheredFact

logger = get_logger()

# Jira issue key: 1+ uppercase-alnum project prefix, dash, number. Anchored on a
# word boundary so it does not fire inside a path like ``ABC-123/foo``-style refs
# in code. Requires at least one letter in the prefix to avoid matching ``123-4``.
_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

# Common false positives sharing the ``WORD-NNN`` shape that are NOT Jira keys.
_DENY_PREFIXES = {"GH", "PR", "ADR", "RFC", "UTF", "ISO", "SHA", "CVE"}


def _jira_keys(intent: str) -> list[str]:
    """Return deduped, order-stable Jira keys in ``intent`` (denylist-filtered)."""
    keys: list[str] = []
    for m in _JIRA_KEY_RE.finditer(intent):
        key = m.group(1)
        prefix = key.split("-", 1)[0]
        if prefix in _DENY_PREFIXES:
            continue
        if key not in keys:
            keys.append(key)
    return keys


class JiraSource:
    """:class:`~orchestrator.intake_sources.GatherSource` over linked Jira issues."""

    name = "jira"

    async def available(
        self, *, cwd: Path, intent: str, cfg: IntakePhaseConfig
    ) -> bool:
        # Cheap key-presence probe only. Whether the MCP is reachable is decided
        # at dispatch time by the agent; an absent MCP degrades to no fact.
        return bool(_jira_keys(intent))

    async def prepare_prompt(
        self, *, cwd: Path, intent: str, cfg: IntakePhaseConfig
    ) -> str:
        keys = _jira_keys(intent)
        return (
            "The task references Jira issue(s). Fetch them with the Jira MCP\n"
            "tools (e.g. `jira_get_issue` with the issue key). Use source `jira`,\n"
            "ref = the issue key (e.g. PROJ-123). If the Jira MCP tools are NOT\n"
            "available in this environment, SKIP this source entirely and emit no\n"
            "`jira` fact — do not guess the issue contents.\n\n"
            "Keys: " + ", ".join(keys) + "\n"
        )

    def parse(self, response: str) -> list[GatheredFact]:
        from orchestrator.intake_sources import parse_facts_for

        return parse_facts_for(response, self.name)
