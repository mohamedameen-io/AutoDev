"""Shared pytest fixtures."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from config.defaults import default_config
from config.loader import save_config
from config.schema import AutodevConfig, QAGatesConfig


@pytest.fixture(autouse=True)
def _resolver_disabled_by_default(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v0.42.0 (ADR-0047): scope the Universal Blocker Resolver OFF by default
    in the test suite.

    The resolver actively recovers at terminal failure sites (re-enabling tasks
    instead of blocking) — an intentional behavior change. The legacy suite
    encodes the pre-resolver block behavior, so the resolver *chokepoint*
    (``execute_phase._maybe_resolve_blocker``, which honours
    ``AUTODEV_RESOLVER_DISABLED``) is disabled here to give every legacy test
    EXACT pre-resolver behavior — true fail-safe + no recovery loops. The
    resolver's own logic is covered directly by ``test_blocker_resolver.py``
    (which calls ``resolve_blocker`` without the env gate), and the wiring is
    covered by tests marked ``@pytest.mark.resolver_enabled`` (which opt back
    in). Production / the CLI / the benchmark do NOT set this env var, so the
    resolver is ON there.

    Two env vars participate here, and the distinction is the whole point of the
    N2 release-gate broken-control (Phase 0 / B6):

    * ``AUTODEV_RESOLVER_DISABLED`` — the suite's INTERNAL default-off knob. It
      is fully *auto-managed* by this fixture: for ``resolver_enabled`` tests it
      is force-DELETED (resolver ON), otherwise force-SET (resolver OFF). An
      external value is therefore NEUTRALIZED — which is exactly why it cannot
      serve as the release broken-control.
    * ``AUTODEV_RESOLVER_FORCE_DISABLED`` — the OPERATOR override. When set to
      ``"1"`` this fixture does NOT auto-unset the resolver even for
      ``resolver_enabled`` tests; instead it forces the resolver OFF for the
      WHOLE suite. This is the valid N2 broken-control: it turns the engagement
      assertions (e.g. ``test_resolver_reached_from_real_loop``) RED, proving
      the engagement gate fires on the planted disable rather than passing
      vacuously.

    Backward-compatible: when ``AUTODEV_RESOLVER_FORCE_DISABLED`` is unset,
    behavior is identical to the prior implementation.
    """
    force_off = os.environ.get("AUTODEV_RESOLVER_FORCE_DISABLED") == "1"
    if request.node.get_closest_marker("resolver_enabled") and not force_off:
        monkeypatch.delenv("AUTODEV_RESOLVER_DISABLED", raising=False)
    else:
        monkeypatch.setenv("AUTODEV_RESOLVER_DISABLED", "1")


@pytest.fixture
def tmp_project_dir(tmp_path: Path) -> Path:
    """Create a fresh project dir with `.autodev/config.json` from defaults."""
    cfg_path = tmp_path / ".autodev" / "config.json"
    save_config(default_config(), cfg_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Integration test helpers
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
    """Write a default .autodev/config.json with tournaments and QA gates disabled."""
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
        "export function greet(name: string): string {\n"
        "  return `Hello, ${name}!`;\n}\n"
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

    (repo / "go.mod").write_text("module example.com/myapp\n\ngo 1.21\n")
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
