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
    * ``*.sln`` / ``*.vcxproj`` → ``"cpp"`` (lowest precedence)
    * ``CMakeLists.txt`` → ``"cpp"`` (lowest precedence)

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
    # v0.39.0 (Cluster A2): C++/CMake at the LOWEST precedence. A .NET
    # solution also carries a ``*.sln``, and Python/Node repos may sit
    # alongside a CMake tree — both must keep winning, so these checks
    # come last, just before the final ``return None``.
    if list(cwd.glob("*.sln")) or list(cwd.glob("*.vcxproj")):
        return "cpp"
    if (cwd / "CMakeLists.txt").exists():
        return "cpp"
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


# v0.39.0 (Cluster A2): languages whose tests AutoDev can actually build
# and run in-environment. A *detected* language outside this set (e.g.
# ``"cpp"``, which requires a configured native toolchain we don't drive)
# means "tests are not runnable here" → the huge-repo profile / runtime
# fallback can soft-pass an infra-class capture failure rather than treat
# it as a code defect. Kept deliberately separate from ``detect_language``
# so ``"cpp"`` stays useful for indexing / sparse logic.
RUNNABLE_TEST_LANGUAGES = {
    "python",
    "nodejs",
    "rust",
    "go",
    "java",
    "dotnet",
    "ruby",
    "swift",
}


def is_repo_unbuildable(cwd: Path) -> bool:
    """True when AutoDev cannot build/run this repo's tests in-environment.

    Returns ``True`` when there is no detected language, or the detected
    language is outside :data:`RUNNABLE_TEST_LANGUAGES` (e.g. ``"cpp"``).
    """
    lang = detect_language(cwd)
    return lang is None or lang not in RUNNABLE_TEST_LANGUAGES


__all__ = [
    "RUNNABLE_TEST_LANGUAGES",
    "detect_language",
    "detect_toolchain",
    "is_repo_unbuildable",
]
