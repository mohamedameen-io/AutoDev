"""Gate G5 — Java first-class runners, cargo ``--workspace``, and degrade-loud.

These tests pin three behaviours owned by :mod:`src.qa.test_runner`:

* **WS2-3** — Java is a *first-class* runnable language: Maven (``pom.xml``)
  uses ``mvn -q test`` and Gradle (``build.gradle`` / ``build.gradle.kts`` /
  ``gradlew``) uses ``./gradlew test`` or ``gradle test``. The run-count is
  parsed for non-vacuity: a non-empty java scope that runs 0 tests fails LOUD
  (``no_test_coverage``), reusing the Phase-1B ``_classify_run_count`` machinery.
* **WS2-10** — ``cargo test`` invocations carry ``--workspace`` so a
  workspace-root rust repo (a *virtual* manifest: ``[workspace]`` without
  ``[package]``) does not false-fail on the root having no tests.
* **degrade-LOUD** — ``dotnet`` / ``ruby`` / ``swift`` are SAFE-DEGRADE, not
  first-class: they must surface an unsupported / skipped signal
  (``passed=False`` + a marker metric), never a silent vacuous pass.

RED-on-HEAD (captured before the fix):

* ``test_maven_runs_and_parses_count`` / ``test_gradle_runs`` —
  ``no test runner configured for language='java'`` (silent pass), no ``mvn`` /
  ``gradle`` command is ever built.
* ``test_cargo_workspace_uses_workspace_flag`` — the built command is
  ``["cargo", "test"]``; ``--workspace`` is absent.
* ``test_dotnet_ruby_swift_degrade_loud`` — silent ``passed=True`` skip.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qa.test_runner import run_tests


def _make_proc(returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    return proc


# ---------------------------------------------------------------------------
# WS2-3 — Java is first-class (Maven + Gradle)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maven_runs_and_parses_count(tmp_path: Path) -> None:
    """A pom.xml repo with a passing test → mvn runs and the count is parsed."""
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.java").write_text("class App {}", encoding="utf-8")
    # Maven surefire summary that affirmatively reports tests ran.
    out = b"Tests run: 5, Failures: 0, Errors: 0, Skipped: 0\nBUILD SUCCESS"
    proc = _make_proc(0, stdout=out)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_tests(tmp_path, language="java")
    assert result.passed, result.details
    argv = list(mock_exec.call_args.args)
    assert argv[0] == "mvn"
    assert "test" in argv


@pytest.mark.asyncio
async def test_gradle_runs(tmp_path: Path) -> None:
    """A build.gradle repo → a gradle/gradlew test command is built and runs."""
    (tmp_path / "build.gradle").write_text("// gradle", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.java").write_text("class App {}", encoding="utf-8")
    out = b"BUILD SUCCESSFUL\n5 tests completed"
    proc = _make_proc(0, stdout=out)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_tests(tmp_path, language="java")
    assert result.passed, result.details
    argv = list(mock_exec.call_args.args)
    assert argv[0] in ("gradle", "./gradlew", str(tmp_path / "gradlew"))
    assert "test" in argv


@pytest.mark.asyncio
async def test_gradle_wrapper_preferred_when_present(tmp_path: Path) -> None:
    """When a gradlew wrapper exists, it is preferred over a bare ``gradle``."""
    (tmp_path / "build.gradle.kts").write_text("// kts", encoding="utf-8")
    wrapper = tmp_path / "gradlew"
    wrapper.write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.java").write_text("class App {}", encoding="utf-8")
    proc = _make_proc(0, stdout=b"BUILD SUCCESSFUL\n3 tests completed")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_tests(tmp_path, language="java")
    assert result.passed
    argv = list(mock_exec.call_args.args)
    assert argv[0] == str(wrapper)


@pytest.mark.asyncio
async def test_java_zero_tests_fails_loud(tmp_path: Path) -> None:
    """Non-vacuity: java source present but 0 tests ran → fail LOUD."""
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.java").write_text("class App {}", encoding="utf-8")
    # Surefire affirmatively reporting nothing ran, while exiting 0.
    out = b"Tests run: 0, Failures: 0, Errors: 0, Skipped: 0\nBUILD SUCCESS"
    proc = _make_proc(0, stdout=out)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await run_tests(tmp_path, language="java")
    assert not result.passed, result.details
    assert result.metrics.get("no_test_coverage") is True


@pytest.mark.asyncio
async def test_java_toolchain_missing_degrades_loud(tmp_path: Path) -> None:
    """A missing mvn/gradle binary degrades LOUD, not a silent pass."""
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        result = await run_tests(tmp_path, language="java")
    assert not result.passed
    assert result.metrics.get("skipped_toolchain_missing") is True


# ---------------------------------------------------------------------------
# WS2-10 — cargo --workspace (virtual manifest)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cargo_workspace_uses_workspace_flag(tmp_path: Path) -> None:
    """A virtual-manifest workspace root → ``cargo test --workspace``."""
    # Virtual manifest: [workspace] without [package].
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["crate_a"]\n', encoding="utf-8"
    )
    member = tmp_path / "crate_a" / "src"
    member.mkdir(parents=True)
    (member / "lib.rs").write_text("// rust", encoding="utf-8")
    proc = _make_proc(0, stdout=b"test result: ok. 3 passed; 0 failed")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_tests(tmp_path, language="rust")
    assert result.passed, result.details
    argv = list(mock_exec.call_args.args)
    assert argv[0] == "cargo"
    assert "--workspace" in argv


@pytest.mark.asyncio
async def test_cargo_plain_package_also_uses_workspace_flag(tmp_path: Path) -> None:
    """``--workspace`` is harmless on a single package, so it is always added."""
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "x"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "lib.rs").write_text("// rust", encoding="utf-8")
    proc = _make_proc(0, stdout=b"test result: ok. 1 passed; 0 failed")
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as mock_exec:
        result = await run_tests(tmp_path, language="rust")
    assert result.passed
    argv = list(mock_exec.call_args.args)
    assert "--workspace" in argv


@pytest.mark.asyncio
async def test_cargo_toolchain_missing_degrades_loud(tmp_path: Path) -> None:
    """cargo absent → skipped_toolchain_missing (Phase-1B handling preserved)."""
    (tmp_path / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        result = await run_tests(tmp_path, language="rust")
    assert not result.passed
    assert result.metrics.get("skipped_toolchain_missing") is True


# ---------------------------------------------------------------------------
# degrade-LOUD — dotnet / ruby / swift are SAFE-DEGRADE, not first-class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("lang", ["dotnet", "ruby", "swift"])
async def test_dotnet_ruby_swift_degrade_loud(tmp_path: Path, lang: str) -> None:
    """SAFE-DEGRADE languages must surface an unsupported signal, not silent-pass."""
    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
        result = await run_tests(tmp_path, language=lang)
    # No first-class runner is invoked.
    mock_exec.assert_not_called()
    # Degrade LOUD: a non-pass carrying an unsupported marker — never a clean green.
    assert not result.passed
    assert result.metrics.get("unsupported_language") is True


# ---------------------------------------------------------------------------
# Real-run smoke (only where the toolchain exists) — java is FOUND in CI here,
# but mvn/gradle are not, so this stays a command-shape assertion. cargo/mvn
# absent → exercised via the toolchain-missing tests above.
# ---------------------------------------------------------------------------
