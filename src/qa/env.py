"""Target-environment tool resolution for QA gates.

The lint and test gates must run the *target* project's own tooling, not
whatever happens to be on AutoDev's ``PATH``. This module resolves how to
invoke a tool (e.g. ``ruff``, ``flake8``, ``pytest``) inside the target repo
and which Python linter that repo actually uses.

Resolution is purely filesystem/manifest driven so it stays deterministic and
cheap; no subprocesses are spawned here.
"""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path


def _load_pyproject(cwd: Path) -> dict[str, object]:
    """Parse ``pyproject.toml`` in *cwd*, returning ``{}`` on any failure.

    Missing file, unreadable file, and malformed TOML are all treated as
    "no configuration present" so callers can fall back to defaults.
    """
    path = cwd / "pyproject.toml"
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _has_dev_group(cwd: Path) -> bool:
    """True when ``pyproject.toml`` declares a ``[dependency-groups]`` ``dev`` key."""
    data = _load_pyproject(cwd)
    groups = data.get("dependency-groups")
    if not isinstance(groups, dict):
        return False
    return "dev" in groups


def _section_in_ini(path: Path, section: str) -> bool:
    """True when *path* contains an INI section header ``[section]``.

    Uses a simple line scan (no configparser) so unrelated parse errors in the
    file don't matter. File-read errors are treated as "section absent".
    """
    header = f"[{section}]"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(line.strip() == header for line in text.splitlines())


def resolve_tool(cwd: Path, tool: str) -> list[str]:
    """Return the argv prefix used to invoke *tool* inside the repo at *cwd*.

    Cascade (first match wins):

    * ``.venv/bin/<tool>`` exists → run it directly.
    * ``uv.lock`` present and ``uv`` on PATH → ``uv run [--group dev] <tool>``
      (the ``--group dev`` is added only when ``pyproject.toml`` declares a
      ``[dependency-groups]`` ``dev`` table).
    * ``poetry.lock`` present and ``poetry`` on PATH → ``poetry run <tool>``.
    * otherwise → the bare ``[<tool>]`` (relies on PATH / graceful skip).
    """
    venv_bin = cwd / ".venv" / "bin" / tool
    if venv_bin.exists():
        return [str(venv_bin)]
    if (cwd / "uv.lock").exists() and shutil.which("uv"):
        dev_group = ["--group", "dev"] if _has_dev_group(cwd) else []
        return ["uv", "run", *dev_group, tool]
    if (cwd / "poetry.lock").exists() and shutil.which("poetry"):
        return ["poetry", "run", tool]
    return [tool]


def detect_python_linter(cwd: Path) -> str:
    """Return the Python linter the repo at *cwd* uses: ``"ruff"`` or ``"flake8"``.

    ruff is detected first (and wins ties), then flake8, defaulting to ruff to
    preserve historical behaviour.
    """
    data = _load_pyproject(cwd)
    tool_table = data.get("tool")
    tool_table = tool_table if isinstance(tool_table, dict) else {}

    if (
        (cwd / "ruff.toml").exists()
        or (cwd / ".ruff.toml").exists()
        or "ruff" in tool_table
    ):
        return "ruff"

    if (
        (cwd / ".flake8").exists()
        or _section_in_ini(cwd / "setup.cfg", "flake8")
        or _section_in_ini(cwd / "tox.ini", "flake8")
        or "flake8" in tool_table
    ):
        return "flake8"

    return "ruff"


__all__ = ["detect_python_linter", "resolve_tool"]
