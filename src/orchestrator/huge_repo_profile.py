"""v0.39.0 (Cluster A1): central huge-repo profile resolver.

A single function, :func:`apply_huge_repo_profile`, returns an *effective*
:class:`config.schema.AutodevConfig` with huge-repo-sensible overrides
applied. It is:

* **Non-destructive** — deep-copies the input cfg; never mutates the
  caller's object and never writes ``.autodev/config.json``.
* **Idempotent** — applying twice yields an equal config (the only knob
  it flips is gated on "not already enabled").
* **Escape-hatch-preserving** — short-circuits to identity when the repo
  isn't huge, which includes the ``huge_repo_overrides_disabled`` master
  escape hatch (honored inside :func:`orchestrator.repo_size.is_huge_repo`).

The profile's NEW live effects today are:

* Auto-enabling ``treat_unrunnable_tests_as_no_tests`` on a huge AND
  unbuildable repo (e.g. a C++/CMake monorepo whose tests AutoDev can't
  build/run in this environment).
* Auto-enabling ``worktree_sparse_checkout_enabled`` on any huge repo
  (when not already set). The execute-phase worktree path keys its
  sparse decision off ``worktree_huge_repo_mode`` (which already
  defaults sparse ON for huge repos), but other consumers — and the
  documented huge-repo invariant — read the explicit
  ``worktree_sparse_checkout_enabled`` flag; flipping it here makes the
  effective config self-consistent so a huge-repo run is sparse-by-default
  through every code path that reads the flag. Guarded on "not already
  enabled" so it is idempotent and an operator opt-in is never clobbered.

Parallelism and the guardrail / circuit-breaker / test-diagnosis knob
scaling are ALREADY auto-applied elsewhere (``resolve_parallelism``, the
H5 resolver) — this module deliberately does NOT duplicate them.
"""

from __future__ import annotations

from autologging import get_logger
from orchestrator.repo_size import is_huge_repo
from qa.detect import is_repo_unbuildable


logger = get_logger(__name__)


def apply_huge_repo_profile(cfg, cwd, *, capacity=None):
    """Return an EFFECTIVE config with huge-repo-sensible overrides applied.

    Args:
        cfg: The incoming :class:`config.schema.AutodevConfig`.
        cwd: Repository root (``Path``).
        capacity: Optional :class:`runtime.repo_probe.RepoCapacity`.
            Currently unused — reserved so future profile rules can key
            off the richer capacity signal without changing the call site.

    Returns:
        The SAME ``cfg`` object (identity) on a small repo or when the
        escape hatch is set; otherwise a deep copy with the profile
        applied.
    """
    if not is_huge_repo(cwd, cfg=cfg):
        # Small repo / huge_repo_overrides_disabled → identity, no copy.
        return cfg
    eff = cfg.model_copy(deep=True)
    applied: list[tuple[str, object, object]] = []
    if is_repo_unbuildable(cwd) and not eff.treat_unrunnable_tests_as_no_tests:
        eff.treat_unrunnable_tests_as_no_tests = True
        applied.append(("treat_unrunnable_tests_as_no_tests", False, True))
    # huge-repo follow-up: auto-enable sparse worktrees on huge repos so
    # every code path that reads the explicit flag (and the documented
    # huge-repo invariant) agrees with the ``worktree_huge_repo_mode``
    # default. Guarded on "not already enabled" → idempotent + never
    # clobbers an operator opt-in. Mirrors the
    # ``treat_unrunnable_tests_as_no_tests`` override pattern above.
    if not eff.worktree_sparse_checkout_enabled:
        eff.worktree_sparse_checkout_enabled = True
        applied.append(("worktree_sparse_checkout_enabled", False, True))
    if applied:
        logger.info(
            "orchestrator.huge_repo_profile_applied",
            cwd=str(cwd),
            applied=applied,
        )
    return eff


__all__ = ["apply_huge_repo_profile"]
