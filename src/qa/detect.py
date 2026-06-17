"""Language and toolchain detection for QA gates.

Detects the primary language of a project by inspecting manifest files and
source extensions in the given working directory. Returns ``None`` when no
known signal is found.

v1.0 (Phase 2, WS2-4 / WS2-12): detection is now **weighted** rather than
first-match. A polyglot / monorepo repo returns the *dominant* language by
combined manifest + source weight, not whichever manifest happened to be
checked first. The scan also descends into subdirectories so monorepos whose
manifests live under ``services/`` / ``web/`` are detected (root-only first-
match missed those entirely). Gradle-Kotlin / wrapper builds
(``build.gradle.kts`` / ``settings.gradle[.kts]`` / ``gradlew``) are now
recognised as ``java``.

The output vocabulary is deliberately the *detection* vocabulary
(``python`` / ``nodejs`` / ``rust`` / ``go`` / ``java`` / ``dotnet`` /
``ruby`` / ``swift`` / ``cpp``) that the downstream QA runners
(:mod:`qa.test_runner`, :mod:`qa.build_check`) key off — NOT the finer-grained
``typescript`` / ``javascript`` split that :mod:`runtime.language_profile`
uses for indexing. Extension weights below are aligned with
``runtime.language_profile.EXTENSION_WEIGHTS`` but collapsed into this
coarser vocabulary.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path, PurePosixPath

from runtime.repo_probe import iter_repo_files

# Per-extension (detection-language, weight). Aligned with
# ``runtime.language_profile.EXTENSION_WEIGHTS`` but collapsed into the
# detection vocabulary: ``typescript`` / ``javascript`` → ``nodejs`` and
# ``c`` → ``cpp`` (the QA runners have no separate C path). A single ``.py``
# (weight 100) outvotes a single ``.go`` / ``.rs`` (weight 50) — denser
# signal per file — which keeps the single-file/manifest-only golden cases
# resolving the same way they did under first-match.
_EXTENSION_WEIGHTS: dict[str, tuple[str, int]] = {
    ".ts": ("nodejs", 50),
    ".tsx": ("nodejs", 50),
    ".js": ("nodejs", 40),
    ".jsx": ("nodejs", 40),
    ".mjs": ("nodejs", 30),
    ".cjs": ("nodejs", 30),
    ".py": ("python", 100),
    ".cpp": ("cpp", 80),
    ".cc": ("cpp", 80),
    ".cxx": ("cpp", 80),
    ".h": ("cpp", 60),
    ".hpp": ("cpp", 60),
    ".c": ("cpp", 80),
    ".java": ("java", 70),
    ".kt": ("java", 70),
    ".kts": ("java", 70),
    ".go": ("go", 50),
    ".rs": ("rust", 50),
    ".cs": ("dotnet", 60),
    ".rb": ("ruby", 50),
    ".swift": ("swift", 50),
}

# Per-manifest (detection-language, weight). Manifests are a *strong* signal
# (much heavier than any single source file) because their presence is a
# deliberate declaration of the project's primary toolchain. The relative
# weights also encode the precedence that the legacy first-match order used
# to express, so the existing golden cases still resolve identically:
#
#   * dotnet (``*.csproj`` / ``*.sln`` carries a ``.csproj``) beats the
#     lower-precedence cpp ``*.sln`` / ``*.vcxproj`` signal.
#   * python / nodejs / java manifests beat a co-located CMake tree.
#   * cpp project/CMake signals sit lowest, just above raw extension counts.
#
# A manifest contributes its weight ONCE regardless of how many copies exist
# in the tree (counting copies would let a monorepo's many ``package.json``
# shards drown out an otherwise-dominant language); source *extensions* carry
# the per-file volume signal instead.
_MANIFEST_WEIGHTS: dict[str, tuple[str, int]] = {
    # python manifests carry a hair MORE weight than the other manifests so
    # an *ambiguous*, source-free polyglot repo (bare ``pyproject.toml`` +
    # ``package.json``, no source files) resolves to ``python`` — matching
    # the legacy golden default. Real source volume (a single ``.ts`` adds
    # 50, a ``.py`` adds 100) still overrides this nudge, so a JS-dominated
    # tree correctly flips to ``nodejs`` (that is the whole point of WS2-4).
    "pyproject.toml": ("python", 1010),
    "setup.py": ("python", 1010),
    "package.json": ("nodejs", 1000),
    "Cargo.toml": ("rust", 1000),
    "go.mod": ("go", 1000),
    "pom.xml": ("java", 1000),
    "build.gradle": ("java", 1000),
    # WS2-12: Gradle-Kotlin DSL + settings + wrapper. A modern Gradle
    # project may ship ONLY these (no ``build.gradle`` / ``pom.xml``).
    "build.gradle.kts": ("java", 1000),
    "settings.gradle": ("java", 1000),
    "settings.gradle.kts": ("java", 1000),
    "gradlew": ("java", 1000),
    "Gemfile": ("ruby", 1000),
    # cpp signals sit BELOW dotnet so a ``*.sln`` accompanying a ``*.csproj``
    # does not steal the win (see ``*.csproj`` glob below).
    "CMakeLists.txt": ("cpp", 300),
}

# Manifests matched by glob rather than exact name. Same one-shot weighting.
# ``*.csproj`` (dotnet) outweighs the cpp ``*.sln`` / ``*.vcxproj`` so a .NET
# solution (which carries both) resolves to dotnet, matching the legacy
# precedence.
_MANIFEST_GLOBS: tuple[tuple[str, str, int], ...] = (
    ("*.csproj", "dotnet", 1000),
    ("*.swift", "swift", 500),
    ("*.sln", "cpp", 300),
    ("*.vcxproj", "cpp", 300),
)


# G7-detect: a submodule path declaration in ``.gitmodules`` looks like
# ``path = vendor/sub`` (leading whitespace + ``path`` key). The host repo's
# language must reflect the HOST tree only — a checked-out submodule's working
# tree (its own ``package.json`` / ``*.ts`` / ``Cargo.toml`` ...) is FOREIGN
# and must not flip the host's weighted scan. ``git ls-files`` already shields
# git repos (a submodule appears as a single gitlink, not its contents), but
# the ``os.walk`` fallback in :func:`runtime.repo_probe.iter_repo_files`
# descends into the submodule working tree, so we must exclude those subtrees
# explicitly here.
_GITMODULES_PATH_RE = re.compile(r"^\s*path\s*=\s*(.+?)\s*$", re.MULTILINE)


def _submodule_prefixes(cwd: Path) -> tuple[tuple[str, ...], ...]:
    """Return submodule path prefixes (posix parts) to exclude from the scan.

    Sources:

    * ``.gitmodules`` — every ``path = <dir>`` declaration (the host repo's
      authoritative manifest of which subtrees are foreign submodules).
    * ``.git/modules`` — git's internal submodule object/work store. Belt-and-
      suspenders: the ``os.walk`` fallback already skips ``.git`` via
      :data:`runtime.repo_probe._SKIP_DIRS`, but the git fast-path lists
      tracked files only, so this prefix is harmless there and defends any
      non-standard walk.

    Returns a tuple of path-part tuples (e.g. ``(("vendor", "sub"),)``) so the
    caller can do a cheap prefix-match against each scanned file's relative
    parts. An unreadable / absent ``.gitmodules`` yields the ``.git/modules``
    prefix only.
    """
    prefixes: list[tuple[str, ...]] = [(".git", "modules")]
    gitmodules = cwd / ".gitmodules"
    try:
        text = gitmodules.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return tuple(prefixes)
    for raw in _GITMODULES_PATH_RE.findall(text):
        rel = raw.strip().strip("/")
        if not rel:
            continue
        parts = tuple(p for p in PurePosixPath(rel).parts if p not in (".", ""))
        if parts:
            prefixes.append(parts)
    return tuple(prefixes)


def _under_prefix(parts: tuple[str, ...], prefixes: tuple[tuple[str, ...], ...]) -> bool:
    """True when *parts* (a relative file's path parts) sits under any prefix."""
    for pref in prefixes:
        if len(parts) >= len(pref) and parts[: len(pref)] == pref:
            return True
    return False


def _score_languages(cwd: Path) -> dict[str, float]:
    """Return a ``{detection-language: weight}`` map for the repo at *cwd*.

    Combines one-shot manifest signals (strong, presence-based) with
    per-file source-extension signals (volume-based), scanning the whole
    tree so monorepo subdir manifests are seen too. An empty map means no
    known signal was found.

    G7-detect: git submodule subtrees (declared in ``.gitmodules``, plus
    ``.git/modules``) are EXCLUDED so a submodule's foreign manifests /
    sources cannot flip the HOST repo's detected language.
    """
    scores: dict[str, float] = defaultdict(float)

    submodule_prefixes = _submodule_prefixes(cwd)

    # Manifest + source-extension signals, in a single pass over the tree.
    seen_manifests: set[str] = set()
    seen_globs: set[str] = set()
    for fp in iter_repo_files(cwd):
        # G7-detect: skip files that live under a declared submodule subtree.
        if submodule_prefixes:
            try:
                rel_parts = fp.relative_to(cwd).parts
            except ValueError:
                rel_parts = fp.parts
            if _under_prefix(rel_parts, submodule_prefixes):
                continue
        name = fp.name
        # 1) Manifest signal (one-shot per manifest kind, anywhere in tree).
        info = _MANIFEST_WEIGHTS.get(name)
        if info is not None and name not in seen_manifests:
            lang, weight = info
            scores[lang] += float(weight)
            seen_manifests.add(name)
        suffix = fp.suffix.lower()
        for pattern, glang, gweight in _MANIFEST_GLOBS:
            ext = pattern[1:]  # ``*.csproj`` -> ``.csproj``
            if suffix == ext and pattern not in seen_globs:
                scores[glang] += float(gweight)
                seen_globs.add(pattern)
        # 2) Source-extension volume signal.
        ext_info = _EXTENSION_WEIGHTS.get(suffix)
        if ext_info is not None:
            elang, eweight = ext_info
            scores[elang] += float(eweight)

    return dict(scores)


def _neg_key(lang: str) -> tuple[int, ...]:
    """Tie-break key: smaller (earlier-alphabetical) names sort *higher*.

    ``max`` wants the larger key to win; we want the alphabetically-earliest
    language to win ties, so invert each codepoint.
    """
    return tuple(-ord(ch) for ch in lang)


def detect_language(cwd: Path) -> str | None:
    """Return the primary (dominant) language of the project rooted at *cwd*.

    Detection is **weighted**: every manifest and source file in the tree
    contributes to a per-language score and the highest-scoring language
    wins. Manifests dominate single source files, so a manifest-only repo
    still resolves to that manifest's language; a polyglot repo carrying two
    manifests (e.g. ``pyproject.toml`` AND ``package.json``) resolves to the
    language whose *source* dominates.

    Recognised manifests / extensions (collapsed to the QA-runner
    vocabulary):

    * ``pyproject.toml`` / ``setup.py`` / ``*.py`` → ``"python"``
    * ``package.json`` / ``*.ts`` / ``*.js`` / ... → ``"nodejs"``
    * ``Cargo.toml`` / ``*.rs`` → ``"rust"``
    * ``go.mod`` / ``*.go`` → ``"go"``
    * ``pom.xml`` / ``build.gradle`` / ``build.gradle.kts`` /
      ``settings.gradle[.kts]`` / ``gradlew`` / ``*.java`` / ``*.kt`` →
      ``"java"``
    * ``*.csproj`` / ``*.cs`` → ``"dotnet"``
    * ``Gemfile`` / ``*.rb`` → ``"ruby"``
    * ``*.swift`` → ``"swift"``
    * ``CMakeLists.txt`` / ``*.sln`` / ``*.vcxproj`` / ``*.cpp`` / ... →
      ``"cpp"`` (lowest manifest precedence)

    Ties are broken deterministically by language name (alphabetical) so the
    result is stable; in practice manifest weights make ties between
    distinct manifests impossible. Returns ``None`` when no signal is found.
    """
    scores = _score_languages(cwd)
    if not scores:
        return None
    # max() picks the highest score; tie-break alphabetically for determinism.
    return max(scores, key=lambda lang: (scores[lang], _neg_key(lang)))


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
    # For java, prefer gradle when ANY gradle marker is present (Groovy or
    # Kotlin DSL, settings file, or the wrapper). pom.xml-only stays maven.
    if language == "java" and _has_gradle_marker(cwd):
        return "gradle"
    return _toolchain_map.get(language)


def _has_gradle_marker(cwd: Path) -> bool:
    """True when *cwd* carries any Gradle build marker (root-level)."""
    return any(
        (cwd / marker).exists()
        for marker in (
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            "settings.gradle.kts",
            "gradlew",
        )
    )


# v1.0 (Phase 2, WS2-3 coordination): languages whose tests AutoDev can
# actually BUILD AND RUN in-environment. A *detected* language outside this
# set means "tests are not runnable here" → the huge-repo profile / runtime
# fallback degrades LOUD (no_test_coverage / skipped_toolchain_missing)
# rather than silently soft-passing an infra-class capture failure.
#
# FIRST-CLASS = {python, nodejs, go, rust, java}. These have working runners
# in :mod:`qa.test_runner` (pytest / npm / cargo / go) and java is wired as a
# coordinated first-class target.
#
# REMOVED in v1.0: ``dotnet`` / ``ruby`` / ``swift``. They have NO runner in
# :mod:`qa.test_runner` (it returns the "no test runner configured" skip),
# so keeping them here let a real test-capture failure look like a clean
# soft-pass. They now correctly fall through to :func:`is_repo_unbuildable`
# → degrade-loud. ``cpp`` was never in this set for the same reason.
RUNNABLE_TEST_LANGUAGES = {
    "python",
    "nodejs",
    "rust",
    "go",
    "java",
}


def is_repo_unbuildable(cwd: Path) -> bool:
    """True when AutoDev cannot build/run this repo's tests in-environment.

    Returns ``True`` when there is no detected language, or the detected
    language is outside :data:`RUNNABLE_TEST_LANGUAGES` (e.g. ``"cpp"``,
    ``"dotnet"``, ``"ruby"``, ``"swift"``).
    """
    lang = detect_language(cwd)
    return lang is None or lang not in RUNNABLE_TEST_LANGUAGES


# Gate-closer A (G6): source-code file extensions BEYOND the detection
# vocabulary in :data:`_EXTENSION_WEIGHTS`. Their presence (with NO recognised
# language) means the repo carries *source we cannot detect/run* — an
# unsupported language (e.g. Elixir / Scala / Haskell / PHP / Dart / ...), as
# opposed to a genuinely-empty repo (docs / config only). Used by
# :func:`repo_has_source` to distinguish the two so the QA-gate dispatch can
# degrade LOUD for the former while keeping the legit ``no_source`` pass for
# the latter. Kept lowercase, leading-dot, matched against ``Path.suffix``.
_EXTRA_SOURCE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".ex", ".exs",          # elixir
        ".erl", ".hrl",         # erlang
        ".scala", ".sc",        # scala
        ".clj", ".cljs", ".cljc",  # clojure
        ".hs", ".lhs",          # haskell
        ".ml", ".mli",          # ocaml
        ".fs", ".fsx", ".fsi",  # f#
        ".dart",                # dart
        ".php",                 # php
        ".pl", ".pm",           # perl
        ".lua",                 # lua
        ".r", ".jl",            # r / julia
        ".groovy",              # groovy (non-gradle)
        ".elm",                 # elm
        ".nim",                 # nim
        ".zig",                 # zig
        ".cr",                  # crystal
        ".vala",                # vala
        ".d",                   # d
        ".m", ".mm",            # objective-c
        ".sh", ".bash", ".zsh", ".fish",  # shell
        ".sql",                 # sql
    }
)


def repo_has_source(cwd: Path) -> bool:
    """True when the repo at *cwd* carries ANY recognisable source/manifest.

    "Source" = a file whose extension is a detection-vocabulary source ext
    (:data:`_EXTENSION_WEIGHTS`), an *extra* source ext
    (:data:`_EXTRA_SOURCE_EXTENSIONS`, e.g. ``.ex`` / ``.scala``), OR a known
    manifest (:data:`_MANIFEST_WEIGHTS` / :data:`_MANIFEST_GLOBS`). A repo with
    only README / config / licence files (no source, no manifest) is
    *source-free* → the legit ``no_source`` pass; a repo with ``.ex`` files but
    no detected language is *unsupported* → degrade LOUD.

    Git submodule subtrees (declared in ``.gitmodules`` / ``.git/modules``) are
    excluded, mirroring :func:`_score_languages`, so a submodule's source never
    makes an otherwise-empty host look like it carries source.
    """
    submodule_prefixes = _submodule_prefixes(cwd)
    manifest_names = set(_MANIFEST_WEIGHTS)
    glob_exts = {pattern[1:] for pattern, _lang, _w in _MANIFEST_GLOBS}
    source_exts = set(_EXTENSION_WEIGHTS) | _EXTRA_SOURCE_EXTENSIONS
    for fp in iter_repo_files(cwd):
        if submodule_prefixes:
            try:
                rel_parts = fp.relative_to(cwd).parts
            except ValueError:
                rel_parts = fp.parts
            if _under_prefix(rel_parts, submodule_prefixes):
                continue
        if fp.name in manifest_names:
            return True
        suffix = fp.suffix.lower()
        if suffix in source_exts or suffix in glob_exts:
            return True
    return False


def classify_language_support(cwd: Path) -> tuple[str, str | None, str]:
    """Classify the QA-runnability of the repo at *cwd* for the gate dispatch.

    Returns a ``(status, language, reason)`` triple:

    * ``("runnable", lang, "")`` — a first-class RUNNABLE language was detected
      (gates run normally).
    * ``("no_source", None, reason)`` — no detected language AND no source at
      all (genuinely-empty repo → legit ``no_source`` pass).
    * ``("unsupported", lang_or_None, reason)`` — the repo carries source but
      the language is undetectable (``lang=None``) OR is recognised-but-NOT
      runnable (``lang`` ∈ {cpp, dotnet, ruby, swift, ...}). The dispatch must
      degrade LOUD for this case and emit a ``language_unsupported`` ledger op.
    """
    lang = detect_language(cwd)
    if lang is not None and lang in RUNNABLE_TEST_LANGUAGES:
        return ("runnable", lang, "")
    if lang is None:
        if repo_has_source(cwd):
            return (
                "unsupported",
                None,
                "no recognised language but repo carries source files "
                "(unsupported language — QA gates cannot run)",
            )
        return ("no_source", None, "no source files / no recognised language")
    # Detected a language, but it is not RUNNABLE in-environment.
    return (
        "unsupported",
        lang,
        f"detected language={lang!r} has no runnable QA toolchain "
        "in-environment (non-runnable language)",
    )


__all__ = [
    "RUNNABLE_TEST_LANGUAGES",
    "classify_language_support",
    "detect_language",
    "detect_toolchain",
    "is_repo_unbuildable",
    "repo_has_source",
]
