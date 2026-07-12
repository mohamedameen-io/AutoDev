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
import sys
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


def resolve_target_python(cwd: Path) -> list[str]:
    """Return the argv prefix of the *target* repo's Python interpreter.

    WS2-21 / WS-6b: running a QA gate against the target repo with AutoDev's OWN
    ``sys.executable`` is a version-mismatch trap — AutoDev runs on (say) 3.13
    while the target pins 3.9, so 3.9-valid syntax (or a version-sensitive
    pure-Python tool such as ``flake8``) FALSE-FAILS on otherwise-clean code.
    Resolve the *target's* interpreter instead.

    Cascade (first match wins):

    * ``<cwd>/.venv/bin/python3`` / ``<cwd>/.venv/bin/python`` — the repo's own
      virtualenv interpreter (direct, no PATH guesswork).
    * :func:`resolve_tool` for ``python3`` then ``python`` — this also surfaces
      uv/poetry-managed interpreters (``uv run python3 -m …``) and any
      ``.venv/bin/python*`` it finds. ``resolve_tool`` returns the *bare*
      ``[tool]`` as its last-resort fallback; that is NOT a target-specific
      resolution (it would shell out to whatever ``python``/``python3`` is on
      PATH, defeating the point), so we ignore the bare form and keep cascading.
    * ``sys.executable`` — only when nothing target-specific resolves (a repo
      with no venv / no lockfile manager): AutoDev's interpreter is the best
      available, matching the legacy behaviour for that case.
    """
    for direct in (".venv/bin/python3", ".venv/bin/python"):
        candidate = cwd / direct
        if candidate.exists():
            return [str(candidate)]
    for tool in ("python3", "python"):
        argv = resolve_tool(cwd, tool)
        # Skip the bare ``[tool]`` last-resort — only accept a managed/venv form.
        if argv != [tool]:
            return argv
    return [sys.executable]


def resolve_python_tool(cwd: Path, tool: str) -> list[str]:
    """Return the argv prefix to invoke a *pure-Python* QA *tool* inside *cwd*,
    preferring the TARGET repo's own interpreter over AutoDev's host.

    WS-6b: a version-sensitive pure-Python tool (notably ``flake8``, also
    ``pytest``) crashes / false-fails when run under AutoDev's host interpreter
    (py3.13) against a repo pinned to an older Python. :func:`resolve_tool`
    already prefers the target's ``.venv/bin/<tool>`` / ``uv run`` /
    ``poetry run``; its only host-bound outcome is the bare ``[tool]`` last
    resort. When we hit that last resort but the target repo DOES expose its own
    interpreter (a ``.venv`` python), run the tool as a module under that
    interpreter (``<target-python> -m <tool>``) so it executes on the target's
    Python, not the host's.

    Cascade (first match wins):

    * :func:`resolve_tool` result when it is target-specific (anything other
      than the bare ``[tool]``) — e.g. ``.venv/bin/<tool>``, ``uv run <tool>``.
    * ``[*resolve_target_python(cwd), "-m", tool]`` when a target ``.venv``
      interpreter exists (see :func:`resolve_target_python`).
    * the bare ``[tool]`` — no target env resolvable → unchanged host behaviour
      (matches the legacy fallback / graceful skip).

    NOTE: intended for pure-Python tools only. A self-contained binary such as
    ``ruff`` (version-agnostic) has no interpreter-mismatch problem and must NOT
    be routed through ``-m`` — call :func:`resolve_tool` for those.
    """
    argv = resolve_tool(cwd, tool)
    if argv != [tool]:
        return argv
    target_python = resolve_target_python(cwd)
    if target_python != [sys.executable]:
        return [*target_python, "-m", tool]
    return [tool]


def detect_js_package_manager(cwd: Path) -> str:
    """Return the JS package manager the repo at *cwd* uses.

    Resolution is lockfile-driven (the lockfile is the authoritative record of
    which manager actually installed ``node_modules``), first match wins:

    * ``pnpm-lock.yaml`` → ``"pnpm"``
    * ``yarn.lock``      → ``"yarn"``
    * ``package-lock.json`` (or no lockfile at all) → ``"npm"``

    npm is the default because it is the universally-available baseline shipped
    with Node itself, so a repo with no lockfile is still runnable.
    """
    if (cwd / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (cwd / "yarn.lock").exists():
        return "yarn"
    return "npm"


def resolve_js_tool(cwd: Path, tool: str) -> list[str]:
    """Return the argv prefix used to invoke a JS *tool* inside the repo at *cwd*.

    The JS gates (eslint, tsc, …) previously hardcoded ``npx``/``npm``, which
    ignored project-local installs and non-npm workspaces. This resolver fixes
    both (WS2-20).

    Cascade (first match wins):

    * ``node_modules/.bin/<tool>`` exists → run the project-pinned binary
      directly (no package-manager indirection, deterministic, offline-safe).
    * lockfile names a workspace manager available on PATH →
      ``pnpm exec <tool>`` / ``yarn exec <tool>`` / ``npm exec <tool>``
      (per :func:`detect_js_package_manager`).
    * ``npx`` on PATH → ``npx --no-install <tool>`` (uses a local/cached copy
      only; never auto-installs).
    * otherwise → the bare ``[<tool>]`` (relies on PATH / graceful skip).
    """
    local_bin = cwd / "node_modules" / ".bin" / tool
    if local_bin.exists():
        return [str(local_bin)]

    manager = detect_js_package_manager(cwd)
    if shutil.which(manager):
        # ``<pm> exec <tool>`` runs the project-local binary across all three
        # managers (npm>=7, yarn classic+berry, pnpm), respecting workspace
        # hoisting that a bare ``node_modules/.bin`` lookup may miss.
        return [manager, "exec", tool]

    if shutil.which("npx"):
        return ["npx", "--no-install", tool]
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


__all__ = [
    "detect_js_package_manager",
    "detect_python_linter",
    "resolve_js_tool",
    "resolve_python_tool",
    "resolve_target_python",
    "resolve_tool",
]
