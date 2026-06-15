"""GitHub gather source — pulls the canonical linked issue/PR (ADR-0045, FR2).

The #199 lesson: the linked issue often carries the full problem statement the
pasted summary dropped. This source detects ``#NNN`` references and GitHub
issue/PR URLs in the intent, then instructs the dispatched agent to fetch them
with ``gh issue view`` / ``gh pr view`` (Bash) or ``WebFetch`` (URLs). It honors
``cfg.exclude_globs`` — the dispatched agent is told NEVER to pull a PR/branch
matching an excluded pattern (the benchmark contamination guard, e.g. the
solution branch). The agent performs the I/O; this module only PREPARES the
instruction and PARSES the ``github``-sourced facts.
"""

from __future__ import annotations

import re
from pathlib import Path

from autologging import get_logger

from config.schema import IntakePhaseConfig
from state.schemas import GatheredFact

logger = get_logger()

# ``#199`` / ``org/repo#199`` / ``GH-199`` shorthand references.
_ISSUE_REF_RE = re.compile(r"(?:\b[\w.-]+/[\w.-]+)?#(\d+)\b|\bGH-(\d+)\b")
# Full GitHub issue / PR URLs.
_URL_RE = re.compile(
    r"https?://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+", re.IGNORECASE
)


def _references(intent: str) -> tuple[list[str], list[str]]:
    """Return ``(short_refs, urls)`` found in ``intent`` (deduped, order-stable)."""
    short: list[str] = []
    for m in _ISSUE_REF_RE.finditer(intent):
        num = m.group(1) or m.group(2)
        ref = f"#{num}"
        if ref not in short:
            short.append(ref)
    urls: list[str] = []
    for m in _URL_RE.finditer(intent):
        u = m.group(0)
        if u not in urls:
            urls.append(u)
    return short, urls


class GitHubSource:
    """:class:`~orchestrator.intake_sources.GatherSource` over linked issues/PRs."""

    name = "github"

    async def available(
        self, *, cwd: Path, intent: str, cfg: IntakePhaseConfig
    ) -> bool:
        short, urls = _references(intent)
        return bool(short or urls)

    async def prepare_prompt(
        self, *, cwd: Path, intent: str, cfg: IntakePhaseConfig
    ) -> str:
        short, urls = _references(intent)
        lines = [
            "The task references GitHub issue(s)/PR(s). Pull the CANONICAL issue —",
            "it is often richer than the pasted summary. Use source `github`.",
            "",
            "For a `#NNN` short ref, run Bash: `gh issue view NNN` (fall back to",
            "`gh pr view NNN` if it is a PR). For a full URL, use WebFetch.",
            "Emit one fact per concrete claim, ref `github:org/repo#NNN` (or the URL).",
        ]
        if short:
            lines.append("Short refs: " + ", ".join(short))
        if urls:
            lines.append("URLs: " + ", ".join(urls))
        if cfg.exclude_globs:
            lines += [
                "",
                "EXCLUSION GUARD (mandatory): do NOT fetch, read, or summarize any",
                "PR, branch, diff, or file matching these globs — they are off-limits",
                "(e.g. the solution branch). If a referenced PR matches, SKIP it and",
                "emit no fact for it:",
                "  " + ", ".join(cfg.exclude_globs),
            ]
        return "\n".join(lines)

    def parse(self, response: str) -> list[GatheredFact]:
        from orchestrator.intake_sources import parse_facts_for

        return parse_facts_for(response, self.name)
