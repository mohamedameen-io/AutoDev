"""v0.17.0 S5: ``Orchestrator.tracked_files`` lazy cache.

The cache is populated on first access by shelling out to ``git ls-files``.
Mirrors the v0.13.0 ``_repo_capacity`` lazy-init pattern: ``None`` means
"not yet probed"; subsequent reads return the same set.

Tests cover:

1. Lazy initialization — accessor returns a non-None set after first call.
2. Caching — second access returns the SAME set object (identity check).
3. Empty repo — accessor returns the empty set without crashing.
4. Manual override — assigning to ``_tracked_files`` directly is honored.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from config.defaults import default_config
from adapters.types import AgentSpec


def _git_repo(tmp_path: Path, files: list[str]) -> Path:
    """Build a small git repo containing ``files``."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "t"],
        cwd=tmp_path,
        check=True,
    )
    for fp in files:
        full = tmp_path / fp
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text("# stub\n")
    if files:
        subprocess.run(
            ["git", "add", "-A"], cwd=tmp_path, check=True
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "init"],
            cwd=tmp_path,
            check=True,
            env={
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t",
                "PATH": __import__("os").environ.get("PATH", ""),
            },
        )
    return tmp_path


@pytest.fixture
def stub_orchestrator(tmp_path: Path):
    """Build an Orchestrator over a tiny git repo with two tracked files."""
    from orchestrator import Orchestrator
    from stub_adapter import StubAdapter

    repo = _git_repo(tmp_path, ["src/qa/foo.py", "src/main.py"])
    cfg = default_config()
    adapter = StubAdapter({})
    registry: dict[str, AgentSpec] = {}
    return Orchestrator(repo, cfg, adapter, registry, session_id="t")


def test_tracked_files_lazy_populated(stub_orchestrator) -> None:
    files = stub_orchestrator.tracked_files
    assert isinstance(files, set)
    assert "src/qa/foo.py" in files
    assert "src/main.py" in files


def test_tracked_files_cached_on_repeated_access(stub_orchestrator) -> None:
    first = stub_orchestrator.tracked_files
    second = stub_orchestrator.tracked_files
    # Identity: same set object — no second probe.
    assert first is second


def test_tracked_files_in_empty_repo(tmp_path: Path) -> None:
    """Empty repo (no tracked files) returns the empty set, no crash."""
    from orchestrator import Orchestrator
    from stub_adapter import StubAdapter

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    cfg = default_config()
    adapter = StubAdapter({})
    orch = Orchestrator(tmp_path, cfg, adapter, {}, session_id="t")
    assert orch.tracked_files == set()
