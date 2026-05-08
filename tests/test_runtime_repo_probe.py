"""Tests for :mod:`runtime.repo_probe`.

The module probes repo size (file count, total bytes) at orchestrator start
and provides a resolver that maps capacity + complexity onto a
``max_turns`` value scaled for huge repos. Tests mock subprocess so they're
hermetic and deterministic across hosts / git states.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# probe_repo: returns a populated RepoCapacity dataclass
# ---------------------------------------------------------------------------


def test_probe_repo_returns_positive_counts(tmp_path: Path) -> None:
    """The unmocked probe returns sensible non-negative values on any
    directory (even an empty one or a non-git dir falls back to du-style
    enumeration)."""
    from runtime.repo_probe import RepoCapacity, probe_repo

    cap = probe_repo(tmp_path)
    assert isinstance(cap, RepoCapacity)
    assert cap.file_count >= 0
    assert cap.total_bytes >= 0
    # Empty/tiny dir is never huge.
    assert cap.is_huge is False


def test_probe_repo_marks_unity_class_as_huge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A repo with >20k files (mocked) flips ``is_huge`` to True.

    Mocks the file-count branch to simulate a Unity-class repo.
    """
    from runtime import repo_probe

    def fake_count(_cwd: Path) -> int:
        return 25_000

    def fake_bytes(_cwd: Path) -> int:
        return 1_000_000_000  # 1 GB — under 5 GB threshold

    monkeypatch.setattr(repo_probe, "_count_files", fake_count)
    monkeypatch.setattr(repo_probe, "_total_bytes", fake_bytes)

    cap = repo_probe.probe_repo(tmp_path)
    assert cap.file_count == 25_000
    assert cap.total_bytes == 1_000_000_000
    assert cap.is_huge is True


def test_probe_repo_marks_huge_via_total_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A repo with normal file count but >5 GB total bytes is also huge."""
    from runtime import repo_probe

    monkeypatch.setattr(repo_probe, "_count_files", lambda _: 5_000)
    monkeypatch.setattr(repo_probe, "_total_bytes", lambda _: 6 * 1024**3)

    cap = repo_probe.probe_repo(tmp_path)
    assert cap.file_count == 5_000
    assert cap.is_huge is True


def test_probe_repo_marks_normal_repo_as_not_huge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Below both thresholds → ``is_huge`` stays False."""
    from runtime import repo_probe

    monkeypatch.setattr(repo_probe, "_count_files", lambda _: 1_500)
    monkeypatch.setattr(repo_probe, "_total_bytes", lambda _: 100 * 1024**2)

    cap = repo_probe.probe_repo(tmp_path)
    assert cap.is_huge is False


def test_probe_repo_logs_once(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """The probe emits ``tournament.repo_probed`` with structured fields."""
    from runtime import repo_probe

    monkeypatch.setattr(repo_probe, "_count_files", lambda _: 12_345)
    monkeypatch.setattr(repo_probe, "_total_bytes", lambda _: 99_999)

    repo_probe.probe_repo(tmp_path)
    out = capsys.readouterr().out
    assert "tournament.repo_probed" in out
    assert ("file_count=12345" in out) or ('"file_count": 12345' in out)
    assert ("is_huge=False" in out) or ('"is_huge": false' in out)


# ---------------------------------------------------------------------------
# resolve_max_turns: layers an is_huge multiplier over TASK_MAX_TURNS_DEFAULTS
# ---------------------------------------------------------------------------


def test_resolve_max_turns_huge_repo_doubles_simple() -> None:
    """``is_huge=True`` + ``complexity='simple'`` + ``base=None`` → 20
    (doubled from default 10)."""
    from runtime.repo_probe import RepoCapacity, resolve_max_turns

    cap = RepoCapacity(
        file_count=25_000, total_bytes=10_000_000_000, depth_max=10, is_huge=True
    )
    assert resolve_max_turns("simple", cap, base=None) == 20


def test_resolve_max_turns_huge_repo_doubles_medium() -> None:
    from runtime.repo_probe import RepoCapacity, resolve_max_turns

    cap = RepoCapacity(
        file_count=25_000, total_bytes=10_000_000_000, depth_max=10, is_huge=True
    )
    assert resolve_max_turns("medium", cap, base=None) == 40


def test_resolve_max_turns_huge_repo_doubles_complex() -> None:
    from runtime.repo_probe import RepoCapacity, resolve_max_turns

    cap = RepoCapacity(
        file_count=25_000, total_bytes=10_000_000_000, depth_max=10, is_huge=True
    )
    assert resolve_max_turns("complex", cap, base=None) == 80


def test_resolve_max_turns_normal_repo_preserves_base() -> None:
    """``is_huge=False`` → no multiplier — falls back to lookup or base."""
    from runtime.repo_probe import RepoCapacity, resolve_max_turns

    cap = RepoCapacity(
        file_count=1_000, total_bytes=10_000_000, depth_max=5, is_huge=False
    )
    assert resolve_max_turns("simple", cap, base=None) == 10
    assert resolve_max_turns("medium", cap, base=None) == 20
    assert resolve_max_turns("complex", cap, base=None) == 40


def test_resolve_max_turns_explicit_base_overrides() -> None:
    """An explicit ``base`` (operator override) bypasses the lookup table.

    On normal repos: base is returned unchanged.
    On huge repos: base is doubled.
    """
    from runtime.repo_probe import RepoCapacity, resolve_max_turns

    normal = RepoCapacity(
        file_count=1_000, total_bytes=10_000_000, depth_max=5, is_huge=False
    )
    huge = RepoCapacity(
        file_count=25_000, total_bytes=10_000_000_000, depth_max=10, is_huge=True
    )
    assert resolve_max_turns("medium", normal, base=15) == 15
    assert resolve_max_turns("medium", huge, base=15) == 30


def test_resolve_max_turns_complexity_none_returns_none() -> None:
    """``complexity=None`` + ``base=None`` → ``None`` (caller falls back)."""
    from runtime.repo_probe import RepoCapacity, resolve_max_turns

    cap = RepoCapacity(
        file_count=1_000, total_bytes=10_000_000, depth_max=5, is_huge=False
    )
    assert resolve_max_turns(None, cap, base=None) is None


def test_resolve_max_turns_complexity_none_with_base_returns_base() -> None:
    """``complexity=None`` but explicit base → base (doubled if huge)."""
    from runtime.repo_probe import RepoCapacity, resolve_max_turns

    normal = RepoCapacity(
        file_count=1_000, total_bytes=10_000_000, depth_max=5, is_huge=False
    )
    huge = RepoCapacity(
        file_count=25_000, total_bytes=10_000_000_000, depth_max=10, is_huge=True
    )
    assert resolve_max_turns(None, normal, base=12) == 12
    assert resolve_max_turns(None, huge, base=12) == 24


def test_resolve_max_turns_unknown_complexity_returns_none() -> None:
    """Defensive: an out-of-Literal complexity yields ``None`` (caller's
    spec default kicks in)."""
    from runtime.repo_probe import RepoCapacity, resolve_max_turns

    cap = RepoCapacity(
        file_count=1_000, total_bytes=10_000_000, depth_max=5, is_huge=False
    )
    assert resolve_max_turns("trivial", cap, base=None) is None
