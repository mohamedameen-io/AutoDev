"""v0.39.0 B3: conservative parallelism on huge repos.

:func:`orchestrator.huge_repo_overrides.resolve_huge_repo_parallelism`
halves the host-resolved parallelism on huge repos (via the
``parallelism_multiplier`` key in ``huge_repo_multipliers``) to reduce
429/529 overload, while leaving operator pins and small repos untouched.

Resolution rules:

1. ``configured is not None`` (operator pin) → *base* returned verbatim,
   never silently scaled.
2. Small repo / escape hatch (``is_huge_repo`` False) → *base* unchanged.
3. Huge repo, auto-resolved → ``int(base * mult)`` floored at 1 and
   capped at ``_HUGE_REPO_PARALLELISM_CEILING``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from config.schema import TaskOverridesConfig


class _FakeAutodevCfg:
    """Minimal duck-typed config for the resolver."""

    def __init__(
        self,
        *,
        multipliers: dict[str, float] | None = None,
        disabled: bool = False,
    ) -> None:
        self.huge_repo_overrides_disabled = disabled
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
    """Force :func:`orchestrator.repo_size.is_huge_repo` True (honors the
    escape hatch) so the helper is tested in isolation from the
    file-count probe."""
    import orchestrator.repo_size as size_mod

    def _force_true(cwd: Path, threshold: int | None = None, *, cfg: Any = None) -> bool:  # noqa: ARG001
        if cfg is not None and getattr(cfg, "huge_repo_overrides_disabled", False):
            return False
        return True

    monkeypatch.setattr(size_mod, "is_huge_repo", _force_true)


# ---------------------------------------------------------------------------
# Module constant / export.
# ---------------------------------------------------------------------------


def test_ceiling_constant_value() -> None:
    from orchestrator.huge_repo_overrides import _HUGE_REPO_PARALLELISM_CEILING

    assert _HUGE_REPO_PARALLELISM_CEILING == 6


def test_helper_exported() -> None:
    import orchestrator.huge_repo_overrides as mod

    assert "resolve_huge_repo_parallelism" in mod.__all__


# ---------------------------------------------------------------------------
# Huge-repo halving.
# ---------------------------------------------------------------------------


def test_huge_repo_halves_auto_resolved(
    _force_huge_repo: None, tmp_path: Path
) -> None:
    """base 12 + huge + mult 0.5 → 6."""
    from orchestrator.huge_repo_overrides import resolve_huge_repo_parallelism

    cfg = _FakeAutodevCfg(multipliers={"parallelism_multiplier": 0.5})
    out = resolve_huge_repo_parallelism(
        base=12, configured=None, cwd=tmp_path, cfg=cfg
    )
    assert out == 6


def test_huge_repo_caps_at_ceiling(
    _force_huge_repo: None, tmp_path: Path
) -> None:
    """base 20 → halved to 10 → capped at the ceiling (6)."""
    from orchestrator.huge_repo_overrides import resolve_huge_repo_parallelism

    cfg = _FakeAutodevCfg(multipliers={"parallelism_multiplier": 0.5})
    out = resolve_huge_repo_parallelism(
        base=20, configured=None, cwd=tmp_path, cfg=cfg
    )
    assert out == 6


def test_huge_repo_floors_at_one(
    _force_huge_repo: None, tmp_path: Path
) -> None:
    """base 1 → 0.5 → floored at 1."""
    from orchestrator.huge_repo_overrides import resolve_huge_repo_parallelism

    cfg = _FakeAutodevCfg(multipliers={"parallelism_multiplier": 0.5})
    out = resolve_huge_repo_parallelism(
        base=1, configured=None, cwd=tmp_path, cfg=cfg
    )
    assert out == 1


# ---------------------------------------------------------------------------
# Operator pin = escape hatch (never silently scaled).
# ---------------------------------------------------------------------------


def test_operator_pin_returns_base_unchanged(
    _force_huge_repo: None, tmp_path: Path
) -> None:
    """configured=8 (pin) on a huge repo → returns *base*, not scaled."""
    from orchestrator.huge_repo_overrides import resolve_huge_repo_parallelism

    cfg = _FakeAutodevCfg(multipliers={"parallelism_multiplier": 0.5})
    out = resolve_huge_repo_parallelism(
        base=12, configured=8, cwd=tmp_path, cfg=cfg
    )
    # The pin bypasses scaling — the helper returns the *base* it was
    # handed (the caller resolves the pin into base via resolve_parallelism).
    assert out == 12


# ---------------------------------------------------------------------------
# Small repo / escape hatch → no-op.
# ---------------------------------------------------------------------------


def test_small_repo_returns_base(tmp_path: Path) -> None:
    """is_huge_repo False (small tmp_path) → base unchanged."""
    from orchestrator.huge_repo_overrides import resolve_huge_repo_parallelism

    cfg = _FakeAutodevCfg(multipliers={"parallelism_multiplier": 0.5})
    out = resolve_huge_repo_parallelism(
        base=12, configured=None, cwd=tmp_path, cfg=cfg
    )
    assert out == 12


def test_escape_hatch_returns_base(tmp_path: Path) -> None:
    """huge_repo_overrides_disabled → is_huge_repo False → base unchanged."""
    from orchestrator.huge_repo_overrides import resolve_huge_repo_parallelism

    cfg = _FakeAutodevCfg(
        multipliers={"parallelism_multiplier": 0.5}, disabled=True
    )
    out = resolve_huge_repo_parallelism(
        base=12, configured=None, cwd=tmp_path, cfg=cfg
    )
    assert out == 12
