"""Tests for :mod:`config.defaults` -- resolve_model and default_config."""

from __future__ import annotations

from config.defaults import default_config, resolve_model


# ---------------------------------------------------------------------------
# resolve_model: explicit model always wins
# ---------------------------------------------------------------------------


def test_resolve_model_explicit_model_returned() -> None:
    """When an explicit model is provided it is returned unchanged."""
    assert resolve_model("gpt-4", role="architect", platform="cursor") == "gpt-4"


# ---------------------------------------------------------------------------
# resolve_model: Cursor platform mapping
# ---------------------------------------------------------------------------


def test_resolve_model_cursor_architect() -> None:
    assert resolve_model(None, role="architect", platform="cursor") == "opus"


def test_resolve_model_cursor_architect_b() -> None:
    assert resolve_model(None, role="architect_b", platform="cursor") == "opus"


def test_resolve_model_cursor_reviewer() -> None:
    assert resolve_model(None, role="reviewer", platform="cursor") == "sonnet"


def test_resolve_model_cursor_judge() -> None:
    assert resolve_model(None, role="judge", platform="cursor") == "sonnet"


def test_resolve_model_cursor_developer() -> None:
    """Cursor developer falls through to the catch-all 'auto'."""
    assert resolve_model(None, role="developer", platform="cursor") == "auto"


def test_resolve_model_cursor_explorer() -> None:
    """Cursor explorer falls through to the catch-all 'auto'."""
    assert resolve_model(None, role="explorer", platform="cursor") == "auto"


# ---------------------------------------------------------------------------
# resolve_model: Claude Code (platform="auto") mapping
# ---------------------------------------------------------------------------


def test_resolve_model_claude_architect() -> None:
    assert resolve_model(None, role="architect", platform="auto") == "opus"


def test_resolve_model_claude_explorer() -> None:
    assert resolve_model(None, role="explorer", platform="auto") == "haiku"


def test_resolve_model_claude_developer() -> None:
    """Non-architect, non-explorer roles default to sonnet on auto platform."""
    assert resolve_model(None, role="developer", platform="auto") == "sonnet"


# ---------------------------------------------------------------------------
# default_config: integration with resolve_model
# ---------------------------------------------------------------------------


def test_default_config_sets_max_turns_per_role() -> None:
    cfg = default_config()
    assert cfg.agents["architect"].max_turns == 5
    assert cfg.agents["developer"].max_turns == 10
    assert cfg.agents["judge"].max_turns == 1
    assert cfg.agents["explorer"].max_turns == 3


def test_default_config_cursor_platform() -> None:
    """default_config(platform='cursor') should use cursor-specific model resolution."""
    cfg = default_config(platform="cursor")
    # Architect roles get opus on cursor
    assert cfg.agents["architect"].model == "opus"
    assert cfg.agents["architect_b"].model == "opus"
    # Reviewer/judge roles get sonnet on cursor
    assert cfg.agents["reviewer"].model == "sonnet"
    assert cfg.agents["judge"].model == "sonnet"
    # Developer/explorer get auto on cursor
    assert cfg.agents["developer"].model == "auto"
    assert cfg.agents["explorer"].model == "auto"


# ---------------------------------------------------------------------------
# default_config: tournament defaults wired up with safety nets enabled
# ---------------------------------------------------------------------------


def test_default_plan_tournament_uses_five_judges() -> None:
    """Plan tournament defaults bumped 3 → 5 judges for faster convergence
    (autoreason: 7 judges = 3× faster vs 3; we pick 5 as middle ground).
    """
    cfg = default_config()
    assert cfg.tournaments.plan.num_judges == 5


def test_default_impl_tournament_keeps_single_judge() -> None:
    """Impl tournament stays at num_judges=1 — it's structurally different
    (uses git worktree variants and is always-on, single-judge by convention).
    """
    cfg = default_config()
    assert cfg.tournaments.impl.num_judges == 1


def test_default_plan_tournament_has_score_stability_enabled() -> None:
    """Plan tournament ships with the runaway detector enabled by default.

    window=4 (half of max_rounds=15). v0.6.0 bumped ``max_delta`` 1→2 so
    the detector fires on the QNX historical trajectory at pass 5; the
    sub-test ``test_default_plan_tournament_score_stability_max_delta_bumped_to_2``
    is the empirical anchor for the bumped value.
    """
    cfg = default_config()
    assert cfg.tournaments.plan.score_stability_window == 4
    # v0.6.0 bump: was 1, now 2.
    assert cfg.tournaments.plan.score_stability_max_delta == 2


def test_default_impl_tournament_has_score_stability_enabled() -> None:
    """Impl tournament also ships with the runaway detector enabled.

    max_rounds is small (3) so window=2 still allows one normal pass before
    the detector can fire.
    """
    cfg = default_config()
    assert cfg.tournaments.impl.score_stability_window == 2
    assert cfg.tournaments.impl.score_stability_max_delta == 1


def test_default_plan_tournament_max_rounds_unchanged() -> None:
    """max_rounds stays at 15 — only num_judges and stability fields move."""
    cfg = default_config()
    assert cfg.tournaments.plan.convergence_k == 2
    assert cfg.tournaments.plan.max_rounds == 15


def test_default_impl_tournament_max_rounds_unchanged() -> None:
    cfg = default_config()
    assert cfg.tournaments.impl.convergence_k == 1
    assert cfg.tournaments.impl.max_rounds == 3


# ---------------------------------------------------------------------------
# default_config: user_complexity (test-time-compute bucket)
# ---------------------------------------------------------------------------


def test_default_user_complexity_is_medium() -> None:
    """``default_config()`` ships ``user_complexity="medium"`` — the
    middle-bucket default that AutoDev uses when the user has not flagged
    a different complexity tier on the CLI or in their config.
    """
    assert default_config().user_complexity == "medium"


# ---------------------------------------------------------------------------
# default_config: v0.6.0 — bumped score_stability_max_delta + winner_stability
# ---------------------------------------------------------------------------


def test_default_plan_tournament_score_stability_max_delta_bumped_to_2() -> None:
    """Plan ``score_stability_max_delta`` bumped 1→2 to fire on the QNX
    historical trajectory ``[(5,10,15)*3, (5,12,13), (5,11,14)]`` at pass 5.
    """
    cfg = default_config()
    assert cfg.tournaments.plan.score_stability_max_delta == 2


def test_default_impl_tournament_score_stability_max_delta_unchanged() -> None:
    """Impl ``score_stability_max_delta`` stays at 1 (max_rounds=3 makes 2
    unsafe — bumping would trip on virtually any normal trajectory).
    """
    cfg = default_config()
    assert cfg.tournaments.impl.score_stability_max_delta == 1


def test_default_plan_tournament_has_winner_stability_window_3() -> None:
    """Plan ships with ``winner_stability_window=3`` — terminates if 3
    consecutive passes share the same non-A effective winner.
    """
    cfg = default_config()
    assert cfg.tournaments.plan.winner_stability_window == 3


def test_default_impl_tournament_has_winner_stability_window_2() -> None:
    """Impl ships with ``winner_stability_window=2`` — max_rounds=3 means
    the smaller window still permits one normal pass before the detector fires.
    """
    cfg = default_config()
    assert cfg.tournaments.impl.winner_stability_window == 2


# Round-trip the schema (new field must validate on existing-shape configs).
def test_tournament_phase_config_winner_stability_window_round_trip() -> None:
    """The v0.6.0 ``winner_stability_window`` field round-trips through
    ``model_dump`` + ``model_validate`` cleanly. Default is ``None``.
    """
    from config.schema import TournamentPhaseConfig

    base = TournamentPhaseConfig(
        enabled=True,
        num_judges=5,
        convergence_k=2,
        max_rounds=15,
    )
    assert base.winner_stability_window is None

    with_win = TournamentPhaseConfig(
        enabled=True,
        num_judges=5,
        convergence_k=2,
        max_rounds=15,
        winner_stability_window=3,
    )
    assert with_win.winner_stability_window == 3
    assert (
        TournamentPhaseConfig.model_validate(with_win.model_dump()).winner_stability_window
        == 3
    )


# ---------------------------------------------------------------------------
# default_config: v0.6.2 — max_plan_lines_growth_ratio (oversize-AB demotion)
# ---------------------------------------------------------------------------


def test_default_plan_tournament_max_plan_lines_growth_ratio_is_1_5() -> None:
    """Plan ships ``max_plan_lines_growth_ratio=1.5`` so an AB winner that
    more than 1.5×s the incumbent line count is demoted to the next-best
    Borda winner. Combined with v0.6.2's stronger judge directive this
    closes the 'verbose synthesizer wins forever' failure mode.
    """
    cfg = default_config()
    assert cfg.tournaments.plan.max_plan_lines_growth_ratio == 1.5


def test_default_impl_tournament_max_plan_lines_growth_ratio_is_none() -> None:
    """Impl tournaments operate on diff bundles, not line-counted plan
    markdown — the line-ratio knob is meaningless there. Default is ``None``
    (feature off) for impl.
    """
    cfg = default_config()
    assert cfg.tournaments.impl.max_plan_lines_growth_ratio is None


def test_tournament_phase_config_max_plan_lines_growth_ratio_round_trip() -> None:
    """The v0.6.2 ``max_plan_lines_growth_ratio`` field round-trips through
    ``model_dump`` + ``model_validate`` cleanly. Default is ``None``.
    """
    from config.schema import TournamentPhaseConfig

    base = TournamentPhaseConfig(
        enabled=True,
        num_judges=5,
        convergence_k=2,
        max_rounds=15,
    )
    assert base.max_plan_lines_growth_ratio is None

    with_ratio = TournamentPhaseConfig(
        enabled=True,
        num_judges=5,
        convergence_k=2,
        max_rounds=15,
        max_plan_lines_growth_ratio=1.5,
    )
    assert with_ratio.max_plan_lines_growth_ratio == 1.5
    reloaded = TournamentPhaseConfig.model_validate(with_ratio.model_dump())
    assert reloaded.max_plan_lines_growth_ratio == 1.5
