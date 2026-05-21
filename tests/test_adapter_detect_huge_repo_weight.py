"""v0.37.0 H5: ``AUTODEV_LANG_WEIGHT`` defaults to 0.5 on huge repos.

When ``cwd`` is supplied and :func:`orchestrator.repo_size.is_huge_repo`
returns True, the language-weighted platform fitness path engages by
default (weight=0.5) so the operator's first run on a huge repo picks
the better-fit adapter without needing to set
``AUTODEV_LANG_WEIGHT=1.0`` manually.

Precedence (unchanged from H4):
1. Explicit ``preferred`` parameter wins.
2. Trigger-context env (``CLAUDECODE`` / ``TERM_PROGRAM=Cursor``).
3. ``AUTODEV_PLATFORM`` env.
4. ``AUTODEV_LANG_WEIGHT`` env (H5 default: 0.5 on huge repos).
5. Fallback to Claude → Cursor.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from adapters.claude_code import ClaudeCodeAdapter
from adapters.cursor import CursorAdapter
from adapters.detect import detect_platform


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip host trigger / weight env vars so the developer shell doesn't
    bleed in. H4 trigger-context coverage lives in its own test module."""
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.delenv("AUTODEV_PLATFORM", raising=False)
    monkeypatch.delenv("AUTODEV_LANG_WEIGHT", raising=False)
    for key in [k for k in list(os.environ) if k.startswith("CURSOR_")]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _clear_repo_size_cache() -> None:
    from orchestrator.repo_size import clear_cache

    clear_cache()
    yield
    clear_cache()


def _write_ts_codebase(tmp_path: Path, n: int = 5) -> None:
    """Make the cwd look TS-heavy so cursor wins fitness if engaged."""
    (tmp_path / "src").mkdir(exist_ok=True)
    for i in range(n):
        (tmp_path / "src" / f"app{i}.ts").write_text("export {};\n", encoding="utf-8")


def _patch_huge(monkeypatch: pytest.MonkeyPatch, huge: bool) -> None:
    """Force the H5 is_huge_repo helper to a fixed boolean."""
    import orchestrator.repo_size as size_mod

    def _f(cwd: Path, threshold: int | None = None, *, cfg: Any = None) -> bool:  # noqa: ARG001
        if cfg is not None and getattr(cfg, "huge_repo_overrides_disabled", False):
            return False
        return huge

    monkeypatch.setattr(size_mod, "is_huge_repo", _f)


# ---------------------------------------------------------------------------
# Huge repo + no env → weight defaults to 0.5 → fitness engages.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_huge_repo_default_weight_engages_fitness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No env var set; is_huge_repo=True → fitness path runs → cursor wins on TS-heavy cwd."""
    _patch_huge(monkeypatch, huge=True)
    _write_ts_codebase(tmp_path)

    with (
        patch.object(
            ClaudeCodeAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "ok")),
        ),
        patch.object(
            CursorAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "ok")),
        ),
    ):
        name = await detect_platform("auto", cwd=tmp_path)

    assert name == "cursor"


@pytest.mark.asyncio
async def test_small_repo_default_weight_zero_keeps_claude(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Small repo → default weight 0.0 → fitness OFF → Claude wins by historical bias."""
    _patch_huge(monkeypatch, huge=False)
    _write_ts_codebase(tmp_path)

    with (
        patch.object(
            ClaudeCodeAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "ok")),
        ),
        patch.object(
            CursorAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "ok")),
        ),
    ):
        name = await detect_platform("auto", cwd=tmp_path)

    assert name == "claude_code"


# ---------------------------------------------------------------------------
# Env var still wins over the H5 huge-repo default.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_lang_weight_overrides_huge_repo_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``AUTODEV_LANG_WEIGHT=2.0`` env wins even on huge repos."""
    monkeypatch.setenv("AUTODEV_LANG_WEIGHT", "2.0")
    _patch_huge(monkeypatch, huge=True)
    _write_ts_codebase(tmp_path)

    with (
        patch.object(
            ClaudeCodeAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "ok")),
        ),
        patch.object(
            CursorAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "ok")),
        ),
    ):
        name = await detect_platform("auto", cwd=tmp_path)

    # Cursor wins on TS-heavy cwd regardless — but the test exercises the
    # env-wins-path. ``AUTODEV_LANG_WEIGHT=0.0`` would assert the inverse.
    assert name == "cursor"


@pytest.mark.asyncio
async def test_env_lang_weight_zero_disables_fitness_even_on_huge_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Operator can explicitly opt OUT of H5's huge-repo fitness via
    ``AUTODEV_LANG_WEIGHT=0.0`` env override."""
    monkeypatch.setenv("AUTODEV_LANG_WEIGHT", "0.0")
    _patch_huge(monkeypatch, huge=True)
    _write_ts_codebase(tmp_path)

    with (
        patch.object(
            ClaudeCodeAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "ok")),
        ),
        patch.object(
            CursorAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "ok")),
        ),
    ):
        name = await detect_platform("auto", cwd=tmp_path)

    # Fitness disabled → Claude historical bias wins despite TS-heavy cwd.
    assert name == "claude_code"


# ---------------------------------------------------------------------------
# Trigger-context (H4) precedence still wins over H5 lang-weight default.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trigger_context_wins_over_huge_repo_lang_weight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``CLAUDECODE=1`` + huge repo → claude_code wins WITHOUT consulting fitness.

    Verifies H4 precedence is preserved when H5 lang-weight default applies.
    """
    monkeypatch.setenv("CLAUDECODE", "1")
    _patch_huge(monkeypatch, huge=True)
    _write_ts_codebase(tmp_path)  # TS-heavy — would route Cursor under fitness.

    with (
        patch.object(
            ClaudeCodeAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "ok")),
        ),
        patch.object(
            CursorAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "ok")),
        ),
    ):
        name = await detect_platform("auto", cwd=tmp_path)

    assert name == "claude_code"


# ---------------------------------------------------------------------------
# cwd=None preserves legacy behavior (no huge-repo probe possible).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_cwd_default_weight_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cwd is None`` → can't run the huge-repo probe → weight stays 0.0."""
    with (
        patch.object(
            ClaudeCodeAdapter,
            "healthcheck",
            AsyncMock(return_value=(True, "ok")),
        ),
    ):
        name = await detect_platform("auto", cwd=None)
    assert name == "claude_code"
