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
                enabled=True,
                # Bumped 3 -> 5: autoreason finds 7 judges yield ~3x faster
                # convergence than 3. We pick 5 as a cost/quality middle ground.
                num_judges=5,
                convergence_k=2,
                max_rounds=15,
                # Runaway detector: terminate early when per-pass Borda scores
                # are stuck across several consecutive passes. Window=4 is
                # half of max_rounds; max_delta=1 only fires on a genuinely
                # flat trajectory (sum of |Δscore| across A/B/AB ≤ 1).
                score_stability_window=4,
                score_stability_max_delta=1,
            ),
            impl=TournamentPhaseConfig(
                enabled=True,
                # Impl tournament stays single-judge by convention — it's
                # structurally different (git worktree variants).
                num_judges=1,
                convergence_k=1,
                max_rounds=3,
                # max_rounds is small (3); window=2 still permits one normal
                # pass before the detector can latch onto a stuck Borda outcome.
                score_stability_window=2,
                score_stability_max_delta=1,
            ),
            max_parallel_subprocesses=3,
            auto_disable_for_models=["opus"],
        ),
        qa_gates=QAGatesConfig(),
        qa_retry_limit=3,
        user_complexity="medium",
        guardrails=GuardrailsConfig(),
        hive=HiveConfig(
            enabled=True,
            path=Path("~/.local/share/autodev/shared-learnings.jsonl"),
        ),
    )
