"""v0.38.0 I1 (HK13): end-to-end huge-repo integration for GuardrailEnforcer.

Materialises a real on-disk repo above the
``index_full_rebuild_threshold_files`` default and verifies that
:class:`guardrails.enforcer.GuardrailEnforcer` resolves its effective
``max_duration_s_per_task`` / ``max_diff_bytes`` caps through the H5
huge-repo multipliers. This closes the wiring gap left over from v0.37.0
where the multipliers were declared in the defaults dict but never
consulted at enforcement time.

Real disk fixture (5001 ``.cpp`` files) intentionally exercises the
``os.walk`` fallback path inside :func:`orchestrator.repo_size.is_huge_repo`
— no monkeypatching, so this catches resolution-order regressions in
the actual production code path used by large-codebase operators.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.defaults import default_config
from config.schema import AutodevConfig
from guardrails.enforcer import GuardrailEnforcer
from orchestrator.repo_size import (
    DEFAULT_HUGE_REPO_THRESHOLD,
    clear_cache,
    clear_ttl_cache,
    is_huge_repo,
)


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _drain_repo_size_caches() -> None:
    """Both caches must be empty before and after each test so the on-disk
    fixture is re-counted instead of inheriting a stale entry."""
    clear_cache()
    clear_ttl_cache()
    yield
    clear_cache()
    clear_ttl_cache()


@pytest.fixture
def huge_cpp_repo(tmp_path: Path) -> Path:
    """A tmp_path with > 5000 ``.cpp`` files — pushes
    :func:`is_huge_repo` past its default threshold via the os.walk
    fallback (no .git dir; the git fast-path is skipped)."""
    # 5001 keeps the test deterministic; one above the 5000 default.
    target = DEFAULT_HUGE_REPO_THRESHOLD + 1
    for i in range(target):
        (tmp_path / f"f{i}.cpp").touch()
    return tmp_path


def _make_cfg(
    *,
    huge_repo_overrides_disabled: bool = False,
    max_duration_s_per_task: int = 600,
    max_diff_bytes: int = 100_000,
) -> AutodevConfig:
    """Build a populated :class:`AutodevConfig` via the project's
    default-config helper, then override the bits we care about.

    Using ``default_config()`` guarantees every nested required model
    (agents, tournaments, hive, …) is supplied — bypasses brittle
    hand-assembly that drifts every release.
    """
    base = default_config().model_dump()
    base["huge_repo_overrides_disabled"] = huge_repo_overrides_disabled
    base["guardrails"]["max_duration_s_per_task"] = max_duration_s_per_task
    base["guardrails"]["max_diff_bytes"] = max_diff_bytes
    return AutodevConfig(**base)


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


def test_huge_cpp_repo_is_detected_as_huge(huge_cpp_repo: Path) -> None:
    """Real on-disk fixture trips ``is_huge_repo`` via os.walk fallback."""
    assert is_huge_repo(huge_cpp_repo) is True


def test_enforcer_scales_caps_on_huge_repo(huge_cpp_repo: Path) -> None:
    """v0.38.0 I1: huge-repo multipliers fire for both cap knobs."""
    cfg = _make_cfg()
    base_duration = float(cfg.guardrails.max_duration_s_per_task)
    base_diff = float(cfg.guardrails.max_diff_bytes)
    mult_duration = float(
        cfg.task_overrides.huge_repo_multipliers["max_duration_s_per_task"]
    )
    mult_diff = float(cfg.task_overrides.huge_repo_multipliers["max_diff_bytes"])

    enf = GuardrailEnforcer(cfg.guardrails, cwd=huge_cpp_repo, parent_cfg=cfg)

    assert enf._eff_max_duration_s == pytest.approx(base_duration * mult_duration)
    assert enf._eff_max_diff_bytes == pytest.approx(base_diff * mult_diff)


def test_enforcer_respects_huge_repo_escape_hatch(huge_cpp_repo: Path) -> None:
    """``huge_repo_overrides_disabled=True`` returns raw cap values even
    when the on-disk file count crosses the threshold."""
    cfg = _make_cfg(huge_repo_overrides_disabled=True)
    # Re-sanity-check we still hold the unmodified guardrails caps.
    base_duration = float(cfg.guardrails.max_duration_s_per_task)
    base_diff = float(cfg.guardrails.max_diff_bytes)

    enf = GuardrailEnforcer(cfg.guardrails, cwd=huge_cpp_repo, parent_cfg=cfg)

    assert enf._eff_max_duration_s == pytest.approx(base_duration)
    assert enf._eff_max_diff_bytes == pytest.approx(base_diff)


def test_enforcer_without_cwd_uses_raw_caps(huge_cpp_repo: Path) -> None:
    """Legacy ``GuardrailEnforcer(cfg.guardrails)`` callers fall through
    to the raw guardrail values even on a huge repo (backward compat)."""
    cfg = _make_cfg()
    base_duration = float(cfg.guardrails.max_duration_s_per_task)
    base_diff = float(cfg.guardrails.max_diff_bytes)

    enf = GuardrailEnforcer(cfg.guardrails)

    assert enf._eff_max_duration_s == pytest.approx(base_duration)
    assert enf._eff_max_diff_bytes == pytest.approx(base_diff)
