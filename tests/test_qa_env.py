"""Tests for :mod:`src.qa.env`."""

from __future__ import annotations

from pathlib import Path

import pytest

from qa.env import detect_python_linter, resolve_tool


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


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
