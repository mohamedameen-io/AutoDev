"""Language and toolchain detection for QA gates.

Detects the primary language of a project by inspecting manifest files in the
given working directory. Returns ``None`` when no known manifest is found.
"""

from __future__ import annotations

from pathlib import Path


def detect_language(cwd: Path) -> str | None:
    """Return the primary language of the project rooted at *cwd*.

    Detection order (first match wins):

    * ``pyproject.toml`` or ``setup.py`` → ``"python"``
    * ``package.json`` → ``"nodejs"``
    * ``Cargo.toml`` → ``"rust"``
    * ``go.mod`` → ``"go"``
    * ``pom.xml`` or ``build.gradle`` → ``"java"``
    * ``*.csproj`` → ``"dotnet"``
    * ``Gemfile`` → ``"ruby"``
    * ``*.swift`` → ``"swift"``

    Returns ``None`` when no manifest is found.
    """
    if (cwd / "pyproject.toml").exists() or (cwd / "setup.py").exists():
        return "python"
    if (cwd / "package.json").exists():
        return "nodejs"
    if (cwd / "Cargo.toml").exists():
        return "rust"
    if (cwd / "go.mod").exists():
        return "go"
    if (cwd / "pom.xml").exists() or (cwd / "build.gradle").exists():
        return "java"
    if list(cwd.glob("*.csproj")):
        return "dotnet"
    if (cwd / "Gemfile").exists():
        return "ruby"
    if list(cwd.glob("*.swift")):
        return "swift"
    return None


def detect_toolchain(cwd: Path) -> str | None:
    """Return the primary lint/build tool for the project rooted at *cwd*.

    Maps language → canonical tool name:

    * ``python`` → ``"ruff"``
    * ``nodejs`` → ``"eslint"``
    * ``rust`` → ``"cargo"``
    * ``go`` → ``"golangci-lint"``
    * ``java`` → ``"maven"`` (``pom.xml`` preferred) or ``"gradle"``
    * ``dotnet`` → ``"dotnet"``
    * ``ruby`` → ``"rubocop"``
    * ``swift`` → ``"swiftlint"``

    Returns ``None`` when the language cannot be detected.
    """
    language = detect_language(cwd)
    _toolchain_map: dict[str, str] = {
        "python": "ruff",
        "nodejs": "eslint",
        "rust": "cargo",
        "go": "golangci-lint",
        "java": "maven",
        "dotnet": "dotnet",
        "ruby": "rubocop",
        "swift": "swiftlint",
    }
    if language is None:
        return None
    # For java, prefer gradle when build.gradle is present.
    if language == "java" and (cwd / "build.gradle").exists():
        return "gradle"
    return _toolchain_map.get(language)


__all__ = ["detect_language", "detect_toolchain"]
