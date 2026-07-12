"""WS-5 (I-3): architect_b turn budget must fund its new Read+Bash workload.

WS-5 grants the plan critic ``architect_b`` Read + Bash so it can run a
reproduction and empirically falsify a suspect bug-fix acceptance oracle. But
it must now run that reproduction AND emit the revised proposal within a single
dispatch. ``error_max_turns`` is a *deterministic* subtype (no retry), so at the
old 5-turn floor the pass aborts and the plan-phase salvage path recovers the
UN-refined incumbent — i.e. WS-5 silently fails on exactly the
bug-fix-suspect-oracle task it targets, worst on huge repos.

Fix (mirrors WS-2a):
  * base floor 5 -> 8 in ``config.defaults`` (parity with the read-heavy
    reviewer's 8; strictly below test_engineer's write+run 12);
  * a role-keyed ``huge_repo_multipliers`` entry so huge repos get proportional
    headroom (8 x 2.0 = 16) out-of-the-box, resolved by the same
    ``resolve_huge_repo_value`` resolver the H5 knobs use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.defaults import _AGENT_MAX_TURNS, default_config
from config.schema import TaskOverridesConfig


# ---------------------------------------------------------------------------
# Raised base floor (defaults.py).
# ---------------------------------------------------------------------------


def test_architect_b_floor_raised_to_8() -> None:
    """``architect_b`` 5 -> 8 — funds the run-a-reproduction + emit-revision
    workload the Read+Bash grant introduces."""
    assert _AGENT_MAX_TURNS["architect_b"] == 8
    assert default_config().agents["architect_b"].max_turns == 8


def test_architect_b_floor_matches_reviewer_below_test_engineer() -> None:
    """Workload-ordering pin: architect_b (read+exec critic) sits at the
    read-heavy reviewer floor (8) and strictly below the write+run
    test_engineer (12)."""
    assert _AGENT_MAX_TURNS["architect_b"] == _AGENT_MAX_TURNS["reviewer"]
    assert _AGENT_MAX_TURNS["architect_b"] < _AGENT_MAX_TURNS["test_engineer"]


# ---------------------------------------------------------------------------
# Proportional huge-repo headroom (schema.py huge_repo_multipliers).
# ---------------------------------------------------------------------------


def test_architect_b_huge_multiplier_present() -> None:
    """architect_b carries a role-keyed huge-repo multiplier so Unity-class
    repos get proportional headroom without config tweaks (project principle)."""
    mults = TaskOverridesConfig().huge_repo_multipliers
    assert mults["architect_b"] == 2.0
    # Parity with the other exec-workload role keys.
    assert mults["architect_b"] == mults["developer"]


class _FakeAutodevCfg:
    """Minimal duck-typed config for the huge-repo resolver."""

    def __init__(self, *, multipliers: dict[str, float] | None = None) -> None:
        self.huge_repo_overrides_disabled = False
        self.index_full_rebuild_threshold_files = 5000
        if multipliers is None:
            multipliers = dict(TaskOverridesConfig().huge_repo_multipliers)
        self.task_overrides = type(
            "_TO", (), {"huge_repo_multipliers": multipliers}
        )()


@pytest.fixture(autouse=True)
def _clear_repo_size_cache() -> None:
    from orchestrator.repo_size import clear_cache

    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def _force_huge_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    import orchestrator.repo_size as size_mod

    def _force_true(cwd: Path, threshold: int | None = None, *, cfg: Any = None) -> bool:  # noqa: ARG001
        if cfg is not None and getattr(cfg, "huge_repo_overrides_disabled", False):
            return False
        return True

    monkeypatch.setattr(size_mod, "is_huge_repo", _force_true)


def test_resolver_scales_architect_b_floor_on_huge_repo(
    _force_huge_repo: None, tmp_path: Path
) -> None:
    """On a huge repo the raised architect_b floor (8) scales 2.0x -> 16 via the
    same ``huge_repo_multipliers`` resolver pattern WS-2a uses for its floors."""
    from orchestrator.huge_repo_overrides import resolve_huge_repo_value

    cfg = _FakeAutodevCfg()
    eff, mult = resolve_huge_repo_value(
        key="architect_b",
        base_value=float(_AGENT_MAX_TURNS["architect_b"]),
        cwd=tmp_path,
        cfg=cfg,
    )
    assert mult == 2.0
    assert int(round(eff)) == 16
