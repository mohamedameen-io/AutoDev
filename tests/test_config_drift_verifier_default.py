"""v0.17.0 S1: ``drift_verifier_enabled`` default flips to True.

v0.16.0 wired the drift-verifier gate but kept the default OFF so a
missing role spec / unstubbed adapter wouldn't surprise legacy callers.
With ``tests/stub_adapter.py`` now returning a parser-compatible verdict
by default, we can flip the production default to True for the
phase-review tournament where drift-checking matters most.
"""

from __future__ import annotations

from config.defaults import default_config
from config.schema import AutodevConfig, TournamentPhaseConfig


def test_default_drift_verifier_enabled_true_for_phase_review() -> None:
    cfg = default_config()
    assert cfg.tournaments.phase_review.drift_verifier_enabled is True


def test_explicit_phase_config_default_drift_verifier_true() -> None:
    """An explicitly-constructed phase config also defaults ON."""
    phase = TournamentPhaseConfig(
        enabled=True,
        num_judges=3,
        convergence_k=2,
        max_rounds=4,
    )
    assert phase.drift_verifier_enabled is True


def test_users_can_disable_via_config() -> None:
    """Override path still works (opt-out)."""
    cfg = AutodevConfig(
        **{
            **default_config().model_dump(),
            "tournaments": {
                **default_config().tournaments.model_dump(),
                "phase_review": {
                    **default_config().tournaments.phase_review.model_dump(),
                    "drift_verifier_enabled": False,
                },
            },
        }
    )
    assert cfg.tournaments.phase_review.drift_verifier_enabled is False
