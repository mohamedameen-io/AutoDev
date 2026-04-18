"""Shared fixtures for integration tests.

Test boundary
-------------
There are two tiers of integration tests in this package:

**Stub-backed tests** (default)
    Use :class:`~adapters.stub.StubAdapter` instead of a real LLM binary.
    These run with no special environment variables and are safe to execute in
    CI without network access or API credentials.  The vast majority of tests
    in this package fall into this category.

**Live tests**
    Invoke the real ``claude -p`` subprocess (or equivalent) and make actual
    LLM calls.  A test opts into live mode by requesting the :func:`live_mode`
    fixture, which skips automatically unless the ``AUTODEV_LIVE=1`` environment
    variable is set:

    .. code-block:: shell

        AUTODEV_LIVE=1 pytest tests/integration/ -v

    Live tests are excluded from normal CI runs to avoid flakiness caused by
    network conditions or model availability.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from config.defaults import default_config
from config.loader import save_config
from config.schema import AutodevConfig, QAGatesConfig


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: mark test as an integration test (requires explicit run)",
    )
    config.addinivalue_line(
        "markers",
        "live: mark test as requiring AUTODEV_LIVE=1 env var",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_init_repo(repo: Path, user_email: str = "t@t", user_name: str = "t") -> None:
    """Initialise a bare git repo with user config."""
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", user_email],
        cwd=str(repo),
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", user_name],
        cwd=str(repo),
        check=True,
    )


def _git_commit_all(repo: Path, message: str = "initial") -> None:
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "commit", "-qm", message],
        cwd=str(repo),
        check=True,
    )


def make_autodev_config(repo: Path) -> AutodevConfig:
    """Write a default .autodev/config.json with tournaments and QA gates disabled.

    QA gates are disabled so that integration tests are not blocked by
    environment-specific tooling (eslint, secretscan, etc.) that is not
    relevant to the orchestrator flow being tested.
    """
    cfg = default_config()
    cfg.platform = "claude_code"
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.qa_gates = QAGatesConfig(
        syntax_check=False,
        lint=False,
        build_check=False,
        test_runner=False,
        secretscan=False,
        sast_scan=False,
        mutation_test=False,
    )
    save_config(cfg, repo / ".autodev" / "config.json")
    return cfg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """Minimal Python repo with a single module and a test file."""
    repo = tmp_path / "python_repo"
    repo.mkdir()
    _git_init_repo(repo)

    (repo / "math_utils.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n"
    )
    (repo / "test_math_utils.py").write_text(
        "from math_utils import add\n\ndef test_add() -> None:\n    assert add(1, 2) == 3\n"
    )
    (repo / "README.md").write_text("# Python Repo\n")

    _git_commit_all(repo)
    return repo


@pytest.fixture
def tmp_git_repo_nodejs(tmp_path: Path) -> Path:
    """Minimal NodeJS/TypeScript repo."""
    repo = tmp_path / "nodejs_repo"
    repo.mkdir()
    _git_init_repo(repo)

    (repo / "package.json").write_text(
        '{\n  "name": "my-app",\n  "version": "1.0.0",\n'
        '  "scripts": {"build": "tsc"},\n'
        '  "devDependencies": {"typescript": "^5.0.0"}\n}\n'
    )
    (repo / "tsconfig.json").write_text(
        '{\n  "compilerOptions": {\n    "target": "ES2020",\n'
        '    "module": "commonjs",\n    "strict": true,\n'
        '    "outDir": "dist"\n  },\n  "include": ["src"]\n}\n'
    )
    src = repo / "src"
    src.mkdir()
    (src / "index.ts").write_text(
        'export function greet(name: string): string {\n'
        '  return `Hello, ${name}!`;\n}\n'
    )
    (repo / "README.md").write_text("# NodeJS Repo\n")

    _git_commit_all(repo)
    return repo


@pytest.fixture
def tmp_git_repo_go(tmp_path: Path) -> Path:
    """Minimal Go repo."""
    repo = tmp_path / "go_repo"
    repo.mkdir()
    _git_init_repo(repo)

    (repo / "go.mod").write_text(
        "module example.com/myapp\n\ngo 1.21\n"
    )
    (repo / "main.go").write_text(
        'package main\n\nimport "fmt"\n\nfunc main() {\n'
        '\tfmt.Println("Hello, World!")\n}\n'
    )
    (repo / "math.go").write_text(
        "package main\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n"
    )
    (repo / "README.md").write_text("# Go Repo\n")

    _git_commit_all(repo)
    return repo


@pytest.fixture
def live_mode() -> bool:
    """Return True when AUTODEV_LIVE=1 is set; skip the test otherwise."""
    enabled = os.environ.get("AUTODEV_LIVE", "").strip() == "1"
    if not enabled:
        pytest.skip("Set AUTODEV_LIVE=1 to run live integration tests")
    return True
