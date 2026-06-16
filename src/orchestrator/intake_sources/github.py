"""GitHub gather source — pulls the canonical linked issue/PR (ADR-0045, FR2).

The #199 lesson: the linked issue often carries the full problem statement the
pasted summary dropped. This source detects ``#NNN`` references and GitHub
issue/PR URLs in the intent, then instructs the dispatched agent to fetch them
with ``gh issue view`` / ``gh pr view`` (Bash) or ``WebFetch`` (URLs). It honors
``cfg.exclude_globs`` — the dispatched agent is told NEVER to pull a PR/branch
matching an excluded pattern (the benchmark contamination guard, e.g. the
solution branch). The agent performs the I/O; this module only PREPARES the
instruction and PARSES the ``github``-sourced facts.

v0.42.1 (F3, gate-a): a thin ``bug.md`` often carries NO explicit ``#NNN`` ref,
so the canonical issue (#199 in Run-5) was never pulled. When no explicit ref is
present but ``gh`` is available, the source now AUTONOMOUSLY discovers it: it
derives the repo from the git remote, runs ``gh issue list`` scoped to that repo
with symptom keywords drawn from the intent, then a token-overlap MATCH GUARD
keeps only a confident match (a low-overlap result is discarded → degrade to
repo-only). The discovered ``#NNN`` then flows into ``prepare_prompt`` exactly
like an explicit ref.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from autologging import get_logger

from config.schema import IntakePhaseConfig
from state.schemas import GatheredFact

logger = get_logger()

# Bound the gh issue-list probe so a slow remote never stalls the gap path.
_GH_TIMEOUT_S = 20
# How many candidate issues to fetch for the match guard to score.
_GH_LIST_LIMIT = 5
# Minimum number of shared symptom keywords to treat a discovered issue as a
# confident match (below this the result is discarded → degrade to repo-only).
_MATCH_MIN_OVERLAP = 2


def _gh_available() -> bool:
    """Return whether the ``gh`` CLI is on PATH (no network — a ``shutil.which``
    probe, mirroring ``qa.env`` / ``cli.commands.doctor``).

    The agent fetches the canonical issue with ``gh issue view`` / ``gh pr view``;
    in a headless runner with no ``gh`` binary that dispatch is wasted (the agent
    can emit no ``github`` fact), so the source gates on the CLI being present.
    A module-level function so tests can monkeypatch BOTH branches without a
    network call. Never raises.
    """
    return shutil.which("gh") is not None

# ``#199`` / ``org/repo#199`` / ``GH-199`` shorthand references.
_ISSUE_REF_RE = re.compile(r"(?:\b[\w.-]+/[\w.-]+)?#(\d+)\b|\bGH-(\d+)\b")
# Full GitHub issue / PR URLs.
_URL_RE = re.compile(
    r"https?://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+", re.IGNORECASE
)
# owner/repo from an https or ssh GitHub remote URL (``.git`` suffix optional).
_REMOTE_SLUG_RE = re.compile(
    r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", re.IGNORECASE
)

# Common English + bug-report filler dropped from symptom-keyword extraction so
# the ``gh issue list`` search and the match guard key on signal, not stopwords.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "at", "for",
        "with", "by", "is", "are", "was", "were", "be", "been", "it", "its", "this",
        "that", "these", "those", "as", "from", "into", "out", "up", "down", "if",
        "then", "than", "when", "while", "we", "i", "you", "they", "he", "she",
        "fix", "fixes", "bug", "issue", "error", "problem", "run", "task", "see",
        "should", "would", "could", "can", "will", "not", "no", "do", "does",
    }
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}")


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


def _slug_from_remote_url(url: str) -> str | None:
    """Parse ``owner/repo`` from an https/ssh GitHub remote URL, else ``None``.

    Handles ``https://github.com/o/r.git``, ``https://github.com/o/r``,
    ``git@github.com:o/r.git``. Pure (no I/O); never raises.
    """
    m = _REMOTE_SLUG_RE.search(url.strip())
    return m.group(1) if m else None


def _git_remote_slug(cwd: Path) -> str | None:
    """Return the ``owner/repo`` of the ``origin`` remote, or ``None``.

    Shells ``git -C <cwd> remote get-url origin`` (the established subprocess
    pattern in :mod:`runtime.repo_probe`). MUST NOT raise — returns ``None`` on
    any error (no remote, no git, non-GitHub remote).
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return _slug_from_remote_url(out.stdout.strip())


def _symptom_keywords(intent: str, *, limit: int = 8) -> list[str]:
    """Extract de-duplicated, order-stable symptom keywords from ``intent``.

    Lowercases, drops stopwords and short tokens, keeps numeric error codes
    (e.g. ``429``). Pure; used both for the ``gh`` search and the match guard.
    """
    out: list[str] = []
    for tok in _TOKEN_RE.findall(intent.lower()):
        if tok in _STOPWORDS:
            continue
        if tok not in out:
            out.append(tok)
        if len(out) >= limit:
            break
    return out


def _gh_issue_list(slug: str, keywords: list[str]) -> list[dict]:
    """Run ``gh issue list`` scoped to ``slug``, searched by ``keywords``.

    Returns up to :data:`_GH_LIST_LIMIT` candidate issues as dicts with
    ``number``/``title``/``body`` keys. MUST NOT raise — returns ``[]`` on any
    error (no ``gh``, network failure, bad JSON, non-zero exit). The agent still
    does the rich fetch (``gh issue view``); this probe only DISCOVERS which
    issue to pull.
    """
    search = " ".join(keywords).strip()
    cmd = [
        "gh", "--repo", slug, "issue", "list",
        "--state", "all", "--limit", str(_GH_LIST_LIMIT),
        "--json", "number,title,body",
    ]
    if search:
        cmd += ["--search", search]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_GH_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    try:
        data = json.loads(out.stdout or "[]")
    except (json.JSONDecodeError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def _best_match(intent: str, issues: list[dict]) -> tuple[int, str] | None:
    """Pick the best symptom-overlapping issue, or ``None`` if none is confident.

    Scores each candidate by the size of the keyword intersection between the
    intent and the issue title+body. Returns ``(number, title)`` of the best
    match iff its overlap is >= :data:`_MATCH_MIN_OVERLAP`; otherwise ``None``
    (discard → degrade to repo-only). Pure; never raises.
    """
    intent_kw = set(_symptom_keywords(intent, limit=20))
    if not intent_kw:
        return None
    best: tuple[int, int, str] | None = None  # (overlap, number, title)
    for iss in issues:
        num = iss.get("number")
        if not isinstance(num, int):
            continue
        title = str(iss.get("title") or "")
        text = f"{title} {iss.get('body') or ''}"
        overlap = len(intent_kw & set(_symptom_keywords(text, limit=50)))
        if best is None or overlap > best[0]:
            best = (overlap, num, title)
    if best is None or best[0] < _MATCH_MIN_OVERLAP:
        return None
    return best[1], best[2]


class GitHubSource:
    """:class:`~orchestrator.intake_sources.GatherSource` over linked issues/PRs."""

    name = "github"

    def __init__(self) -> None:
        # Ref discovered autonomously in ``available()`` (no explicit intent ref)
        # so ``prepare_prompt`` can surface it. ``None`` on the explicit-ref path.
        self._discovered_ref: str | None = None

    async def available(
        self, *, cwd: Path, intent: str, cfg: IntakePhaseConfig
    ) -> bool:
        # Two-part gate (ADR-0045): a concrete issue/PR ref in the intent AND the
        # ``gh`` CLI on PATH. A ``#NNN`` with no ``gh`` binary (the Run-4 headless
        # reality) means the dispatched agent could not run ``gh issue view``
        # anyway, so we deactivate instead of spending a wasted fragment.
        self._discovered_ref = None
        short, urls = _references(intent)
        if short or urls:
            if not _gh_available():
                logger.info("intake.gather.github_skipped_no_gh", refs=short + urls)
                return False
            return True

        # v0.42.1 (F3): no explicit ref. AUTONOMOUSLY discover the canonical
        # issue so a thin ``bug.md`` (no ``#NNN``) still pulls it (the #199
        # Run-5 case). Requires ``gh`` + a GitHub remote to scope the search.
        if not _gh_available():
            logger.info("intake.gather.github_skipped_no_gh", refs=[])
            return False
        slug = _git_remote_slug(cwd)
        if not slug:
            logger.info("intake.gather.github_skipped_no_remote")
            return False
        keywords = _symptom_keywords(intent)
        issues = _gh_issue_list(slug, keywords)
        if not issues:
            logger.info("intake.gather.github_no_issues", slug=slug)
            return False
        match = _best_match(intent, issues)
        if match is None:
            # MATCH GUARD: a low-overlap candidate is the WRONG issue — discard
            # and degrade to repo-only rather than enrich from an unrelated issue.
            logger.info("intake.gather.github_no_confident_match", slug=slug)
            return False
        number, title = match
        self._discovered_ref = f"github:{slug}#{number}"
        logger.info(
            "intake.gather.github_autonomous_match",
            slug=slug,
            number=number,
            title=title[:120],
        )
        return True

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
        if not short and not urls and self._discovered_ref:
            # Autonomous-discovery path: the intent named no ref, so surface the
            # DISCOVERED canonical issue for the agent to pull (and normalize).
            ref = self._discovered_ref  # github:owner/repo#NNN
            number = ref.rsplit("#", 1)[-1]
            lines.append(
                "DISCOVERED ref (the intent named none, this issue was found by "
                f"symptom search): {ref} — run Bash `gh issue view {number}` and "
                f"normalize it to a `{ref}` fact."
            )
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
