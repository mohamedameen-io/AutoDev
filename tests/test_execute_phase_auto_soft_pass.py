"""v0.39.0 (Cluster A2b): unit tests for :func:`maybe_enable_auto_soft_pass`.

The runtime fallback tracks consecutive test-runner ``capture_failed``
diagnoses on a huge repo and, after >=2 in a row, auto-enables
``treat_unrunnable_tests_as_no_tests`` in-memory (idempotent). It is a
no-op on small repos and when the ``huge_repo_overrides_disabled`` escape
hatch is set.
"""

from __future__ import annotations

import pytest

from orchestrator.execute_phase import maybe_enable_auto_soft_pass


class _FakeCfg:
    def __init__(self, *, disabled: bool = False) -> None:
        self.huge_repo_overrides_disabled = disabled
        self.treat_unrunnable_tests_as_no_tests = False


class _FakeCapacity:
    def __init__(self, *, is_huge: bool) -> None:
        self.is_huge = is_huge


class _FakeOrch:
    """Minimal duck-typed orchestrator stub for the pure helper."""

    def __init__(self, *, is_huge: bool = True, disabled: bool = False) -> None:
        self.cfg = _FakeCfg(disabled=disabled)
        self._repo_capacity = _FakeCapacity(is_huge=is_huge)
        self._consecutive_capture_failed = 0
        # No plan_manager → the helper's best-effort ledger op is skipped.
        self.plan_manager = None


def test_two_consecutive_capture_failed_flips_once() -> None:
    orch = _FakeOrch(is_huge=True)
    # First capture_failed: counter=1, not yet flipped.
    assert maybe_enable_auto_soft_pass(orch, "capture_failed") is False
    assert orch.cfg.treat_unrunnable_tests_as_no_tests is False
    # Second capture_failed: counter=2 → flips.
    assert maybe_enable_auto_soft_pass(orch, "capture_failed") is True
    assert orch.cfg.treat_unrunnable_tests_as_no_tests is True
    # Third capture_failed: already enabled → idempotent no-op.
    assert maybe_enable_auto_soft_pass(orch, "capture_failed") is False
    assert orch.cfg.treat_unrunnable_tests_as_no_tests is True


def test_clean_ok_resets_counter() -> None:
    orch = _FakeOrch(is_huge=True)
    assert maybe_enable_auto_soft_pass(orch, "capture_failed") is False
    # A clean run resets the streak before it reaches 2.
    assert maybe_enable_auto_soft_pass(orch, "ok") is False
    assert orch._consecutive_capture_failed == 0
    # One more capture_failed is only the first of a new streak → no flip.
    assert maybe_enable_auto_soft_pass(orch, "capture_failed") is False
    assert orch.cfg.treat_unrunnable_tests_as_no_tests is False


def test_no_tests_found_resets_counter() -> None:
    orch = _FakeOrch(is_huge=True)
    assert maybe_enable_auto_soft_pass(orch, "capture_failed") is False
    assert maybe_enable_auto_soft_pass(orch, "no_tests_found") is False
    assert orch._consecutive_capture_failed == 0
    assert maybe_enable_auto_soft_pass(orch, "capture_failed") is False
    assert orch.cfg.treat_unrunnable_tests_as_no_tests is False


def test_small_repo_never_flips() -> None:
    orch = _FakeOrch(is_huge=False)
    for _ in range(5):
        assert maybe_enable_auto_soft_pass(orch, "capture_failed") is False
    assert orch.cfg.treat_unrunnable_tests_as_no_tests is False


def test_escape_hatch_never_flips() -> None:
    orch = _FakeOrch(is_huge=True, disabled=True)
    for _ in range(5):
        assert maybe_enable_auto_soft_pass(orch, "capture_failed") is False
    assert orch.cfg.treat_unrunnable_tests_as_no_tests is False


def test_missing_capacity_attr_never_flips() -> None:
    """An orchestrator stub without _repo_capacity is treated as small."""
    orch = _FakeOrch(is_huge=True)
    delattr(orch, "_repo_capacity")
    for _ in range(5):
        assert maybe_enable_auto_soft_pass(orch, "capture_failed") is False
    assert orch.cfg.treat_unrunnable_tests_as_no_tests is False
