"""Phase 0: parametrised baseline of architect hedge-text behavior.

For every fixture in :mod:`tests.fixtures.malformed_architect_outputs`
this module asserts the v0.26.2 plan-parser + on-disk validator behavior
labelled on the fixture. Commit 3 (Phase 1 parser hardening) is
expected to flip several fixtures' realised behavior from
``parse_ok_validate_fail`` to ``parse_drops_then_validate_ok`` — at
which point this baseline is updated and the regression-guard fixtures
remain ``parse_ok_validate_ok``.

The test runs purely in-process: no orchestrator, no adapter, no git
repo for the parse-error cases. Validate cases run inside a tmp_path
repo with a single tracked file so :func:`validate_files_exist`
engages.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from fixtures.malformed_architect_outputs import (
    ALL_HEDGE_FIXTURES,
    HedgeFixture,
)
from orchestrator.plan_parser import PlanParseError, parse_plan_markdown


def _init_repo_with_math(tmp_path: Path) -> None:
    """Bootstrap a git repo containing ``src/math/__init__.py``."""
    (tmp_path / "src" / "math").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "math" / "__init__.py").write_text(
        "def add(a, b): return a + b\n", encoding="utf-8"
    )
    # The legitimate-space-with-slash fixture needs ``docs/My File.md``.
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "My File.md").write_text("doc\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=str(tmp_path), check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=str(tmp_path), check=True
    )
    subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=str(tmp_path), check=True
    )


def _try_parse_and_validate(
    fixture: HedgeFixture, tmp_path: Path
) -> tuple[bool, bool]:
    """Return ``(parsed_ok, validated_ok)`` for one fixture.

    Parse first; if that raises ``PlanParseError`` return ``(False, False)``.
    Otherwise run ``validate_files_exist`` against the tmp repo and
    return ``(True, did_validate_ok)``.
    """
    try:
        plan = parse_plan_markdown(fixture.markdown)
    except PlanParseError:
        return False, False

    # Phase 0: lazy import — the validator module is sometimes stubbed
    # in unrelated tests; keep this test entirely self-contained.
    from orchestrator.file_existence_validator import validate_files_exist
    from orchestrator.path_validator import PathValidationError

    try:
        validate_files_exist(plan, tmp_path)
    except PathValidationError:
        return True, False
    return True, True


@pytest.mark.parametrize("fixture", ALL_HEDGE_FIXTURES, ids=lambda f: f.name)
def test_v026_baseline_for_hedge_fixture(
    fixture: HedgeFixture, tmp_path: Path
) -> None:
    """Pin the v0.26.2 parse + validate behavior for each hedge fixture.

    The fixture's ``v026_behavior`` label is the source of truth. When
    Commit 3 lands and the parser starts stripping a hedge upstream, the
    fixture's label is updated to ``parse_drops_then_validate_ok`` and
    this test continues to enforce the new (tighter) baseline.
    """
    _init_repo_with_math(tmp_path)
    parsed_ok, validated_ok = _try_parse_and_validate(fixture, tmp_path)

    if fixture.v026_behavior == "parse_error":
        assert not parsed_ok, (
            f"{fixture.name}: expected parse_error baseline but parser succeeded"
        )
    elif fixture.v026_behavior == "parse_ok_validate_fail":
        assert parsed_ok and not validated_ok, (
            f"{fixture.name}: expected parse_ok_validate_fail baseline; "
            f"got parsed={parsed_ok}, validated={validated_ok}"
        )
    elif fixture.v026_behavior == "parse_drops_then_validate_ok":
        assert parsed_ok and validated_ok, (
            f"{fixture.name}: expected parse_drops_then_validate_ok; "
            f"got parsed={parsed_ok}, validated={validated_ok}"
        )
    elif fixture.v026_behavior == "parse_ok_validate_ok":
        assert parsed_ok and validated_ok, (
            f"{fixture.name}: expected parse_ok_validate_ok; "
            f"got parsed={parsed_ok}, validated={validated_ok}"
        )
    else:  # pragma: no cover — exhaustive Literal check
        pytest.fail(f"unknown v026_behavior label: {fixture.v026_behavior!r}")


def test_legitimate_space_with_slash_regression_guard(
    tmp_path: Path,
) -> None:
    """REGRESSION GUARD: a legitimate path with spaces AND a slash
    (``docs/My File.md``) must validate cleanly. Any future shape-check
    that rejects spaces in paths would surface here first.
    """
    from fixtures.malformed_architect_outputs import (
        LEGITIMATE_SPACE_WITH_SLASH_REGRESSION,
    )

    _init_repo_with_math(tmp_path)
    parsed_ok, validated_ok = _try_parse_and_validate(
        LEGITIMATE_SPACE_WITH_SLASH_REGRESSION, tmp_path
    )
    assert parsed_ok and validated_ok, (
        "legitimate path with space + slash must parse AND validate; "
        f"got parsed={parsed_ok}, validated={validated_ok}"
    )
