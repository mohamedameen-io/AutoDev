"""Smoke test for v0.32.0 Phase 1.3 :mod:`state.failure_classes` taxonomy.

Phase 6 deliverable: a fast-suite anchor that fails loudly if anyone
deletes or renames the FailureClass enum or its plan-time recurrence
entries. The actual classification logic is exercised in dedicated
tests under :mod:`tests.test_state_failure_classes` (Phase 1.3); this
module is a pure import + presence check that doubles as a CI gate.
"""

from __future__ import annotations


def test_failure_classes_module_imports() -> None:
    """The Phase 1.3 module is importable from its canonical path."""
    from state import failure_classes  # noqa: F401


def test_failure_class_enum_exists_and_is_str_backed() -> None:
    """:class:`FailureClass` is exported and JSON-serialisable via str."""
    from enum import Enum

    from state.failure_classes import FailureClass

    assert issubclass(FailureClass, Enum)
    assert issubclass(FailureClass, str), (
        "FailureClass must subclass str so its members serialise as JSON "
        "strings inside ledger op payloads (Phase 1.3 contract)."
    )


def test_failure_class_taxonomy_covers_three_branches() -> None:
    """Execute-time, plan-time, and infrastructure branches all populated.

    The taxonomy is deliberately small but the three branches exist by
    contract — Phase 1.4 routes recovery on the branch prefix. Removing
    any branch would silently widen ``UNKNOWN`` and break recovery.
    """
    from state.failure_classes import FailureClass

    values = {member.value for member in FailureClass}
    assert any(v.startswith("execute.") for v in values), (
        "FailureClass missing execute-time entries"
    )
    assert any(v.startswith("plan.") for v in values), (
        "FailureClass missing plan-time entries"
    )
    # Infrastructure / Unknown catch-all surface — documented in the
    # module docstring as the cross-cutting branch.
    assert "Unknown" in {member.name for member in FailureClass} or any(
        v.startswith("infra.") for v in values
    ), "FailureClass missing the Unknown / infrastructure catch-all"


def test_classify_helper_is_exported() -> None:
    """:func:`classify` is part of the module's public API."""
    from state import failure_classes

    assert hasattr(failure_classes, "classify"), (
        "state.failure_classes.classify must remain exported — Phase 1.4 "
        "recovery tiers consume it directly."
    )
