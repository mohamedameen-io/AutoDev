"""v0.22.1 A4: EditScopeViolation surfaces both raw + normalized paths.

The 2026-05-09 Unity ledger contained ``edit_scope_violation`` events
with truncated messages (``…``) that hid which path malformation broke
validation. This test pins the new diagnostic format so the offending
path is unambiguous in future ledgers.

Full path normalization pipeline (architect-retry, structured errors)
lands in v0.22.2 B4.
"""

from __future__ import annotations

import pytest

from orchestrator.dag import (
    EditScopeViolation,
    _normalize_for_diagnostic,
    validate_edit_scope,
)
from state.schemas import Phase, Plan, Task


def test_normalize_strips_surrounding_backticks() -> None:
    assert _normalize_for_diagnostic("`Tests/Foo.cs`") == "Tests/Foo.cs"


def test_normalize_strips_surrounding_double_quotes() -> None:
    assert _normalize_for_diagnostic('"src/foo.py"') == "src/foo.py"


def test_normalize_strips_surrounding_single_quotes() -> None:
    assert _normalize_for_diagnostic("'src/foo.py'") == "src/foo.py"


def test_normalize_strips_leading_dot_slash() -> None:
    assert _normalize_for_diagnostic("./src/foo.py") == "src/foo.py"


def test_normalize_collapses_double_slashes() -> None:
    assert _normalize_for_diagnostic("src//foo.py") == "src/foo.py"


def test_normalize_strips_trailing_slash() -> None:
    assert _normalize_for_diagnostic("src/foo/") == "src/foo"


def test_normalize_strips_whitespace() -> None:
    assert _normalize_for_diagnostic("  foo.py  ") == "foo.py"


def test_normalize_idempotent_on_clean_path() -> None:
    assert _normalize_for_diagnostic("src/foo.py") == "src/foo.py"


def test_normalize_empty_string() -> None:
    assert _normalize_for_diagnostic("") == ""


def test_edit_scope_violation_includes_raw_and_normalized() -> None:
    """When a quoted/wrapped path violates scope, the error names both forms."""
    plan = Plan(
        plan_id="p1",
        spec_hash="h1",
        edit_scope=["src/"],
        created_at="2026-05-10T00:00:00",
        updated_at="2026-05-10T00:00:00",
        phases=[
            Phase(
                id="1",
                title="P1",
                tasks=[
                    Task(
                        id="1.1",
                        phase_id="1",
                        title="t",
                        description="d",
                        files=["`Tests/Foo.cs`"],  # raw includes backticks
                        status="pending",
                    ),
                ],
            )
        ],
    )
    with pytest.raises(EditScopeViolation) as exc:
        validate_edit_scope(plan)
    msg = str(exc.value)
    # Raw form (with backticks) MUST appear so operators see the malformation.
    assert "`Tests/Foo.cs`" in msg
    # Normalized form MUST appear so operators see the resolved path.
    assert "Tests/Foo.cs" in msg
    # The "normalized:" label is the contract:
    assert "normalized:" in msg
