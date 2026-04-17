"""Default `.autodev/config.json` content."""

from __future__ import annotations

from pathlib import Path

from config.schema import (
    AgentConfig,
    AutodevConfig,
    GuardrailsConfig,
    HiveConfig,
    QAGatesConfig,
    TournamentPhaseConfig,
    TournamentsConfig,
)


_AGENT_MODEL_DEFAULTS: dict[str, str | None] = {
    "architect": None,
    "explorer": None,
    "domain_expert": None,
    "developer": None,
    "reviewer": None,
    "test_engineer": None,
    "critic_sounding_board": None,
    "critic_drift_verifier": None,
    "docs": None,
    "designer": None,
    "critic_t": None,
    "architect_b": None,
    "synthesizer": None,
    "judge": None,
}

_AGENT_MAX_TURNS: dict[str, int] = {
    "architect": 5,
    "explorer": 3,
    "domain_expert": 3,
    "developer": 10,
    "reviewer": 3,
    "test_engineer": 5,
    "critic_sounding_board": 3,
    "critic_drift_verifier": 3,
    "docs": 3,
    "designer": 3,
    "critic_t": 1,
    "architect_b": 5,
    "synthesizer": 1,
    "judge": 1,
}


def resolve_model(model: str | None, role: str, platform: str) -> str:
    """Resolve model based on platform and role.

    Cursor:
    - architect/architect_b: opus (high reasoning, falls back to auto if rate limited)
    - reviewer/judge/critic_*/synthesizer/docs: sonnet (moderate reasoning)
    - explorer/developer/test_engineer: auto (auto-selects best model per-task)

    Claude Code: Uses aliases (opus/sonnet/haiku) that auto-resolve to latest.
    """
    if model is not None:
        return model

    if platform == "cursor":
        if role in ("architect", "architect_b"):
            return "opus"
        if role in (
            "reviewer",
            "judge",
            "critic_t",
            "synthesizer",
            "critic_drift_verifier",
            "docs",
            "designer",
            "domain_expert",
        ):
            return "sonnet"
        return "auto"

    if role == "architect":
        return "opus"
    if role == "explorer":
        return "haiku"
    return "sonnet"


def default_config(platform: str = "auto") -> AutodevConfig:
    """Return the shipped default configuration."""
    agents = {
        name: AgentConfig(
            model=resolve_model(model, name, platform),
            max_turns=_AGENT_MAX_TURNS.get(name, 1),
        )
        for name, model in _AGENT_MODEL_DEFAULTS.items()
    }
    return AutodevConfig(
        schema_version="1.0.0",
        platform="auto",
        agents=agents,
        tournaments=TournamentsConfig(
            plan=TournamentPhaseConfig(
                enabled=True, num_judges=3, convergence_k=2, max_rounds=15
            ),
            impl=TournamentPhaseConfig(
                enabled=True, num_judges=1, convergence_k=1, max_rounds=3
            ),
            max_parallel_subprocesses=3,
            auto_disable_for_models=["opus"],
        ),
        qa_gates=QAGatesConfig(),
        qa_retry_limit=3,
        guardrails=GuardrailsConfig(),
        hive=HiveConfig(
            enabled=True,
            path=Path("~/.local/share/autodev/shared-learnings.jsonl"),
        ),
    )
