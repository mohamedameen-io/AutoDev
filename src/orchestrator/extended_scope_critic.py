"""v0.20.0 C2: critic-justified extended_scope review.

When a task declares a non-empty ``Task.extended_scope``, the orchestrator
must route the request through ``critic_sounding_board`` BEFORE
:func:`orchestrator.dag.validate_edit_scope` admits the paths. The critic
returns one of the two RESOLUTION directives:

* ``RESOLUTION: approved-extended-scope`` — work proceeds.
* ``RESOLUTION: rejected-extended-scope`` — task is blocked.

Approval decisions are cached in ``plan_manager.metadata`` so re-runs
of ``validate_edit_scope`` do not re-invoke the critic for the same
``(task_id, scope_signature)`` pair (idempotent redundancy — the
critic is expensive and the validator is on the hot path).

The module returns a plain ``bool`` so call sites can decide whether to
raise :class:`orchestrator.dag.EditScopeViolation` (rejection) or
proceed (approval).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from autologging import get_logger
from orchestrator.delegation_envelope import DelegationEnvelope


if TYPE_CHECKING:
    from state.schemas import Task


logger = get_logger(__name__)


_APPROVAL_TOKEN = "RESOLUTION: approved-extended-scope"
_REJECTION_TOKEN = "RESOLUTION: rejected-extended-scope"


def scope_signature(task: "Task") -> str:
    """Deterministic hash of ``(task.id, sorted extended_scope)`` for caching.

    Two tasks with identical id + identical extended_scope share a
    signature; mutating the scope changes the signature and invalidates
    any prior cached decision.
    """
    parts = [task.id, "|".join(sorted(task.extended_scope))]
    return hashlib.sha256("/".join(parts).encode("utf-8")).hexdigest()[:16]


def _build_review_envelope(task: "Task", justification: str) -> DelegationEnvelope:
    """Render an EXTENDED_SCOPE_REVIEW: envelope for critic_sounding_board."""
    return DelegationEnvelope(
        task_id=task.id,
        target_agent="critic_sounding_board",
        action="critique",
        files=list(task.files),
        constraints=[
            f"EXTENDED_SCOPE_REVIEW: task {task.id} declares paths outside its phase/plan EDIT_SCOPE",
            f"Extended-scope: {', '.join(task.extended_scope)}",
            f"Justification: {justification or '(none provided)'}",
        ],
        acceptance=(
            f"Reply with EXACTLY ONE line of either {_APPROVAL_TOKEN!r} "
            f"or {_REJECTION_TOKEN!r}."
        ),
        context={
            "task_title": task.title,
            "task_description": task.description,
            "extended_scope_count": len(task.extended_scope),
        },
    )


def _parse_resolution(text: str) -> bool | None:
    """Parse a critic response. Returns True (approve), False (reject),
    or None (no resolution found — caller treats as rejection)."""
    if not text:
        return None
    haystack = text.strip()
    # Search for tokens — order matters: rejection wins if both appear so
    # the critic can't accidentally over-approve by emitting both.
    if _REJECTION_TOKEN in haystack:
        return False
    if _APPROVAL_TOKEN in haystack:
        return True
    return None


async def critic_review_extended_scope(
    orch: object,
    task: "Task",
    *,
    justification: str = "",
) -> bool:
    """Invoke ``critic_sounding_board`` for an extended-scope review.

    Returns ``True`` on approval, ``False`` on rejection (or any error /
    missing resolution token — fail-closed by design). The call is
    cached in ``orch.plan_manager.metadata['extended_scope_decisions']``
    keyed by :func:`scope_signature` so the validator can be re-run
    cheaply.
    """
    if not task.extended_scope:
        return True

    plan_manager = getattr(orch, "plan_manager", None)
    sig = scope_signature(task)

    # Check cache first.
    if plan_manager is not None:
        try:
            cached_metadata = await _read_metadata(plan_manager)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "extended_scope_critic.metadata_read_failed",
                err=str(exc),
            )
            cached_metadata = {}
        decisions = cached_metadata.get("extended_scope_decisions", {})
        if isinstance(decisions, dict) and sig in decisions:
            cached = decisions[sig]
            logger.info(
                "extended_scope_critic.cache_hit",
                task_id=task.id,
                approved=bool(cached),
            )
            return bool(cached)

    # Cache miss → invoke critic.
    envelope = _build_review_envelope(task, justification)
    delegate = _resolve_delegate(orch)
    if delegate is None:
        logger.warning(
            "extended_scope_critic.no_delegate",
            task_id=task.id,
        )
        return False

    try:
        result = await delegate(orch, "critic_sounding_board", envelope)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "extended_scope_critic.delegate_failed",
            task_id=task.id,
            err=str(exc),
        )
        return False

    text = getattr(result, "text", "") or ""
    decision = _parse_resolution(text)
    approved = bool(decision) if decision is not None else False

    # Persist decision to plan_manager metadata.
    if plan_manager is not None:
        try:
            await _persist_decision(plan_manager, sig, approved)
            await plan_manager.ledger_append(
                op="extended_scope_review",
                payload={
                    "task_id": task.id,
                    "extended_scope": list(task.extended_scope),
                    "approved": approved,
                    "signature": sig,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "extended_scope_critic.metadata_write_failed",
                err=str(exc),
            )

    logger.info(
        "extended_scope_critic.decision",
        task_id=task.id,
        approved=approved,
        signature=sig,
    )
    return approved


def _resolve_delegate(orch: object):  # type: ignore[no-untyped-def]
    """Late-bind the delegate function to avoid an import cycle.

    The orchestrator lifecycle imports ``execute_phase`` which imports
    this module. Resolving the delegate symbol on first call breaks the
    cycle. Tests that pass a stub orchestrator can plant their own
    delegate by setting ``orch._extended_scope_delegate``.
    """
    custom = getattr(orch, "_extended_scope_delegate", None)
    if custom is not None:
        return custom
    from orchestrator.execute_phase import delegate as _real_delegate

    return _real_delegate


async def _read_metadata(plan_manager: object) -> dict:  # type: ignore[no-untyped-def]
    """Best-effort metadata read. Returns ``{}`` on any error."""
    try:
        plan = await plan_manager.load()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return {}
    if plan is None:
        return {}
    return dict(plan.metadata or {})


async def _persist_decision(
    plan_manager: object,  # type: ignore[no-untyped-def]
    sig: str,
    approved: bool,
) -> None:
    """Merge the decision into ``Plan.metadata['extended_scope_decisions']``."""
    plan = await plan_manager.load()  # type: ignore[attr-defined]
    if plan is None:
        return
    metadata = dict(plan.metadata or {})
    decisions = metadata.get("extended_scope_decisions") or {}
    if not isinstance(decisions, dict):
        decisions = {}
    decisions[sig] = bool(approved)
    metadata["extended_scope_decisions"] = decisions
    plan.metadata = metadata
    # Best-effort save — different plan managers expose different surfaces.
    save = getattr(plan_manager, "save", None)
    if save is not None:
        await save(plan)


__all__ = [
    "critic_review_extended_scope",
    "scope_signature",
]
