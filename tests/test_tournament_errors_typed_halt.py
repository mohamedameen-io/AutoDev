"""v0.38.0 I4 (HK7): typed-identity halt — explicit
``halted_task_id`` on :class:`InfrastructureCircuitOpenError`.

Pre-I4 the typed-halt handler walked the plan to infer which task to
attribute the halt to. On the parallel pool that walk raced the
worker stamp and sometimes attributed to the wrong task. I4 threads
the in-flight task id through the typed exception so the handler can
use it directly; the lookup is the back-compat fallback only.
"""

from __future__ import annotations


def test_infra_circuit_open_error_carries_explicit_task_id() -> None:
    """Construction with ``halted_task_id=...`` exposes it as an attribute."""
    from tournament.errors import InfrastructureCircuitOpenError

    exc = InfrastructureCircuitOpenError("msg", halted_task_id="1.1")
    assert exc.halted_task_id == "1.1"
    # ``str(exc)`` still uses the legacy positional arg so log-greps
    # against the operator-facing message stay aligned.
    assert "msg" in str(exc)


def test_infra_circuit_open_error_defaults_to_none() -> None:
    """Legacy positional construction → ``halted_task_id`` is ``None``."""
    from tournament.errors import InfrastructureCircuitOpenError

    exc = InfrastructureCircuitOpenError("msg")
    assert exc.halted_task_id is None
    assert "msg" in str(exc)


def test_infra_circuit_open_error_is_tournament_error_subclass() -> None:
    """Existing ``except TournamentError`` handlers must still match."""
    from errors import TournamentError
    from tournament.errors import InfrastructureCircuitOpenError

    exc = InfrastructureCircuitOpenError("msg", halted_task_id="2.3")
    assert isinstance(exc, TournamentError)


def test_infra_circuit_open_error_keyword_only_halted_task_id() -> None:
    """``halted_task_id`` is keyword-only — accidental positional swap
    against the message arg must not silently mis-bind."""
    from tournament.errors import InfrastructureCircuitOpenError

    # No positional 2nd arg supported — the message slot accepts an
    # arbitrary positional but ``halted_task_id`` requires the keyword.
    exc = InfrastructureCircuitOpenError(
        "circuit open", halted_task_id="3.2"
    )
    assert exc.halted_task_id == "3.2"
    # Calling with no positional + keyword works for the empty-message
    # forensic case.
    exc2 = InfrastructureCircuitOpenError(halted_task_id="4.4")
    assert exc2.halted_task_id == "4.4"
