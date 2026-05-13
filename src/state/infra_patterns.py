"""Shared keyword classifier for infrastructure-class block reasons.

The v0.28.0 ``autodev requeue --infrastructure`` selector and the
v0.29.0 :attr:`Task.block_reason_class` migration shim both classify
free-text ``blocked_reason`` strings into the typed ``"infrastructure"``
vs ``"verdict"`` buckets. The keyword list is the single source of
truth for both; the v0.29.0 plan calls for refactoring it out of
``cli.commands.requeue`` so the schema-load path can use it without
introducing a CLI -> state import cycle.

The list intentionally errs on the side of *under*-classifying as
infrastructure: a transient failure misclassified as ``"verdict"``
just means the operator has to intervene with ``autodev requeue``
manually, while the reverse (legit-verdict reclassified as transient
infra) would auto-restart a task that the agent legitimately said was
wrong. ``classify_blocked_reason`` returns ``"verdict"`` as the
conservative default for any ambiguous string.
"""

from __future__ import annotations

from typing import Literal


# Substring patterns matched case-insensitively against
# ``Task.blocked_reason``. Mirrors the v0.28.0 list in
# ``cli.commands.requeue._INFRA_PATTERNS`` (which now imports from here).
INFRA_PATTERNS: tuple[str, ...] = (
    "403",
    "401",
    "Forbidden",
    "authenticate",
    "Failed to authenticate",
    "api_error_status",
    "Connection refused",
    "DNS",
    # v0.29.0: extend to cover the typed prefixes the orchestrator
    # stamps at the four block sites. ``auth_failed:`` from Bug 2 is
    # already covered by the ``authenticate`` substring; ``rate_limited``
    # and ``server_error`` are subtypes adapters now surface; the
    # architect-diagnosed infra path stamps a ``: infrastructure:``
    # marker. ``qa_gate_timeout``/``qa_gate_io_error`` are NOT included
    # here — those classify per-exception at the call site (timeout on
    # network = infra, timeout on local fs = verdict) and the keyword
    # heuristic would over-classify them.
    "auth_failed",
    "rate_limited",
    "server_error",
    "architect_consult: infrastructure",
)


def looks_infrastructure(blocked_reason: str | None) -> bool:
    """Return ``True`` iff ``blocked_reason`` looks like infrastructure
    failure under the v0.28.0/v0.29.0 keyword heuristic.

    Case-insensitive substring match. ``None`` and empty strings
    return ``False`` so callers can pass :attr:`Task.blocked_reason`
    directly without a guard.
    """
    if not blocked_reason:
        return False
    needle = blocked_reason.lower()
    return any(p.lower() in needle for p in INFRA_PATTERNS)


def classify_blocked_reason(
    blocked_reason: str | None,
) -> Literal["verdict", "infrastructure", "cap"]:
    """Classify a free-text ``blocked_reason`` into a
    :attr:`Task.block_reason_class` bucket.

    Conservative default: when in doubt, classify as ``"verdict"``.
    The migration shim in :class:`PlanManager._load_sync` calls this on
    legacy plans (status=blocked, no class field) to backfill the
    typed enum without losing forensic detail. ``"cap"`` is not
    inferable from the legacy free-text — pre-v0.29.0 plans never
    distinguished cap exhaustion from verdict, so we lump them
    together as ``"verdict"`` and let new blocks stamp ``"cap"``
    explicitly at the call site.
    """
    if looks_infrastructure(blocked_reason):
        return "infrastructure"
    return "verdict"


__all__ = [
    "INFRA_PATTERNS",
    "classify_blocked_reason",
    "looks_infrastructure",
]
