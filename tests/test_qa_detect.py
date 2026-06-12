"""Tests for :mod:`src.qa.detect`."""

from __future__ import annotations

from pathlib import Path


from qa.detect import (
    RUNNABLE_TEST_LANGUAGES,
    detect_language,
    detect_toolchain,
    is_repo_unbuildable,
)


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


# ---------------------------------------------------------------------------
# v0.39.0 (Cluster A2): C++/CMake detection (lowest precedence) +
# is_repo_unbuildable.
# ---------------------------------------------------------------------------


def test_detect_cpp_cmake(tmp_path: Path) -> None:
    _touch(tmp_path / "CMakeLists.txt")
    assert detect_language(tmp_path) == "cpp"


def test_detect_cpp_sln(tmp_path: Path) -> None:
    _touch(tmp_path / "Engine.sln")
    assert detect_language(tmp_path) == "cpp"


def test_detect_cpp_vcxproj(tmp_path: Path) -> None:
    _touch(tmp_path / "Engine.vcxproj")
    assert detect_language(tmp_path) == "cpp"


def test_dotnet_wins_over_sln(tmp_path: Path) -> None:
    """A .NET solution carries BOTH a ``.csproj`` and a ``.sln`` — the
    ``.csproj`` (dotnet) check must win over the lower-precedence cpp
    ``.sln`` check."""
    _touch(tmp_path / "MyApp.csproj")
    _touch(tmp_path / "MyApp.sln")
    assert detect_language(tmp_path) == "dotnet"


def test_python_wins_over_cmake(tmp_path: Path) -> None:
    """A Python repo that happens to carry a CMake tree stays python —
    cpp/CMake is lowest precedence."""
    _touch(tmp_path / "pyproject.toml")
    _touch(tmp_path / "CMakeLists.txt")
    assert detect_language(tmp_path) == "python"


def test_cpp_not_in_toolchain_map(tmp_path: Path) -> None:
    """cpp drives no linter gate yet — detect_toolchain returns None."""
    _touch(tmp_path / "CMakeLists.txt")
    assert detect_language(tmp_path) == "cpp"
    assert detect_toolchain(tmp_path) is None


def test_is_repo_unbuildable_empty(tmp_path: Path) -> None:
    """No detected language → unbuildable."""
    assert is_repo_unbuildable(tmp_path) is True


def test_is_repo_unbuildable_cpp(tmp_path: Path) -> None:
    """cpp is detected but outside RUNNABLE_TEST_LANGUAGES → unbuildable."""
    _touch(tmp_path / "CMakeLists.txt")
    assert "cpp" not in RUNNABLE_TEST_LANGUAGES
    assert is_repo_unbuildable(tmp_path) is True


def test_is_repo_unbuildable_false_for_python(tmp_path: Path) -> None:
    """A runnable language (python) → buildable."""
    _touch(tmp_path / "pyproject.toml")
    assert is_repo_unbuildable(tmp_path) is False
