"""v0.37.0 H5: large-codebase auto-defaults.

The schema default for ``cfg.task_overrides.huge_repo_multipliers`` now
carries knob-keyed entries for every H1/H2/H3 cap so a first run on a
huge repo auto-scales those caps without operator config tuning. The
:func:`orchestrator.huge_repo_overrides.resolve_huge_repo_value` helper
multiplies the base value when:

1. ``orchestrator.repo_size.is_huge_repo(cwd, cfg=cfg)`` returns True;
2. The knob key is present in ``huge_repo_multipliers``; AND
3. ``cfg.huge_repo_overrides_disabled`` is False (master escape hatch).

The resolver emits a ``huge_repo_multiplier_applied`` telemetry op per
scaled knob via the existing ledger plumbing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.schema import TaskOverridesConfig


# ---------------------------------------------------------------------------
# Default multiplier-dict shape.
# ---------------------------------------------------------------------------


def test_h5_knob_keys_present_in_default_multipliers() -> None:
    """All H1/H2/H3 knob keys ship in the populated default dict."""
    defaults = TaskOverridesConfig().huge_repo_multipliers
    for key in (
        "max_duration_s_per_task",
        "max_diff_bytes",
        "max_corrective_tasks_per_phase",
        "test_diag_breaker_window_s",
        "test_diag_breaker_threshold",
        "recent_evidence_max_chars_per_kind",
        "circuit_breaker_threshold",
    ):
        assert key in defaults, f"missing default multiplier key: {key}"
        assert defaults[key] > 1.0


def test_role_keys_still_present_after_h5_extension() -> None:
    """v0.36.0 E1 role keys must still exist alongside the H5 knob keys."""
    defaults = TaskOverridesConfig().huge_repo_multipliers
    for role in ("explorer", "architect", "coder", "developer", "reviewer"):
        assert role in defaults


# ---------------------------------------------------------------------------
# resolve_huge_repo_value: per-knob multiplication when huge.
# ---------------------------------------------------------------------------


class _FakeAutodevCfg:
    """Minimal duck-typed config for the resolver."""

    def __init__(
        self,
        *,
        multipliers: dict[str, float] | None = None,
        disabled: bool = False,
        threshold: int = 5000,
    ) -> None:
        self.huge_repo_overrides_disabled = disabled
        self.index_full_rebuild_threshold_files = threshold
        # Mirror the real config shape so the resolver can read
        # ``cfg.task_overrides.huge_repo_multipliers``.
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
    """Patch :func:`orchestrator.repo_size.is_huge_repo` to always return
    True so the resolver is tested in isolation from the file-count
    probe. The resolver imports the helper locally inside the function
    body, so we patch the symbol on :mod:`orchestrator.repo_size`
    itself (the canonical source).
    """
    import orchestrator.repo_size as size_mod

    def _force_true(cwd: Path, threshold: int | None = None, *, cfg: Any = None) -> bool:  # noqa: ARG001
        if cfg is not None and getattr(cfg, "huge_repo_overrides_disabled", False):
            return False
        return True

    monkeypatch.setattr(size_mod, "is_huge_repo", _force_true)


def test_resolver_scales_max_corrective_tasks_per_phase(
    _force_huge_repo: None, tmp_path: Path
) -> None:
    from orchestrator.huge_repo_overrides import resolve_huge_repo_value

    cfg = _FakeAutodevCfg(multipliers={"max_corrective_tasks_per_phase": 2.0})
    eff, mult = resolve_huge_repo_value(
        key="max_corrective_tasks_per_phase",
        base_value=8.0,
        cwd=tmp_path,
        cfg=cfg,
    )
    assert eff == 16.0
    assert mult == 2.0


def test_resolver_scales_recent_evidence_cap(
    _force_huge_repo: None, tmp_path: Path
) -> None:
    from orchestrator.huge_repo_overrides import resolve_huge_repo_value

    cfg = _FakeAutodevCfg(multipliers={"recent_evidence_max_chars_per_kind": 1.5})
    eff, mult = resolve_huge_repo_value(
        key="recent_evidence_max_chars_per_kind",
        base_value=4000.0,
        cwd=tmp_path,
        cfg=cfg,
    )
    assert eff == 6000.0
    assert mult == 1.5


def test_resolver_scales_test_diag_window(
    _force_huge_repo: None, tmp_path: Path
) -> None:
    from orchestrator.huge_repo_overrides import resolve_huge_repo_value

    cfg = _FakeAutodevCfg(multipliers={"test_diag_breaker_window_s": 2.0})
    eff, mult = resolve_huge_repo_value(
        key="test_diag_breaker_window_s",
        base_value=600.0,
        cwd=tmp_path,
        cfg=cfg,
    )
    assert eff == 1200.0
    assert mult == 2.0


def test_resolver_scales_max_turns_ceiling_huge(
    _force_huge_repo: None, tmp_path: Path
) -> None:
    """v0.39.0 (Cluster A3): the budget-escalation turns ceiling lifts 1.5×
    on huge repos (250 → 375). Uses the real default multiplier dict so the
    test pins the shipped 1.5 multiplier."""
    from orchestrator.huge_repo_overrides import resolve_huge_repo_value

    cfg = _FakeAutodevCfg()  # default multipliers (carry max_turns_ceiling)
    eff, mult = resolve_huge_repo_value(
        key="max_turns_ceiling",
        base_value=250.0,
        cwd=tmp_path,
        cfg=cfg,
    )
    assert int(round(eff)) == 375
    assert mult == 1.5


def test_resolver_max_turns_ceiling_identity_small_repo(tmp_path: Path) -> None:
    """On a small (non-huge) repo the ceiling resolver is identity → 250."""
    from orchestrator.huge_repo_overrides import resolve_huge_repo_value

    cfg = _FakeAutodevCfg(threshold=10**9)  # never huge
    eff, mult = resolve_huge_repo_value(
        key="max_turns_ceiling",
        base_value=250.0,
        cwd=tmp_path,
        cfg=cfg,
    )
    assert int(round(eff)) == 250
    assert mult is None


# ---------------------------------------------------------------------------
# Escape hatch: huge_repo_overrides_disabled.
# ---------------------------------------------------------------------------


def test_escape_hatch_disables_all_scaling(tmp_path: Path) -> None:
    """``cfg.huge_repo_overrides_disabled=True`` returns base unchanged
    even when the repo would otherwise count as huge."""
    from orchestrator.huge_repo_overrides import resolve_huge_repo_value
    from orchestrator.repo_size import is_huge_repo

    # Verify the escape hatch short-circuits the is_huge_repo helper.
    cfg = _FakeAutodevCfg(disabled=True)
    assert is_huge_repo(tmp_path, cfg=cfg) is False

    # ...and therefore the resolver returns base + None multiplier.
    eff, mult = resolve_huge_repo_value(
        key="max_corrective_tasks_per_phase",
        base_value=8.0,
        cwd=tmp_path,
        cfg=cfg,
    )
    assert eff == 8.0
    assert mult is None


# ---------------------------------------------------------------------------
# Small-repo backward compat.
# ---------------------------------------------------------------------------


def test_small_repo_no_scaling(tmp_path: Path) -> None:
    """A small (<5000 files) tmp_path returns base unchanged."""
    from orchestrator.huge_repo_overrides import resolve_huge_repo_value

    cfg = _FakeAutodevCfg()
    eff, mult = resolve_huge_repo_value(
        key="max_corrective_tasks_per_phase",
        base_value=8.0,
        cwd=tmp_path,
        cfg=cfg,
    )
    assert eff == 8.0
    assert mult is None


# ---------------------------------------------------------------------------
# Empty / missing multiplier dict.
# ---------------------------------------------------------------------------


def test_empty_multipliers_no_scaling(
    _force_huge_repo: None, tmp_path: Path
) -> None:
    """``huge_repo_multipliers={}`` produces no scaling even on huge repos."""
    from orchestrator.huge_repo_overrides import resolve_huge_repo_value

    cfg = _FakeAutodevCfg(multipliers={})
    eff, mult = resolve_huge_repo_value(
        key="max_corrective_tasks_per_phase",
        base_value=8.0,
        cwd=tmp_path,
        cfg=cfg,
    )
    assert eff == 8.0
    assert mult is None


def test_missing_key_no_scaling(_force_huge_repo: None, tmp_path: Path) -> None:
    """Keys absent from the dict don't get scaled."""
    from orchestrator.huge_repo_overrides import resolve_huge_repo_value

    cfg = _FakeAutodevCfg(multipliers={"other_key": 5.0})
    eff, mult = resolve_huge_repo_value(
        key="max_corrective_tasks_per_phase",
        base_value=8.0,
        cwd=tmp_path,
        cfg=cfg,
    )
    assert eff == 8.0
    assert mult is None


# ---------------------------------------------------------------------------
# Telemetry op emission shape via apply_and_log_huge_repo_value.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_and_log_emits_telemetry_op_per_key(
    _force_huge_repo: None, tmp_path: Path
) -> None:
    from orchestrator.huge_repo_overrides import apply_and_log_huge_repo_value

    cfg = _FakeAutodevCfg(
        multipliers={
            "max_corrective_tasks_per_phase": 2.0,
            "test_diag_breaker_window_s": 2.0,
        }
    )
    emitted: list[dict[str, Any]] = []

    async def _ledger(op: str, payload: dict[str, Any]) -> None:
        emitted.append({"op": op, "payload": payload})

    eff_corrective = await apply_and_log_huge_repo_value(
        key="max_corrective_tasks_per_phase",
        base_value=8.0,
        cwd=tmp_path,
        cfg=cfg,
        ledger_append=_ledger,
    )
    eff_window = await apply_and_log_huge_repo_value(
        key="test_diag_breaker_window_s",
        base_value=600.0,
        cwd=tmp_path,
        cfg=cfg,
        ledger_append=_ledger,
    )

    assert eff_corrective == 16.0
    assert eff_window == 1200.0
    assert len(emitted) == 2
    ops_by_key = {e["payload"]["key"]: e for e in emitted}
    assert ops_by_key["max_corrective_tasks_per_phase"]["op"] == (
        "huge_repo_multiplier_applied"
    )
    payload = ops_by_key["max_corrective_tasks_per_phase"]["payload"]
    assert payload["base_value"] == 8.0
    assert payload["multiplier"] == 2.0
    assert payload["effective_value"] == 16.0


@pytest.mark.asyncio
async def test_apply_and_log_no_op_when_escape_hatch_set(
    tmp_path: Path,
) -> None:
    """No telemetry op emitted when scaling didn't fire."""
    from orchestrator.huge_repo_overrides import apply_and_log_huge_repo_value

    cfg = _FakeAutodevCfg(disabled=True)
    emitted: list[Any] = []

    async def _ledger(op: str, payload: dict[str, Any]) -> None:
        emitted.append((op, payload))

    eff = await apply_and_log_huge_repo_value(
        key="max_corrective_tasks_per_phase",
        base_value=8.0,
        cwd=tmp_path,
        cfg=cfg,
        ledger_append=_ledger,
    )

    assert eff == 8.0
    assert emitted == []


# ---------------------------------------------------------------------------
# resolve_all_h5_knobs convenience.
# ---------------------------------------------------------------------------


def test_resolve_all_h5_knobs_returns_per_key_tuples(
    _force_huge_repo: None, tmp_path: Path
) -> None:
    from orchestrator.huge_repo_overrides import resolve_all_h5_knobs

    cfg = _FakeAutodevCfg(
        multipliers={
            "max_corrective_tasks_per_phase": 2.0,
            "test_diag_breaker_window_s": 2.0,
            "test_diag_breaker_threshold": 2.0,
            "circuit_breaker_threshold": 2.0,
            "recent_evidence_max_chars_per_kind": 1.5,
        }
    )
    # Add base values for each knob the resolver reads from cfg.
    cfg.max_corrective_tasks_per_phase = 8
    cfg.test_diag_breaker_window_s = 600.0
    cfg.test_diag_breaker_threshold = 3
    cfg.circuit_breaker_threshold = 3
    cfg.recent_evidence_max_chars_per_kind = 4000

    out = resolve_all_h5_knobs(cwd=tmp_path, cfg=cfg)

    assert set(out.keys()) == {
        "max_corrective_tasks_per_phase",
        "test_diag_breaker_window_s",
        "test_diag_breaker_threshold",
        "circuit_breaker_threshold",
        "recent_evidence_max_chars_per_kind",
    }
    base, mult, eff = out["max_corrective_tasks_per_phase"]
    assert base == 8.0 and mult == 2.0 and eff == 16.0


def test_resolve_all_h5_knobs_empty_on_small_repo(tmp_path: Path) -> None:
    from orchestrator.huge_repo_overrides import resolve_all_h5_knobs

    cfg = _FakeAutodevCfg()
    cfg.max_corrective_tasks_per_phase = 8
    out = resolve_all_h5_knobs(cwd=tmp_path, cfg=cfg)
    assert out == {}
