"""Pydantic v2 schema for `.autodev/config.json`."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


REQUIRED_AGENT_ROLES: tuple[str, ...] = (
    "architect",
    "explorer",
    "domain_expert",
    "developer",
    "reviewer",
    "test_engineer",
    "critic_sounding_board",
    "critic_drift_verifier",
    "docs",
    "designer",
    "critic_t",
    "architect_b",
    "synthesizer",
    "judge",
)


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    disabled: bool = False
    max_turns: int | None = None  # None = use role default


class TournamentPhaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    num_judges: int
    convergence_k: int
    max_rounds: int


class TournamentsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: TournamentPhaseConfig
    impl: TournamentPhaseConfig
    max_parallel_subprocesses: int = 3
    auto_disable_for_models: list[str] = Field(default_factory=lambda: ["opus"])


class QAGatesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    syntax_check: bool = True
    lint: bool = True
    build_check: bool = True
    test_runner: bool = True
    secretscan: bool = True
    # These two fields are NOT dispatched by _run_qa_gates. They are consumed
    # exclusively by agent prompts (e.g. architect.md) to drive security-tier
    # routing decisions at planning time. Dispatching them as actual gates is
    # planned in ADR-008 (see line 104 of this file).
    sast_scan: bool = False
    mutation_test: bool = False


class GuardrailsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_tool_calls_per_task: int = 60
    max_duration_s_per_task: int = 900
    max_diff_bytes: int = 5_242_880
    cost_budget_usd_per_plan: float | None = None


class HiveConfig(BaseModel):
    """File-level settings for the hive (cross-project) knowledge tier.

    Governs the *on-disk* location and a master switch. Behavioral tuning
    (ranking, dedup, denylist, etc.) lives on :class:`KnowledgeConfig`.

    ``HiveConfig.enabled`` is the master switch for the hive file itself
    (write + read of ``shared-learnings.jsonl``). The knowledge store also
    honors :attr:`KnowledgeConfig.hive_enabled` for symmetry with the
    swarm-level toggle; both must be true for hive I/O to occur.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    path: Path


class KnowledgeConfig(BaseModel):
    """Behavioral config for the two-tier knowledge system (Phase 9).

    Separate from :class:`HiveConfig` — the latter holds path + master switch;
    this model holds dedup thresholds, ranking toggles, capacity caps, and
    injection policy.

    Hive enablement resolution: ``HiveConfig.enabled and KnowledgeConfig.hive_enabled``.
    Keeping both lets operators disable the hive file entirely (HiveConfig.enabled=false)
    OR leave the file in place but skip hive reads/writes for a particular project
    (KnowledgeConfig.hive_enabled=false).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    swarm_max_entries: int = 100
    hive_max_entries: int = 200
    dedup_threshold: float = 0.6
    max_inject_count: int = 5
    hive_enabled: bool = True
    promotion_min_confirmations: int = 3
    promotion_min_confidence: float = 0.7
    denylist_roles: list[str] = Field(
        default_factory=lambda: [
            "explorer",
            "judge",
            "critic_t",
            "architect_b",
            "synthesizer",
        ]
    )


class AutodevConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    platform: Literal["claude_code", "cursor", "inline", "auto"] = "auto"
    agents: dict[str, AgentConfig]
    tournaments: TournamentsConfig
    qa_gates: QAGatesConfig = Field(default_factory=QAGatesConfig)
    qa_retry_limit: int = 3
    guardrails: GuardrailsConfig = Field(default_factory=GuardrailsConfig)
    hive: HiveConfig
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)

    def require_all_roles(self) -> None:
        """Raise ValueError if any required role is missing from `agents`."""
        missing = [r for r in REQUIRED_AGENT_ROLES if r not in self.agents]
        if missing:
            raise ValueError(f"missing required agent roles: {missing}")
