"""Agent registry: loads prompts + tool map + config into `AgentSpec`.

Public API:

- :func:`build_registry` — build a ``dict[str, AgentSpec]`` from an
  :class:`~config.schema.AutodevConfig`. Covers all 14 required roles.
- :func:`load_prompt` — read the raw markdown body for a role, stripping YAML
  frontmatter.
- :func:`render_prompt` — substitute ``{{KEY}}`` placeholders.

Tournament-role prompts (``critic_t``, ``author_b``, ``synthesizer``,
``judge``) come from :mod:`tournament.prompts`. If that module is
unavailable (Phase 5 not yet merged), an empty string is substituted with a
warning. This keeps Phase 3 independently testable.
"""

from __future__ import annotations

import logging
from pathlib import Path

from adapters.types import AgentSpec
from agents.tool_map import AGENT_TOOL_MAP, resolve_claude_tools
from config.schema import REQUIRED_AGENT_ROLES, AutodevConfig


_PROMPTS_DIR = Path(__file__).parent / "prompts"
_log = logging.getLogger("autodev.agents")


# Human-readable descriptions for tournament roles (they have no .md file —
# the prompt comes from tournament/prompts.py at render time).
_TOURNAMENT_DESCRIPTIONS: dict[str, str] = {
    "critic_t": "Tournament critic. Finds real problems without proposing fixes.",
    "architect_b": "Tournament revision author. Addresses criticisms in a new draft.",
    "synthesizer": "Tournament synthesizer. Merges strongest elements of two versions.",
    "judge": "Tournament judge. Ranks proposals by how well they accomplish the task.",
}


def load_prompt(role: str) -> str:
    """Load the raw prompt body for ``role``, stripping YAML frontmatter.

    Raises ``FileNotFoundError`` if the prompt file is missing.
    """
    path = _PROMPTS_DIR / f"{role}.md"
    text = path.read_text()
    return _strip_frontmatter(text)


def _strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block (``---\\n...\\n---\\n``).

    Returns the original text if no frontmatter is present.
    """
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    return text[end + len("\n---\n") :].lstrip("\n")


def load_description(role: str) -> str:
    """Read the ``description:`` field from a prompt file's frontmatter.

    Returns an empty string if the file or field is missing.
    """
    path = _PROMPTS_DIR / f"{role}.md"
    if not path.exists():
        return _TOURNAMENT_DESCRIPTIONS.get(role, "")
    text = path.read_text()
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---\n", 4)
    if end == -1:
        return ""
    for line in text[4:end].splitlines():
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return ""


def render_prompt(raw: str, ctx: dict[str, str]) -> str:
    """Replace ``{{KEY}}`` placeholders with ``ctx[KEY]``.

    Keys not present in ``ctx`` are left unchanged. A simple ``str.replace``
    loop keeps the renderer dependency-free (no Jinja2).
    """
    result = raw
    for key, value in ctx.items():
        result = result.replace(f"{{{{{key}}}}}", value)
    return result


def _tournament_prompt(role: str) -> str:
    """Return the tournament-role system prompt from ``tournament.prompts``.

    Falls back to an empty string with a warning if the module is absent
    (e.g., Phase 5 hasn't merged yet).
    """
    try:
        from tournament import prompts as tp  # local import on purpose
    except ImportError:
        _log.warning(
            "tournament.prompts not available; "
            "tournament role %s will render with empty prompt",
            role,
        )
        return ""

    mapping = {
        "critic_t": "CRITIC_SYSTEM",
        "architect_b": "ARCHITECT_B_SYSTEM",
        "synthesizer": "SYNTHESIZER_SYSTEM",
        "judge": "JUDGE_SYSTEM",
    }
    attr = mapping.get(role)
    if attr is None:
        return ""
    return getattr(tp, attr, "") or ""


def build_registry(cfg: AutodevConfig) -> dict[str, AgentSpec]:
    """Return an ``{role: AgentSpec}`` registry covering every required role.

    Raises ``ValueError`` if ``cfg.agents`` is missing any role in
    :data:`REQUIRED_AGENT_ROLES`.
    """
    cfg.require_all_roles()
    qa_retry_limit = str(cfg.qa_retry_limit)

    registry: dict[str, AgentSpec] = {}
    for role in REQUIRED_AGENT_ROLES:
        agent_cfg = cfg.agents[role]
        tools = resolve_claude_tools(role)
        tools_str = ", ".join(tools) if tools else "(none)"

        if role in _TOURNAMENT_DESCRIPTIONS:
            raw_prompt = _tournament_prompt(role)
            description = _TOURNAMENT_DESCRIPTIONS[role]
        else:
            raw_prompt = load_prompt(role)
            description = load_description(role) or f"{role} role"

        rendered = render_prompt(
            raw_prompt,
            {"QA_RETRY_LIMIT": qa_retry_limit, "TOOLS": tools_str},
        )

        registry[role] = AgentSpec(
            name=role,
            description=description,
            prompt=rendered,
            tools=tools,
            model=agent_cfg.model,
        )

    return registry


__all__ = [
    "AGENT_TOOL_MAP",
    "build_registry",
    "load_description",
    "load_prompt",
    "render_prompt",
    "resolve_claude_tools",
]
