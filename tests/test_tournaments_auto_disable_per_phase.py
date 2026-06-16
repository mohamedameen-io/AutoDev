"""v0.25.3 — tournaments must never be skipped by default.

Before v0.25.3, ``TournamentsConfig.auto_disable_for_models`` was a single
top-level setting that defaulted to ``["opus"]`` and was consulted
verbatim by all three tournament runners. Because Claude Code's default
model is Opus, the README's #1 discipline mechanism
("tournament-based self-refinement") was silently OFF in practice for
every default install — defeating AutoDev's stated goal of improving
the quality and consistency of AI-generated code.

v0.25.3 makes the policy explicit: tournaments run on every model
including Opus. The auto-disable list moves to each
:class:`TournamentPhaseConfig` so operators can still opt-out for a
specific tournament type (e.g. dev environments where cost is a real
constraint), but the built-in default for plan, impl, AND phase_review
is ``[]`` — no auto-disable, ever, unless the operator explicitly
configures it.

The deprecated top-level ``TournamentsConfig.auto_disable_for_models``
default flips to ``[]``. Legacy on-disk configs (v0.25.2 and earlier)
that still pin the top-level value continue to inherit it into every
per-tournament slot whose own value is ``None`` — until they are
refreshed via ``autodev init --force``.
"""

from __future__ import annotations

import pytest

from config.defaults import default_config
from config.schema import (
    AutodevConfig,
)


# ---------------------------------------------------------------------------
# Default policies (v0.25.3)
# ---------------------------------------------------------------------------


def test_default_config_plan_tournament_runs_on_opus() -> None:
    """Plan tournament runs by default on every model, including Opus."""
    cfg = default_config()
    assert cfg.tournaments.plan.auto_disable_for_models == []


def test_default_config_impl_tournament_runs_on_opus() -> None:
    """Impl tournament runs by default on every model, including Opus."""
    cfg = default_config()
    assert cfg.tournaments.impl.auto_disable_for_models == []


def test_default_config_phase_review_tournament_runs_on_opus() -> None:
    """Phase-review tournament runs by default on every model,
    including Opus."""
    cfg = default_config()
    assert cfg.tournaments.phase_review.auto_disable_for_models == []


def test_default_config_top_level_auto_disable_is_empty() -> None:
    """The deprecated top-level field defaults to ``[]`` — operators
    must explicitly set it to opt into the legacy global-disable
    behavior."""
    cfg = default_config()
    assert cfg.tournaments.auto_disable_for_models == []


# ---------------------------------------------------------------------------
# Back-compat: explicit top-level setting inherits to None-valued slots
# ---------------------------------------------------------------------------


def test_legacy_top_level_inherits_to_all_three_when_per_tournament_none(
    sample_config_dict,
) -> None:
    """Existing on-disk configs that pin
    ``tournaments.auto_disable_for_models: ["opus"]`` at the top level
    (written by v0.25.2 and earlier) continue to disable all three
    tournaments on Opus until refreshed.
    """
    raw = sample_config_dict()
    raw["tournaments"]["auto_disable_for_models"] = ["opus"]
    # Each per-tournament block omits ``auto_disable_for_models`` (None).
    cfg = AutodevConfig.model_validate(raw)
    assert cfg.tournaments.plan.auto_disable_for_models == ["opus"]
    assert cfg.tournaments.impl.auto_disable_for_models == ["opus"]
    assert cfg.tournaments.phase_review.auto_disable_for_models == ["opus"]


def test_explicit_per_tournament_value_wins_over_top_level(
    sample_config_dict,
) -> None:
    """When a per-tournament value is set explicitly, it overrides any
    top-level fallback — no silent inheritance."""
    raw = sample_config_dict()
    raw["tournaments"]["auto_disable_for_models"] = ["opus"]
    raw["tournaments"]["plan"]["auto_disable_for_models"] = []  # explicit override
    cfg = AutodevConfig.model_validate(raw)
    assert cfg.tournaments.plan.auto_disable_for_models == []
    # impl + phase_review still inherit from top-level.
    assert cfg.tournaments.impl.auto_disable_for_models == ["opus"]
    assert cfg.tournaments.phase_review.auto_disable_for_models == ["opus"]


def test_empty_top_level_falls_back_to_per_tournament_builtin_defaults(
    sample_config_dict,
) -> None:
    """Top-level explicitly empty + per-tournament None: every
    per-tournament default is ``[]`` (no auto-disable, ever)."""
    raw = sample_config_dict()
    raw["tournaments"]["auto_disable_for_models"] = []
    # Per-tournament unset.
    cfg = AutodevConfig.model_validate(raw)
    assert cfg.tournaments.plan.auto_disable_for_models == []
    assert cfg.tournaments.impl.auto_disable_for_models == []
    assert cfg.tournaments.phase_review.auto_disable_for_models == []


# ---------------------------------------------------------------------------
# Runner contract: each runner reads its own per-tournament field
# ---------------------------------------------------------------------------


def _runner_executable_source(module) -> str:
    """Return the module's source with docstrings stripped — comments and
    strings inside the docstring of the runner mention the field name in
    prose, which would falsely satisfy "does not read top-level" checks.
    """
    import ast
    import inspect

    src = inspect.getsource(module)
    tree = ast.parse(src)
    # Drop every Expr-Constant-str (module / function / class docstrings).
    chunks: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        chunks.append(ast.unparse(node) if hasattr(ast, "unparse") else "")
    return "\n".join(chunks)


def test_plan_tournament_runner_does_not_read_top_level_field() -> None:
    """Regression for v0.25.3: the plan runner must consult its own
    per-tournament list, not the deprecated top-level one. Reading the
    bare top-level field was the bug that silently disabled plan
    tournaments on every Claude Code install (default model: Opus)."""
    from orchestrator import plan_tournament_runner

    src = _runner_executable_source(plan_tournament_runner)
    # The bare top-level read (the bug). Must be absent.
    assert "orch.cfg.tournaments.auto_disable_for_models" not in src


def test_impl_tournament_runner_does_not_read_top_level_field() -> None:
    from orchestrator import impl_tournament_runner

    src = _runner_executable_source(impl_tournament_runner)
    assert "orch.cfg.tournaments.auto_disable_for_models" not in src


def test_phase_review_runner_does_not_read_top_level_field() -> None:
    from orchestrator import phase_review_runner

    src = _runner_executable_source(phase_review_runner)
    assert "orch.cfg.tournaments.auto_disable_for_models" not in src


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_config_dict():
    """Returns a callable that produces a minimal-valid raw dict for
    :class:`AutodevConfig.model_validate`, **without** per-tournament
    ``auto_disable_for_models`` fields set — simulating a legacy
    (v0.25.2 and earlier) on-disk config that only knew about the
    top-level field. Tests mutate and validate."""

    def _factory() -> dict:
        cfg = default_config()
        raw = cfg.model_dump(mode="python")
        # Strip per-tournament auto_disable so the legacy-inheritance
        # path actually runs in the validator (a real v0.25.2 on-disk
        # config wouldn't have these keys).
        for phase in ("plan", "impl", "phase_review"):
            raw["tournaments"][phase].pop("auto_disable_for_models", None)
        return raw

    return _factory
