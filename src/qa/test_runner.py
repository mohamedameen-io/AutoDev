"""Test-runner gate.

Runs the project's test suite and returns a
:class:`~plugins.registry.GateResult`.

Non-vacuity contract (WS2-2, WS2-6)
-----------------------------------
A gate that passes because it ran NOTHING is a bug. This module fails LOUD in
two found-nothing worlds that used to silently green:

* **Zero tests in a non-empty scope** — pytest exits ``0`` printing
  *"no tests ran"* (asyncio/conftest plugins suppress exit-code 5), and
  ``go test`` exits ``0`` printing *"[no test files]"* when source exists but
  no test file does. When source IS present but the runner affirmatively ran
  zero tests, the gate now returns ``passed=False`` with a ``no_test_coverage``
  signal. The legitimate empty-source / empty-baseline fast-exits still pass.

* **Toolchain absent** — ``FileNotFoundError`` (the runner binary is not
  installed) used to be swallowed into a clean pass. It now degrades LOUD:
  ``passed=False`` with a ``skipped_toolchain_missing`` signal — *unknown*, not
  *clean*.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from plugins.registry import GateResult
from qa.detect import detect_language
from qa.env import resolve_python_tool


# The orchestrator normally overrides this via ``cfg.test_timeout_s``; the
# default gives real (large) suites headroom rather than the old 60s ceiling
# that could not finish them.
_DEFAULT_TIMEOUT_S = 600


async def run_tests(
    cwd: Path,
    language: str | None = None,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    paths: list[Path] | None = None,
) -> GateResult:
    """Run the test suite appropriate for *language* (auto-detected when ``None``).

    When *paths* is given (repo-relative changed files), the suite is diff-scoped
    to the changed packages/files for every first-class runner — python (changed
    tests / bounded default), nodejs (changed test files via ``npm test -- …``),
    rust (changed crates via ``cargo test -p …``), and go (changed packages via
    ``go test ./pkg/...``). WS2-18: these multi-language runners used to drop
    *paths* silently and always run the whole suite. ``paths=None`` (and an empty
    no-git-signal scope) still runs the full default suite for back-compat.

    Returns a :class:`GateResult` with ``passed=True`` on success or when the
    test runner is not available.
    """
    lang = language or detect_language(cwd)
    if lang is None:
        return GateResult(passed=True, details="language not detected, skipping tests")

    if lang == "python":
        return await _run_pytest(cwd, timeout_s=timeout_s, paths=paths)

    runners: dict[str, object] = {
        "nodejs": _run_npm_test,
        "rust": _run_cargo_test,
        "go": _run_go_test,
        "java": _run_java_test,
    }
    runner = runners.get(lang)
    if runner is None:
        # SAFE-DEGRADE languages (dotnet/ruby/swift/cpp/…) are NOT first-class:
        # AutoDev does not drive their test toolchains here. Phase-1B convention
        # for "unknown, not clean" applies — degrade LOUD (passed=False + an
        # ``unsupported_language`` marker) so the resolver treats it as blocking
        # rather than a silent vacuous green. severity stays the default 'block'.
        return GateResult(
            passed=False,
            details=(
                f"no first-class test runner for language={lang!r}: "
                "unsupported toolchain (unsupported_language — degraded loud, "
                "not a clean pass)"
            ),
            metrics={"unsupported_language": True, "language": lang},
        )
    return await runner(cwd, timeout_s=timeout_s, paths=paths)  # type: ignore[operator]


def _is_test_path(p: Path) -> bool:
    """True when *p* looks like a pytest test module or lives under ``tests/``."""
    if p.suffix == ".py" and (p.name.startswith("test_") or p.name.endswith("_test.py")):
        return True
    return "tests" in p.parts


def _should_scope(cwd: Path, paths: list[Path] | None) -> bool:
    """True when a diff-scoped selection should be derived from *paths*.

    WS2-18: the nodejs/rust/go runners used to drop *paths* silently and always
    run the whole suite. They now scope to the changed packages/files, mirroring
    ``_run_pytest``'s contract:

    * ``paths is None`` (legacy) → whole suite (return ``False``).
    * ``paths == []`` in a NON-git repo → no git signal, NOT a clean diff; a
      scoped subset would be vacuous (scope 0 files → run nothing → false
      green), so fall back to the whole suite (return ``False``). Mirrors the
      WS2-14 guard in ``_run_pytest``.
    * otherwise → derive a diff-scoped selection (return ``True``). A git repo
      with a genuinely empty diff yields ``paths=[]`` *with* a git signal and is
      handled by the per-language selectors (no source-language files changed →
      whole-suite fall-through, never a vacuous empty scope).
    """
    if paths is None:
        return False
    if not paths and not _has_git_signal(cwd):
        return False
    return True


def _has_git_signal(cwd: Path) -> bool:
    """True iff *cwd* is a git repo (has a ``.git`` entry).

    WS2-14: the changed-file scope handed to the test gate is derived from a
    *git* diff. A NON-git in-place repo therefore yields an EMPTY scope
    (``paths=[]``) that means "no git signal", NOT "a clean git repo with no
    changes". The two must be distinguished: when there is no git signal we
    fall back to the full suite rather than running a vacuous empty-scoped
    subset. Mirrors ``orchestrator.execute_phase._is_git_repo``.
    """
    try:
        return (cwd / ".git").exists()
    except OSError:
        return False


# Affirmative "ran nothing" tokens emitted by the runners on a zero-test run
# that *still exits 0*. Detection is affirmative on purpose: we only fail loud
# when the output positively says it ran/collected nothing. Unrecognised or
# empty output (e.g. a quiet runner) is treated as ``"unknown"`` and does NOT
# trigger the loud fail — that keeps quiet runners and mocked-empty-stdout
# results passing while still killing the concrete vacuous-pass vectors.
_ZERO_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bno tests ran\b", re.IGNORECASE),
    re.compile(r"\bno tests were run\b", re.IGNORECASE),
    re.compile(r"\b0 passed\b", re.IGNORECASE),
    re.compile(r"\bcollected 0 items\b", re.IGNORECASE),
    re.compile(r"\[no test files\]", re.IGNORECASE),  # go test
    re.compile(r"\bno test files\b", re.IGNORECASE),
    # Maven surefire / Gradle summary that affirmatively ran zero tests.
    re.compile(r"\bTests run:\s*0\b", re.IGNORECASE),  # surefire: "Tests run: 0"
    re.compile(r"\b0 tests completed\b", re.IGNORECASE),  # gradle
)
# Positive "tests actually ran" tokens. Their presence overrides a coincidental
# zero-token (e.g. "0 failed, 5 passed").
_RAN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b([1-9]\d*) passed\b", re.IGNORECASE),
    re.compile(r"\b([1-9]\d*) failed\b", re.IGNORECASE),
    re.compile(r"\bcollected ([1-9]\d*) items?\b", re.IGNORECASE),
    re.compile(r"^ok\s", re.IGNORECASE | re.MULTILINE),  # go: "ok  pkg  0.5s"
    re.compile(r"\bok\b.*\bcoverage\b", re.IGNORECASE),
    # Maven surefire: "Tests run: 5, Failures: 0, ..." (>=1 ran).
    re.compile(r"\bTests run:\s*([1-9]\d*)\b", re.IGNORECASE),
    # Gradle: "5 tests completed".
    re.compile(r"\b([1-9]\d*) tests? completed\b", re.IGNORECASE),
)


def _classify_run_count(output: str) -> tuple[str, int]:
    """Classify runner *output* as ``"ran"``, ``"zero"``, or ``"unknown"``.

    * ``"ran"`` — the output positively reports >=1 test executed/collected.
    * ``"zero"`` — the output affirmatively reports it ran/collected nothing.
    * ``"unknown"`` — no recognisable count token (quiet runner / empty stdout).

    A positive "ran" token wins over a zero token so ``"0 failed, 5 passed"``
    classifies as ``"ran"``.
    """
    if any(p.search(output) for p in _RAN_PATTERNS):
        return ("ran", 1)
    if any(p.search(output) for p in _ZERO_PATTERNS):
        return ("zero", 0)
    return ("unknown", -1)


def _has_source(cwd: Path, language: str | None) -> bool:
    """True when *cwd* holds non-trivial source for *language*.

    Used to distinguish a code-present-but-zero-tests repo (fail loud) from a
    genuinely empty / manifest-only repo (legitimate fast-exit pass). Skips
    virtualenv / cache dirs so a stray ``.venv`` does not count as source.
    """

    def _skip(p: Path) -> bool:
        parts = set(p.parts)
        return bool(parts & {".venv", "venv", "__pycache__", ".git", "node_modules"})

    suffixes: tuple[str, ...]
    if language == "python":
        suffixes = (".py",)
    elif language == "go":
        suffixes = (".go",)
    elif language == "nodejs":
        suffixes = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
    elif language == "rust":
        suffixes = (".rs",)
    elif language == "java":
        suffixes = (".java", ".kt")
    else:
        suffixes = ()
    if not suffixes:
        return False
    for suffix in suffixes:
        for candidate in cwd.rglob(f"*{suffix}"):
            if not _skip(candidate):
                return True
    return False


async def _run_subprocess(
    args: list[str],
    cwd: Path,
    *,
    timeout_s: float,
    tool_name: str,
    scope_nonempty: bool = False,
) -> GateResult:
    """Run *args*; return a GateResult.

    When *scope_nonempty* is True and the runner exits 0 but affirmatively ran
    zero tests, fail LOUD (``no_test_coverage``) instead of vacuously passing.
    A missing toolchain (``FileNotFoundError``) degrades LOUD
    (``skipped_toolchain_missing``).
    """
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                *args,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=timeout_s,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except FileNotFoundError:
        # WS2-6: a missing toolchain is *unknown*, not *clean*. Degrade loud so
        # the resolver treats it as blocking rather than a silent green.
        return GateResult(
            passed=False,
            details=(
                f"{tool_name} not installed: test toolchain missing "
                "(skipped_toolchain_missing)"
            ),
            metrics={"skipped_toolchain_missing": True, "tool": tool_name},
        )
    except asyncio.TimeoutError:
        return GateResult(passed=False, details=f"{tool_name} tests timed out")

    combined = (stdout + stderr).decode(errors="replace").strip()
    if proc.returncode == 0:
        # WS2-2: rc==0 alone is not a pass. If source is present but the runner
        # affirmatively ran zero tests, this is a vacuous pass → fail loud.
        verdict, _ = _classify_run_count(combined)
        if scope_nonempty and verdict == "zero":
            return GateResult(
                passed=False,
                details=(
                    f"{tool_name}: 0 tests ran in a non-empty source scope "
                    "(no_test_coverage / scope mismatch — no tests found)"
                ),
                metrics={"no_test_coverage": True, "tool": tool_name},
            )
        return GateResult(passed=True, details=f"{tool_name} tests passed")
    return GateResult(passed=False, details=f"{tool_name} tests failed:\n{combined}")


async def _run_pytest(
    cwd: Path,
    *,
    timeout_s: float,
    paths: list[Path] | None,
) -> GateResult:
    """Run pytest via the repo's tooling, scoped to changed tests if *paths* given.

    WS-6b: pytest is a version-sensitive pure-Python tool — run it under the
    TARGET repo's interpreter (``resolve_python_tool``) rather than AutoDev's
    host py3.13, which would import the target package under the wrong Python.
    Repos with no target venv keep the bare host ``pytest`` (unchanged).
    """
    base = resolve_python_tool(cwd, "pytest")

    if paths is None:
        targets: list[str] = []
    elif not paths and not _has_git_signal(cwd):
        # WS2-14: empty scope (``paths=[]``) in a NON-git in-place repo means
        # "no git signal", NOT "a clean git repo with no changes". The git diff
        # is simply unavailable here, so a diff-scoped subset would be VACUOUS
        # (scope to 0 files → run nothing → false green). Fall back to the FULL
        # suite (``targets=[]`` → bare ``pytest -q``). A git repo with an empty
        # diff (clean tree) still hits the legitimate "no python changes" pass
        # in the ``else`` branch below.
        targets = []
    else:
        changed_tests = [str(p) for p in paths if _is_test_path(p)]
        if changed_tests:
            targets = changed_tests
        elif any(p.suffix == ".py" for p in paths):
            # Source-only change: run a bounded default suite when present.
            targets = ["tests/unit"] if (cwd / "tests" / "unit").is_dir() else []
        else:
            return GateResult(passed=True, details="tests: no python changes")

    args = [*base, *targets, "-q"]
    # Non-empty scope = the repo actually has python source. A manifest-only
    # repo (no ``.py``) keeps the legitimate "nothing to cover yet" fast-exit
    # pass; a repo WITH code but zero tests fails loud.
    scope_nonempty = _has_source(cwd, "python")
    return await _run_subprocess(
        args, cwd, timeout_s=timeout_s, tool_name="pytest", scope_nonempty=scope_nonempty
    )


_NODE_TEST_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")


def _is_node_test_path(p: Path) -> bool:
    """True when *p* looks like a JS/TS test/spec module or lives under ``test(s)/``.

    Recognises the cross-framework conventions jest/mocha/vitest/ava share:
    ``*.test.<ext>``, ``*.spec.<ext>``, and files under a ``test``/``tests``/
    ``__tests__`` directory.
    """
    if p.suffix in _NODE_TEST_EXTS:
        stem = p.name[: -len(p.suffix)]
        if stem.endswith((".test", ".spec")):
            return True
    return bool({"test", "tests", "__tests__"} & set(p.parts))


def _npm_test_args(cwd: Path, paths: list[Path] | None) -> list[str]:
    """Build the ``npm test`` argv, diff-scoped to changed test files if given.

    Test files are forwarded to the underlying runner (jest/mocha/vitest/…) as
    positional filters via the npm ``--`` separator: ``npm test -- <files>``.
    Only changed test files that exist on disk are forwarded — a changed test
    not yet materialized in this worktree would make the runner error on a
    missing path. When *paths* yields no usable changed test files we keep the
    whole suite (back-compat; never a vacuous empty scope).
    """
    if not _should_scope(cwd, paths):
        return ["npm", "test"]
    assert paths is not None
    test_files = [
        str(p) for p in paths if _is_node_test_path(p) and (cwd / p).is_file()
    ]
    if not test_files:
        return ["npm", "test"]
    return ["npm", "test", "--", *test_files]


async def _run_npm_test(
    cwd: Path, *, timeout_s: float, paths: list[Path] | None = None
) -> GateResult:
    # F-6 (Fix 1): ``npm test`` requires a ``package.json``. Without one, npm
    # exits ENOENT — rc≠0 → ``_run_subprocess`` would FALSE-BLOCK as if the
    # tests failed (field-observed on the task_002 benchmark, whose grader is
    # ``node test_index.js``, not npm). Mirror the sibling build gate
    # (``build_check._run_nodejs_build``), which already guards on
    # ``package.json`` existence before ``npm run build``: an absent manifest
    # is a NON-BLOCKING skip (passed=True), NOT a spurious ENOENT block. A
    # genuine test failure (package.json PRESENT, tests fail) still blocks via
    # the runner below — only the absent-manifest case is skipped.
    if not (cwd / "package.json").exists():
        return GateResult(
            passed=True,
            details="no package.json — skipping npm test (no npm project configured)",
        )
    return await _run_subprocess(
        _npm_test_args(cwd, paths),
        cwd,
        timeout_s=timeout_s,
        tool_name="npm test",
        scope_nonempty=_has_source(cwd, "nodejs"),
    )


def _cargo_crate_for(cwd: Path, rel: Path) -> str | None:
    """Return the cargo crate name owning changed file *rel*, or ``None``.

    Walks up from *rel*'s directory to the first ancestor (within *cwd*) holding
    a ``Cargo.toml`` with a ``[package] name = "…"`` entry. A virtual-workspace
    manifest (``[workspace]`` with no ``[package]``) is skipped so we keep
    walking toward the real member crate.
    """
    parent = (cwd / rel).parent
    try:
        parent.relative_to(cwd)
    except ValueError:
        return None
    current = parent
    while True:
        manifest = current / "Cargo.toml"
        if manifest.is_file():
            name = _cargo_package_name(manifest)
            if name is not None:
                return name
        if current == cwd:
            return None
        current = current.parent


_CARGO_PKG_NAME = re.compile(r'^\s*name\s*=\s*"([^"]+)"', re.MULTILINE)


def _cargo_package_name(manifest: Path) -> str | None:
    """Extract ``[package] name`` from a ``Cargo.toml``, or ``None`` if absent.

    Only the ``[package]`` table's ``name`` is honoured; a virtual-workspace
    manifest (``[workspace]`` with no ``[package]``) returns ``None``.
    """
    try:
        text = manifest.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    in_package = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_package = stripped == "[package]"
            continue
        if in_package:
            m = _CARGO_PKG_NAME.match(line)
            if m:
                return m.group(1)
    return None


def _cargo_test_args(cwd: Path, paths: list[Path] | None) -> list[str]:
    """Build the ``cargo test`` argv, diff-scoped to changed crates if given.

    Whole-suite default (``paths is None`` or no git signal) keeps
    ``cargo test --workspace`` (WS2-10: ``--workspace`` runs every member
    crate's tests and is a harmless no-op on a single-package crate). When a
    diff scope IS provided, each changed ``.rs`` file's owning crate is selected
    via ``-p <crate>``; ``--workspace`` is dropped so the run is genuinely
    narrowed. If no crate can be resolved (no ``.rs`` changed, or no
    ``[package]`` manifest found) we keep the whole ``--workspace`` suite.
    """
    if not _should_scope(cwd, paths):
        return ["cargo", "test", "--workspace"]
    assert paths is not None
    crates: list[str] = []
    for p in paths:
        if p.suffix != ".rs":
            continue
        crate = _cargo_crate_for(cwd, p)
        if crate is not None and crate not in crates:
            crates.append(crate)
    if not crates:
        return ["cargo", "test", "--workspace"]
    args = ["cargo", "test"]
    for crate in crates:
        args += ["-p", crate]
    return args


async def _run_cargo_test(
    cwd: Path, *, timeout_s: float, paths: list[Path] | None = None
) -> GateResult:
    # WS2-10: ``--workspace`` runs every member crate's tests. Without it a
    # workspace-ROOT repo (a *virtual* manifest: ``[workspace]`` with no
    # ``[package]``) false-fails because the root has no tests of its own.
    # ``--workspace`` is a harmless no-op on a single-package crate, so it is
    # always added rather than gated on manifest sniffing.
    # WS2-18: when a diff scope is provided, narrow to changed crates with
    # ``-p <crate>`` instead of running the whole workspace.
    return await _run_subprocess(
        _cargo_test_args(cwd, paths),
        cwd,
        timeout_s=timeout_s,
        tool_name="cargo test",
        scope_nonempty=_has_source(cwd, "rust"),
    )


def _go_test_args(cwd: Path, paths: list[Path] | None) -> list[str]:
    """Build the ``go test`` argv, diff-scoped to changed packages if given.

    Whole-suite default (``paths is None`` or no git signal) keeps
    ``go test ./...``. With a diff scope, each changed ``.go`` file's package
    directory becomes a ``./dir/...`` selector (the package root itself maps to
    ``./...``). When no ``.go`` file changed we keep the whole ``./...`` suite
    (never a vacuous empty scope).
    """
    if not _should_scope(cwd, paths):
        return ["go", "test", "./..."]
    assert paths is not None
    pkgs: list[str] = []
    for p in paths:
        if p.suffix != ".go":
            continue
        parent = p.parent
        # Repo-relative dir → "./dir/..."; the module root maps to "./...".
        selector = "./..." if parent in (Path("."), Path("")) else f"./{parent.as_posix()}/..."
        if selector not in pkgs:
            pkgs.append(selector)
    if not pkgs:
        return ["go", "test", "./..."]
    return ["go", "test", *pkgs]


async def _run_go_test(
    cwd: Path, *, timeout_s: float, paths: list[Path] | None = None
) -> GateResult:
    return await _run_subprocess(
        _go_test_args(cwd, paths),
        cwd,
        timeout_s=timeout_s,
        tool_name="go test",
        scope_nonempty=_has_source(cwd, "go"),
    )


def _java_test_command(cwd: Path) -> tuple[list[str], str]:
    """Return ``(argv, tool_name)`` for the java test runner in *cwd*.

    Maven (``pom.xml``) → ``mvn -q test``. Otherwise Gradle, preferring the
    repo's ``./gradlew`` wrapper (Maven/Gradle convention) over a bare
    ``gradle`` on PATH. ``build.gradle`` *and* ``build.gradle.kts`` count.
    Defaults to Maven when neither manifest is present (the detector already
    proved this is a java repo).
    """
    if (cwd / "pom.xml").exists():
        return (["mvn", "-q", "test"], "mvn test")
    wrapper = cwd / "gradlew"
    if wrapper.exists():
        return ([str(wrapper), "test"], "gradlew test")
    if (cwd / "build.gradle").exists() or (cwd / "build.gradle.kts").exists():
        return (["gradle", "test"], "gradle test")
    # No recognisable manifest — fall back to Maven; a missing binary will
    # degrade LOUD via the toolchain-missing path.
    return (["mvn", "-q", "test"], "mvn test")


async def _run_java_test(
    cwd: Path, *, timeout_s: float, paths: list[Path] | None = None
) -> GateResult:
    # WS2-3: java is first-class. Maven surefire ("Tests run: N, …") and Gradle
    # ("N tests completed") run-counts are recognised by the shared classifier,
    # so a non-empty java scope that runs 0 tests fails LOUD (no_test_coverage).
    # WS2-18: ``paths`` is accepted for a uniform runner dispatch but java
    # diff-scoping (per-module ``-pl`` / ``--tests`` selection) is NOT in scope
    # here; the whole-module suite still runs (back-compat).
    del paths
    args, tool_name = _java_test_command(cwd)
    return await _run_subprocess(
        args,
        cwd,
        timeout_s=timeout_s,
        tool_name=tool_name,
        scope_nonempty=_has_source(cwd, "java"),
    )


__all__ = ["run_tests"]
