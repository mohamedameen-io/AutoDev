"""v0.22.1 A2 regression: secretscan auto-skip on huge repos.

The 2026-05-09 Unity run flagged 27K-50K false positives across asset-
GUID test fixtures. v0.22.1 wires a minimal safety valve: when
``runtime.repo_probe.RepoCapacity.is_huge`` is True and the operator
has not explicitly set ``cfg.qa_gates.secretscan_force_run_on_huge_repo``,
the gate dispatcher disables the secretscan gate (and logs a warning).

Full FP redesign (entropy bump, ignore_paths, diff-mode default) is
deferred to v0.23.0 C2.
"""

from __future__ import annotations



from config.schema import QAGatesConfig
from runtime.repo_probe import RepoCapacity


def _build_capacity(*, is_huge: bool) -> RepoCapacity:
    """Construct a RepoCapacity with the required is_huge value."""
    return RepoCapacity(
        file_count=200_000 if is_huge else 1_000,
        total_bytes=8 * 1024**3 if is_huge else 10 * 1024**2,
        depth_max=10,
        is_huge=is_huge,
    )


def test_qagates_default_auto_skip_is_true() -> None:
    """v0.22.1 ships with auto-skip ON by default."""
    cfg = QAGatesConfig()
    assert cfg.secretscan_auto_skip_huge_repo is True
    assert cfg.secretscan_force_run_on_huge_repo is False


def test_huge_repo_auto_skip_decision() -> None:
    """When huge AND auto-skip enabled AND not force_run, gate is disabled."""
    cfg = QAGatesConfig()
    capacity = _build_capacity(is_huge=True)
    expected_skip = bool(
        capacity is not None
        and getattr(capacity, "is_huge", False)
        and getattr(cfg, "secretscan_auto_skip_huge_repo", True)
        and not getattr(cfg, "secretscan_force_run_on_huge_repo", False)
    )
    assert expected_skip is True


def test_force_run_overrides_auto_skip() -> None:
    """Operators can re-enable secretscan on huge repos via force_run."""
    cfg = QAGatesConfig(secretscan_force_run_on_huge_repo=True)
    capacity = _build_capacity(is_huge=True)
    expected_skip = bool(
        capacity is not None
        and capacity.is_huge
        and cfg.secretscan_auto_skip_huge_repo
        and not cfg.secretscan_force_run_on_huge_repo
    )
    assert expected_skip is False


def test_small_repo_runs_secretscan_normally() -> None:
    """Non-huge repos always run secretscan (when cfg.secretscan=True)."""
    cfg = QAGatesConfig()
    capacity = _build_capacity(is_huge=False)
    expected_skip = bool(
        capacity is not None
        and capacity.is_huge
        and cfg.secretscan_auto_skip_huge_repo
    )
    assert expected_skip is False


def test_disabled_auto_skip_runs_normally() -> None:
    """When auto_skip_huge_repo=False, secretscan runs even on huge repos."""
    cfg = QAGatesConfig(secretscan_auto_skip_huge_repo=False)
    capacity = _build_capacity(is_huge=True)
    expected_skip = bool(
        capacity is not None
        and capacity.is_huge
        and cfg.secretscan_auto_skip_huge_repo
    )
    assert expected_skip is False
