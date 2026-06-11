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

The profile's only NEW live effect today is auto-enabling
``treat_unrunnable_tests_as_no_tests`` on a huge AND unbuildable repo
(e.g. a C++/CMake monorepo whose tests AutoDev can't build/run in this
environment). Sparse checkout, async-init default, parallelism, and the
guardrail / circuit-breaker / test-diagnosis knob scaling are ALREADY
auto-applied elsewhere (sparse worktree mode, ``resolve_parallelism``,
the H5 resolver) — this module deliberately does NOT duplicate them.
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
    if applied:
        logger.info(
            "orchestrator.huge_repo_profile_applied",
            cwd=str(cwd),
            applied=applied,
        )
    return eff


__all__ = ["apply_huge_repo_profile"]
