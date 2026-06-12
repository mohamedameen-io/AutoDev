"""v0.39.0 (Cluster A1): tests for :func:`apply_huge_repo_profile`.

The profile resolver returns an EFFECTIVE config with huge-repo-sensible
overrides applied. It is non-destructive (deep copy; never writes
``.autodev/config.json``), idempotent, and escape-hatch-preserving. The
only NEW live effect is auto-enabling ``treat_unrunnable_tests_as_no_tests``
on a huge AND unbuildable repo.

``apply_huge_repo_profile`` binds ``is_huge_repo`` / ``is_repo_unbuildable``
at module import (``from ... import ...``), so the tests patch those
symbols on :mod:`orchestrator.huge_repo_profile` itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from config.defaults import default_config
from orchestrator import huge_repo_profile
from orchestrator.huge_repo_profile import apply_huge_repo_profile


@pytest.fixture(autouse=True)
def _clear_repo_size_cache() -> None:
    from orchestrator.repo_size import clear_cache

    clear_cache()
    yield
    clear_cache()


def _patch_huge(monkeypatch: pytest.MonkeyPatch, *, huge: bool) -> None:
    monkeypatch.setattr(
        huge_repo_profile, "is_huge_repo", lambda cwd, cfg=None: huge
    )


def _patch_unbuildable(monkeypatch: pytest.MonkeyPatch, *, unbuildable: bool) -> None:
    monkeypatch.setattr(
        huge_repo_profile, "is_repo_unbuildable", lambda cwd: unbuildable
    )


def test_small_repo_returns_same_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Small repo → identity (same object, no copy)."""
    _patch_huge(monkeypatch, huge=False)
    _patch_unbuildable(monkeypatch, unbuildable=True)
    cfg = default_config()
    eff = apply_huge_repo_profile(cfg, tmp_path)
    assert eff is cfg


def test_escape_hatch_returns_same_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Escape hatch makes is_huge_repo return False → identity."""
    # Real is_huge_repo honors huge_repo_overrides_disabled, so the escape
    # hatch lands us on the small-repo identity path.
    _patch_huge(monkeypatch, huge=False)
    _patch_unbuildable(monkeypatch, unbuildable=True)
    cfg = default_config()
    cfg.huge_repo_overrides_disabled = True
    eff = apply_huge_repo_profile(cfg, tmp_path)
    assert eff is cfg


def test_huge_unbuildable_deep_copies_and_flips(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """Huge + unbuildable → deep copy with the flag True, input unchanged,
    and one structured log line."""
    _patch_huge(monkeypatch, huge=True)
    _patch_unbuildable(monkeypatch, unbuildable=True)
    cfg = default_config()
    assert cfg.treat_unrunnable_tests_as_no_tests is False

    eff = apply_huge_repo_profile(cfg, tmp_path)

    # Effective copy has the flag flipped; it is NOT the input object.
    assert eff is not cfg
    assert eff.treat_unrunnable_tests_as_no_tests is True
    # Input cfg is unchanged (non-destructive).
    assert cfg.treat_unrunnable_tests_as_no_tests is False
    # Exactly one profile-applied log line (structlog → stdout sink).
    out = capsys.readouterr().out
    assert out.count("orchestrator.huge_repo_profile_applied") == 1


def test_huge_buildable_flag_stays_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Huge but buildable → deep copy, flag stays False."""
    _patch_huge(monkeypatch, huge=True)
    _patch_unbuildable(monkeypatch, unbuildable=False)
    cfg = default_config()
    eff = apply_huge_repo_profile(cfg, tmp_path)
    assert eff.treat_unrunnable_tests_as_no_tests is False


def test_huge_repo_auto_enables_sparse_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Huge repo → ``worktree_sparse_checkout_enabled`` flips True.

    This is the gap-1 fix: huge-repo worktrees must come up sparse so
    they don't materialize the full tree (LFS phantom diffs). Fires
    regardless of buildability.
    """
    _patch_huge(monkeypatch, huge=True)
    _patch_unbuildable(monkeypatch, unbuildable=False)
    cfg = default_config()
    assert cfg.worktree_sparse_checkout_enabled is False

    eff = apply_huge_repo_profile(cfg, tmp_path)

    assert eff.worktree_sparse_checkout_enabled is True
    # Non-destructive: input untouched.
    assert cfg.worktree_sparse_checkout_enabled is False


def test_small_repo_does_not_enable_sparse_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Small repo → no-op; sparse stays at its default (False)."""
    _patch_huge(monkeypatch, huge=False)
    _patch_unbuildable(monkeypatch, unbuildable=False)
    cfg = default_config()
    eff = apply_huge_repo_profile(cfg, tmp_path)
    # Identity on a small repo → flag unchanged.
    assert eff is cfg
    assert eff.worktree_sparse_checkout_enabled is False


def test_sparse_already_enabled_not_clobbered_and_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    """Operator opt-in (sparse already True) → no churn, no extra log line."""
    _patch_huge(monkeypatch, huge=True)
    _patch_unbuildable(monkeypatch, unbuildable=False)
    cfg = default_config()
    cfg.worktree_sparse_checkout_enabled = True

    eff = apply_huge_repo_profile(cfg, tmp_path)
    assert eff.worktree_sparse_checkout_enabled is True
    # Nothing was "applied" for sparse (it was already on) AND the repo is
    # buildable, so there is no profile-applied log line at all.
    out = capsys.readouterr().out
    assert "worktree_sparse_checkout_enabled" not in out


def test_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Apply twice → equal config, flag set once (no churn)."""
    _patch_huge(monkeypatch, huge=True)
    _patch_unbuildable(monkeypatch, unbuildable=True)
    cfg = default_config()
    eff1 = apply_huge_repo_profile(cfg, tmp_path)
    eff2 = apply_huge_repo_profile(eff1, tmp_path)
    assert eff1.treat_unrunnable_tests_as_no_tests is True
    assert eff2.treat_unrunnable_tests_as_no_tests is True
    assert eff1.model_dump() == eff2.model_dump()


def test_no_disk_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The profile never writes .autodev/config.json into the cwd."""
    _patch_huge(monkeypatch, huge=True)
    _patch_unbuildable(monkeypatch, unbuildable=True)
    cfg = default_config()
    apply_huge_repo_profile(cfg, tmp_path)
    assert not (tmp_path / ".autodev").exists()
    assert not (tmp_path / ".autodev" / "config.json").exists()
