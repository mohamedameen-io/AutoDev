"""WS-2a: raised turn-budget floors + proportional huge-repo headroom.

Forensic finding (slice4, 2026-07-12): turn-budget exhaustion is the dominant
defect across a 10-instance benchmark (10/10 instances; 68 ``error_max_turns``
all cut off mid-tool-call; only 4/23 test runs ran clean). The
``test_engineer`` (mandated to write AND run tests) was crippled 9/10 at a
fixed 8-turn budget; the ``developer`` floor of 10 was cited as a root cause
(flask-4045: the fix was never written).

The budget-escalation ladder (:mod:`orchestrator.budget_escalation`) already
escalates a role that exhausts ``error_max_turns`` by 1.5× (attempt 1) then
2.0× (attempt 2). Since attempt-0 exhaustion is now the *dominant* defect (not
an occasional one), WS-2a promotes the ladder's first (1.5×) rung to be the
new floor:

* ``test_engineer`` 8 → 12 (= ceil(8 × 1.5))
* ``developer``     10 → 15 (= ceil(10 × 1.5))

...leaving the ladder's remaining rungs as retry headroom. Huge repos get
proportional headroom via the role-keyed ``huge_repo_multipliers``: the
heaviest write+run role (``test_engineer``) is bumped to 2.0× (parity with the
``developer`` key) so it scales at least as much as the lighter read+verdict
roles on Unity-class repos (project principle: huge repos work out-of-the-box
with zero config tweaks).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.defaults import _AGENT_MAX_TURNS, default_config
from config.schema import TaskOverridesConfig


# ---------------------------------------------------------------------------
# Raised base floors (defaults.py).
# ---------------------------------------------------------------------------


def test_test_engineer_floor_raised_to_12() -> None:
    """``test_engineer`` 8 → 12 — the escalation ladder's attempt-1 (1.5×)
    rung promoted to the floor. 8 was structurally insufficient for the
    write+run+iterate workload (forensic: crippled 9/10)."""
    assert _AGENT_MAX_TURNS["test_engineer"] == 12
    assert default_config().agents["test_engineer"].max_turns == 12


def test_developer_floor_raised_to_15() -> None:
    """``developer`` 10 → 15 — same escalation-ladder-first-rung rationale.
    This is the spec/fallback base (untagged tasks); tagged tasks continue
    to use the complexity table in ``tournament/task_overrides.py``."""
    assert _AGENT_MAX_TURNS["developer"] == 15
    assert default_config().agents["developer"].max_turns == 15


def test_test_engineer_floor_strictly_exceeds_reviewer() -> None:
    """The test_engineer additionally writes+runs tests, so its floor must
    strictly exceed the read-only reviewer's (8) — a workload-ordering pin."""
    assert _AGENT_MAX_TURNS["test_engineer"] > _AGENT_MAX_TURNS["reviewer"]


# ---------------------------------------------------------------------------
# Proportional huge-repo headroom (schema.py huge_repo_multipliers).
# ---------------------------------------------------------------------------


def test_role_multipliers_present_for_raised_budgets() -> None:
    """Both raised-budget roles carry a role-keyed huge-repo multiplier so
    huge repos get proportional headroom out-of-the-box."""
    mults = TaskOverridesConfig().huge_repo_multipliers
    assert mults["test_engineer"] == 2.0
    assert mults["developer"] == 2.0


def test_test_engineer_huge_multiplier_at_least_reviewer() -> None:
    """The heaviest write+run role must scale at least as much on huge repos
    as the lighter read+verdict roles (reviewer/domain_expert)."""
    mults = TaskOverridesConfig().huge_repo_multipliers
    assert mults["test_engineer"] >= mults["domain_expert"]
    # Parity with the developer key (both are heavy write-workload roles).
    assert mults["test_engineer"] == mults["developer"]


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


def test_resolver_scales_test_engineer_floor_on_huge_repo(
    _force_huge_repo: None, tmp_path: Path
) -> None:
    """On a huge repo the raised test_engineer floor (12) scales 2.0× → 24
    via the same ``huge_repo_multipliers`` resolver pattern the H5 knobs use.
    This is the budget the non-task-role dispatch path applies."""
    from orchestrator.huge_repo_overrides import resolve_huge_repo_value

    cfg = _FakeAutodevCfg()
    eff, mult = resolve_huge_repo_value(
        key="test_engineer",
        base_value=float(_AGENT_MAX_TURNS["test_engineer"]),
        cwd=tmp_path,
        cfg=cfg,
    )
    assert mult == 2.0
    assert int(round(eff)) == 24
