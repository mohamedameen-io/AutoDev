"""Tests for :mod:`src.qa.detect`."""

from __future__ import annotations

from pathlib import Path


from qa.detect import detect_language, detect_toolchain


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def test_detect_python_pyproject(tmp_path: Path) -> None:
    _touch(tmp_path / "pyproject.toml")
    assert detect_language(tmp_path) == "python"


def test_detect_python_setup_py(tmp_path: Path) -> None:
    _touch(tmp_path / "setup.py")
    assert detect_language(tmp_path) == "python"


def test_detect_nodejs(tmp_path: Path) -> None:
    _touch(tmp_path / "package.json")
    assert detect_language(tmp_path) == "nodejs"


def test_detect_rust(tmp_path: Path) -> None:
    _touch(tmp_path / "Cargo.toml")
    assert detect_language(tmp_path) == "rust"


def test_detect_go(tmp_path: Path) -> None:
    _touch(tmp_path / "go.mod")
    assert detect_language(tmp_path) == "go"


def test_detect_java_pom(tmp_path: Path) -> None:
    _touch(tmp_path / "pom.xml")
    assert detect_language(tmp_path) == "java"


def test_detect_java_gradle(tmp_path: Path) -> None:
    _touch(tmp_path / "build.gradle")
    assert detect_language(tmp_path) == "java"


def test_detect_dotnet(tmp_path: Path) -> None:
    _touch(tmp_path / "MyApp.csproj")
    assert detect_language(tmp_path) == "dotnet"


def test_detect_ruby(tmp_path: Path) -> None:
    _touch(tmp_path / "Gemfile")
    assert detect_language(tmp_path) == "ruby"


def test_detect_swift(tmp_path: Path) -> None:
    _touch(tmp_path / "main.swift")
    assert detect_language(tmp_path) == "swift"


def test_detect_unknown(tmp_path: Path) -> None:
    assert detect_language(tmp_path) is None


def test_detect_python_takes_priority_over_nodejs(tmp_path: Path) -> None:
    """pyproject.toml wins over package.json (detection order)."""
    _touch(tmp_path / "pyproject.toml")
    _touch(tmp_path / "package.json")
    assert detect_language(tmp_path) == "python"


def test_toolchain_python(tmp_path: Path) -> None:
    _touch(tmp_path / "pyproject.toml")
    assert detect_toolchain(tmp_path) == "ruff"


def test_toolchain_nodejs(tmp_path: Path) -> None:
    _touch(tmp_path / "package.json")
    assert detect_toolchain(tmp_path) == "eslint"


def test_toolchain_rust(tmp_path: Path) -> None:
    _touch(tmp_path / "Cargo.toml")
    assert detect_toolchain(tmp_path) == "cargo"


def test_toolchain_go(tmp_path: Path) -> None:
    _touch(tmp_path / "go.mod")
    assert detect_toolchain(tmp_path) == "golangci-lint"


def test_toolchain_java_gradle(tmp_path: Path) -> None:
    _touch(tmp_path / "build.gradle")
    assert detect_toolchain(tmp_path) == "gradle"


def test_toolchain_java_pom(tmp_path: Path) -> None:
    _touch(tmp_path / "pom.xml")
    assert detect_toolchain(tmp_path) == "maven"


def test_toolchain_unknown(tmp_path: Path) -> None:
    assert detect_toolchain(tmp_path) is None
