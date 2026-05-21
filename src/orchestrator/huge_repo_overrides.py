"""v0.37.0 H5: knob-keyed huge-repo multiplier resolver.

Sibling to :mod:`tournament.task_overrides` (which handles the per-task
``max_turns`` multiplier via complexity / role lookup). H5 extends the
same ``cfg.task_overrides.huge_repo_multipliers`` dict to additionally
carry knob-keyed entries — e.g. ``max_corrective_tasks_per_phase``,
``test_diag_breaker_window_s``, ``recent_evidence_max_chars_per_kind``,
``circuit_breaker_threshold``, ``test_diag_breaker_threshold``,
``max_duration_s_per_task``, ``max_diff_bytes`` — so the H1/H2/H3 caps
auto-scale on huge repos without operator config tuning.

The resolver consults :func:`orchestrator.repo_size.is_huge_repo` and,
when True, multiplies the base value by the per-knob multiplier (when
the dict contains it), emitting one ``huge_repo_multiplier_applied``
telemetry op per scaled knob via the existing ledger plumbing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable


_log = logging.getLogger(__name__)


def resolve_huge_repo_value(
    *,
    key: str,
    base_value: float,
    cwd: Path,
    cfg: Any,
) -> tuple[float, float | None]:
    """Resolve a base value to its huge-repo-scaled equivalent.

    Args:
        key: Knob name (e.g. ``"max_corrective_tasks_per_phase"``).
            Must match a key in
            ``cfg.task_overrides.huge_repo_multipliers`` for scaling
            to apply.
        base_value: Operator-configured baseline (the value the resolver
            would have used pre-H5).
        cwd: Repository root.
        cfg: :class:`config.schema.AutodevConfig` instance.

    Returns:
        Tuple of ``(effective_value, applied_multiplier_or_None)``. The
        second element is ``None`` when no scaling fired (small repo,
        escape hatch set, or missing dict key) so callers can suppress
        the telemetry op in those cases.
    """
    from orchestrator.repo_size import is_huge_repo

    if not is_huge_repo(cwd, cfg=cfg):
        return base_value, None

    task_overrides_cfg = getattr(cfg, "task_overrides", None)
    if task_overrides_cfg is None:
        return base_value, None
    multipliers = getattr(task_overrides_cfg, "huge_repo_multipliers", None)
    if not isinstance(multipliers, dict) or key not in multipliers:
        return base_value, None

    try:
        mult = float(multipliers[key])
    except (TypeError, ValueError):
        return base_value, None
    if mult <= 0:
        return base_value, None

    effective = base_value * mult
    return effective, mult


async def apply_and_log_huge_repo_value(
    *,
    key: str,
    base_value: float,
    cwd: Path,
    cfg: Any,
    ledger_append: Callable[..., Awaitable[Any]] | None = None,
) -> float:
    """Resolve a value and emit the ``huge_repo_multiplier_applied`` op.

    Convenience wrapper that combines :func:`resolve_huge_repo_value`
    with the existing ledger telemetry op shape so call sites stay
    one-liners.

    Args:
        key: Knob name (must match a key in
            ``cfg.task_overrides.huge_repo_multipliers``).
        base_value: Operator-configured baseline.
        cwd: Repository root.
        cfg: :class:`config.schema.AutodevConfig` instance.
        ledger_append: Async callable matching the
            :meth:`state.plan_manager.PlanManager.ledger_append` shape
            (``op=str, payload=dict`` kwargs). ``None`` disables
            telemetry (used by sync call sites / unit tests).

    Returns:
        Effective value (``base_value * multiplier`` when scaling fired,
        else ``base_value``).
    """
    effective, mult = resolve_huge_repo_value(
        key=key, base_value=base_value, cwd=cwd, cfg=cfg
    )
    if mult is None:
        return effective

    if ledger_append is not None:
        try:
            await ledger_append(
                op="huge_repo_multiplier_applied",
                payload={
                    "key": key,
                    "base_value": base_value,
                    "multiplier": mult,
                    "effective_value": effective,
                },
            )
        except Exception as exc:  # noqa: BLE001 — telemetry never blocks
            _log.warning(
                "huge_repo_overrides.ledger_failed key=%s err=%s",
                key,
                str(exc),
            )
    return effective


def resolve_all_h5_knobs(
    *,
    cwd: Path,
    cfg: Any,
) -> dict[str, tuple[float, float, float]]:
    """Resolve all known H5 knob keys against *cfg* in one pass.

    Returns a mapping ``{knob_key: (base_value, multiplier, effective)}``
    for every knob whose base value can be read off *cfg* and whose
    multiplier key is present in
    ``cfg.task_overrides.huge_repo_multipliers``. Returns an empty dict
    when the repo isn't huge or the escape hatch is set.

    Used by the integration test to verify end-to-end auto-scaling
    fires uniformly.
    """
    from orchestrator.repo_size import is_huge_repo

    if not is_huge_repo(cwd, cfg=cfg):
        return {}

    # Map H5 knob name to its (config attr path) for base-value lookup.
    # All currently live directly on AutodevConfig.
    knob_sources: dict[str, str] = {
        "max_corrective_tasks_per_phase": "max_corrective_tasks_per_phase",
        "test_diag_breaker_window_s": "test_diag_breaker_window_s",
        "test_diag_breaker_threshold": "test_diag_breaker_threshold",
        "recent_evidence_max_chars_per_kind": "recent_evidence_max_chars_per_kind",
        "circuit_breaker_threshold": "circuit_breaker_threshold",
        # v0.38.0 I4: budget-shaped backoff knobs (per-event knobs
        # stay 1.0x — see ``huge_repo_multipliers`` defaults).
        "test_diag_backoff_total_budget_s": "test_diag_backoff_total_budget_s",
        "test_diag_auto_reset_window_s": "test_diag_auto_reset_window_s",
    }

    out: dict[str, tuple[float, float, float]] = {}
    for key, attr in knob_sources.items():
        base = getattr(cfg, attr, None)
        if base is None:
            continue
        try:
            base_f = float(base)
        except (TypeError, ValueError):
            continue
        effective, mult = resolve_huge_repo_value(
            key=key, base_value=base_f, cwd=cwd, cfg=cfg
        )
        if mult is None:
            continue
        out[key] = (base_f, mult, effective)
    return out


__all__ = [
    "apply_and_log_huge_repo_value",
    "resolve_all_h5_knobs",
    "resolve_huge_repo_value",
]
