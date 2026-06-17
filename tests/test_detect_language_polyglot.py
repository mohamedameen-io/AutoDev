"""G4 gate: weighted polyglot language detection + gradle-kts (WS2-4, WS2-12).

Engagement-first TDD. These assertions encode the *new* contract:

* (a) WS2-4 — a polyglot repo carrying BOTH ``pyproject.toml`` AND
  ``package.json`` with MORE JavaScript source than Python returns the
  weighted DOMINANT language (``nodejs``), NOT whatever manifest the old
  first-match scan checked first (``python``).
* (b) WS2-12 — a ``build.gradle.kts`` / ``gradlew`` / ``settings.gradle``
  repo (no ``build.gradle`` / ``pom.xml``) is detected as ``java``.
* (c) RUNNABLE membership — ``dotnet`` / ``ruby`` / ``swift`` have no
  in-environment test runner, so they MUST NOT be in
  :data:`RUNNABLE_TEST_LANGUAGES` (degrade-loud, not silent-pass), while
  ``java`` stays first-class.

RED-on-HEAD (before the fix):

* (a) first-match returns ``"python"`` (pyproject checked first).
* (b) returns ``None`` (only ``build.gradle`` / ``pom.xml`` were checked).
* (c) ``dotnet`` / ``ruby`` / ``swift`` ARE in the set today.
"""

from __future__ import annotations

from pathlib import Path

from qa.detect import (
    RUNNABLE_TEST_LANGUAGES,
    detect_language,
)


def _write(path: Path, content: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# (a) WS2-4: weighted polyglot detection — dominant-by-weight, not first-match.
# ---------------------------------------------------------------------------


def test_polyglot_js_dominant_over_python(tmp_path: Path) -> None:
    """pyproject.toml + package.json, but JS source dominates → ``nodejs``.

    First-match (the HEAD behaviour) returns ``"python"`` because
    ``pyproject.toml`` is checked first. The weighted scan must count the
    actual source and return the dominant language instead.
    """
    _write(tmp_path / "pyproject.toml", "[project]\nname='x'\n")
    _write(tmp_path / "package.json", '{"name":"x"}\n')
    # One small Python module ...
    _write(tmp_path / "app.py", "print('hi')\n")
    # ... but many TypeScript/JS modules — JS clearly dominates.
    for i in range(8):
        _write(tmp_path / f"mod{i}.ts", f"export const x{i} = {i};\n")
    assert detect_language(tmp_path) == "nodejs"


def test_polyglot_python_dominant_over_js(tmp_path: Path) -> None:
    """Same two manifests, but Python source dominates → ``python``.

    Proves the winner is driven by weight, not by a fixed manifest order
    (mirror image of the JS-dominant case)."""
    _write(tmp_path / "pyproject.toml", "[project]\nname='x'\n")
    _write(tmp_path / "package.json", '{"name":"x"}\n')
    _write(tmp_path / "index.js", "console.log('hi');\n")
    for i in range(8):
        _write(tmp_path / f"mod{i}.py", f"x{i} = {i}\n")
    assert detect_language(tmp_path) == "python"


def test_polyglot_subdir_manifests(tmp_path: Path) -> None:
    """Monorepo: manifests live in subdirs, not the root.

    Root-only first-match returns ``None`` (no root manifest). The weighted
    scan descends and picks the dominant language across the tree."""
    _write(tmp_path / "services" / "api" / "go.mod", "module x\n")
    for i in range(6):
        _write(tmp_path / "services" / "api" / f"h{i}.go", "package main\n")
    _write(tmp_path / "web" / "package.json", '{"name":"web"}\n')
    _write(tmp_path / "web" / "index.js", "console.log(1);\n")
    assert detect_language(tmp_path) == "go"


# ---------------------------------------------------------------------------
# (b) WS2-12: gradle-kts / gradlew / settings.gradle → java.
# ---------------------------------------------------------------------------


def test_detect_java_build_gradle_kts(tmp_path: Path) -> None:
    _write(tmp_path / "build.gradle.kts", "plugins { java }\n")
    assert detect_language(tmp_path) == "java"


def test_detect_java_settings_gradle(tmp_path: Path) -> None:
    _write(tmp_path / "settings.gradle", "rootProject.name = 'x'\n")
    assert detect_language(tmp_path) == "java"


def test_detect_java_settings_gradle_kts(tmp_path: Path) -> None:
    _write(tmp_path / "settings.gradle.kts", 'rootProject.name = "x"\n')
    assert detect_language(tmp_path) == "java"


def test_detect_java_gradlew(tmp_path: Path) -> None:
    _write(tmp_path / "gradlew", "#!/bin/sh\n")
    assert detect_language(tmp_path) == "java"


# ---------------------------------------------------------------------------
# (c) RUNNABLE membership: dotnet/ruby/swift are NOT runnable; java stays.
# ---------------------------------------------------------------------------


def test_dotnet_ruby_swift_not_runnable() -> None:
    """No in-environment runner for these → must degrade-loud, not silent-pass."""
    assert "dotnet" not in RUNNABLE_TEST_LANGUAGES
    assert "ruby" not in RUNNABLE_TEST_LANGUAGES
    assert "swift" not in RUNNABLE_TEST_LANGUAGES


def test_first_class_languages_runnable() -> None:
    """The five first-class languages stay runnable."""
    for lang in ("python", "nodejs", "go", "rust", "java"):
        assert lang in RUNNABLE_TEST_LANGUAGES
