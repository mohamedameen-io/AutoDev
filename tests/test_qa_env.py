"""Tests for :mod:`src.qa.env`."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from qa.env import (
    detect_python_linter,
    resolve_python_tool,
    resolve_target_python,
    resolve_tool,
)


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def _make_venv_python(cwd: Path, name: str = "python3") -> Path:
    """Create a fake target ``.venv/bin/<name>`` interpreter and return its path."""
    interp = cwd / ".venv" / "bin" / name
    _touch(interp)
    return interp


# ---------------------------------------------------------------------------
# resolve_tool
# ---------------------------------------------------------------------------


def test_resolve_tool_venv_bin(tmp_path: Path) -> None:
    venv_bin = tmp_path / ".venv" / "bin" / "pytest"
    _touch(venv_bin)
    assert resolve_tool(tmp_path, "pytest") == [str(venv_bin)]


def test_resolve_tool_uv_with_dev_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        "[dependency-groups]\ndev = [\"pytest\"]\n", encoding="utf-8"
    )
    monkeypatch.setattr("qa.env.shutil.which", lambda _name: "/usr/bin/uv")
    assert resolve_tool(tmp_path, "pytest") == ["uv", "run", "--group", "dev", "pytest"]


def test_resolve_tool_uv_without_dev_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    # pyproject present but no dependency-groups.dev table.
    (tmp_path / "pyproject.toml").write_text("[project]\nname = \"x\"\n", encoding="utf-8")
    monkeypatch.setattr("qa.env.shutil.which", lambda _name: "/usr/bin/uv")
    assert resolve_tool(tmp_path, "pytest") == ["uv", "run", "pytest"]


def test_resolve_tool_uv_no_pyproject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr("qa.env.shutil.which", lambda _name: "/usr/bin/uv")
    assert resolve_tool(tmp_path, "pytest") == ["uv", "run", "pytest"]


def test_resolve_tool_poetry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "poetry.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr("qa.env.shutil.which", lambda _name: "/usr/bin/poetry")
    assert resolve_tool(tmp_path, "pytest") == ["poetry", "run", "pytest"]


def test_resolve_tool_bare(tmp_path: Path) -> None:
    assert resolve_tool(tmp_path, "pytest") == ["pytest"]


def test_resolve_tool_venv_beats_uv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv_bin = tmp_path / ".venv" / "bin" / "pytest"
    _touch(venv_bin)
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr("qa.env.shutil.which", lambda _name: "/usr/bin/uv")
    assert resolve_tool(tmp_path, "pytest") == [str(venv_bin)]


# ---------------------------------------------------------------------------
# detect_python_linter
# ---------------------------------------------------------------------------


def test_detect_linter_tool_ruff_table(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    assert detect_python_linter(tmp_path) == "ruff"


def test_detect_linter_ruff_toml(tmp_path: Path) -> None:
    (tmp_path / "ruff.toml").write_text("", encoding="utf-8")
    assert detect_python_linter(tmp_path) == "ruff"


def test_detect_linter_dot_ruff_toml(tmp_path: Path) -> None:
    (tmp_path / ".ruff.toml").write_text("", encoding="utf-8")
    assert detect_python_linter(tmp_path) == "ruff"


def test_detect_linter_dot_flake8(tmp_path: Path) -> None:
    (tmp_path / ".flake8").write_text("[flake8]\nmax-line-length = 100\n", encoding="utf-8")
    assert detect_python_linter(tmp_path) == "flake8"


def test_detect_linter_setup_cfg_flake8(tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_text("[flake8]\nmax-line-length = 100\n", encoding="utf-8")
    assert detect_python_linter(tmp_path) == "flake8"


def test_detect_linter_tox_ini_flake8(tmp_path: Path) -> None:
    (tmp_path / "tox.ini").write_text("[flake8]\nmax-line-length = 100\n", encoding="utf-8")
    assert detect_python_linter(tmp_path) == "flake8"


def test_detect_linter_tool_flake8_table(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.flake8]\nmax-line-length = 100\n", encoding="utf-8")
    assert detect_python_linter(tmp_path) == "flake8"


def test_detect_linter_default_ruff(tmp_path: Path) -> None:
    assert detect_python_linter(tmp_path) == "ruff"


def test_detect_linter_ruff_beats_flake8(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    (tmp_path / ".flake8").write_text("[flake8]\n", encoding="utf-8")
    assert detect_python_linter(tmp_path) == "ruff"


# ---------------------------------------------------------------------------
# resolve_target_python (WS-6b: promoted from qa.build_check into the shared env)
# ---------------------------------------------------------------------------


def test_resolve_target_python_prefers_venv(tmp_path: Path) -> None:
    interp = _make_venv_python(tmp_path, "python3")
    assert resolve_target_python(tmp_path) == [str(interp)]


def test_resolve_target_python_prefers_python3_over_python(tmp_path: Path) -> None:
    py3 = _make_venv_python(tmp_path, "python3")
    _make_venv_python(tmp_path, "python")
    assert resolve_target_python(tmp_path) == [str(py3)]


def test_resolve_target_python_falls_back_to_sys_executable(tmp_path: Path) -> None:
    assert resolve_target_python(tmp_path) == [sys.executable]


def test_resolve_target_python_uv_managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A uv-managed target (uv.lock, no ``.venv``) resolves to a uv-run
    interpreter, not AutoDev's host ``sys.executable``."""
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr("qa.env.shutil.which", lambda _name: "/usr/bin/uv")
    argv = resolve_target_python(tmp_path)
    assert argv[:2] == ["uv", "run"]
    assert sys.executable not in argv


# ---------------------------------------------------------------------------
# resolve_python_tool (WS-6b: run a pure-Python tool under the TARGET interpreter)
# ---------------------------------------------------------------------------


def test_resolve_python_tool_prefers_venv_bin(tmp_path: Path) -> None:
    """When the venv exposes the tool directly, use it (no ``-m`` indirection)."""
    venv_tool = tmp_path / ".venv" / "bin" / "flake8"
    _touch(venv_tool)
    assert resolve_python_tool(tmp_path, "flake8") == [str(venv_tool)]


def test_resolve_python_tool_bare_when_no_target_env(tmp_path: Path) -> None:
    """No venv / no lockfile manager → the bare host tool (unchanged behaviour)."""
    assert resolve_python_tool(tmp_path, "flake8") == ["flake8"]


def test_resolve_python_tool_uses_target_python_module_when_only_venv_python(
    tmp_path: Path,
) -> None:
    """WS-6b core fix: a target ``.venv`` python exists but NOT ``.venv/bin/flake8``
    → run the tool as a module UNDER the target interpreter (``python -m flake8``),
    never the bare host tool (which would run under AutoDev's py3.13 and crash
    flake8)."""
    interp = _make_venv_python(tmp_path, "python3")
    assert resolve_python_tool(tmp_path, "flake8") == [str(interp), "-m", "flake8"]


def test_resolve_python_tool_uv_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A uv-managed repo resolves the tool via ``uv run`` (target-specific, no ``-m``)."""
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr("qa.env.shutil.which", lambda _name: "/usr/bin/uv")
    assert resolve_python_tool(tmp_path, "pytest") == ["uv", "run", "pytest"]
