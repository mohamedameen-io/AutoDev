"""v0.26.0 — regression coverage for subprocess adapters + tournaments.

v0.25.4 introduced ``TournamentAdapterMismatchError`` and a preflight
check to fail-fast when ``InlineAdapter`` was paired with any enabled
tournament. v0.26.0 removed InlineAdapter, the typed error, AND the
preflight helper — the mismatch is now unrepresentable.

What remains is a single smoke test: an ``Orchestrator`` wired to a
subprocess-like adapter with all three tournaments enabled must
construct cleanly (no spurious raise). Kept as a guard against future
regressions of the form "someone reintroduces a guard that bites the
happy subprocess path".
"""

from __future__ import annotations

from pathlib import Path

from agents import build_registry
from config.defaults import default_config
from orchestrator import Orchestrator

from stub_adapter import StubAdapter


def test_subprocess_adapter_with_all_tournaments_enabled_constructs_cleanly(
    tmp_path: Path,
) -> None:
    """Subprocess-like adapter + all three tournaments enabled is the
    canonical happy path. Orchestrator construction must NOT raise."""
    cfg = default_config()
    cfg.tournaments.plan.enabled = True
    cfg.tournaments.impl.enabled = True
    cfg.tournaments.phase_review.enabled = True
    registry = build_registry(cfg)
    orch = Orchestrator(
        cwd=tmp_path,
        cfg=cfg,
        adapter=StubAdapter({}),
        registry=registry,
        session_id="sess-test-subprocess",
    )
    # Sanity: all three tournament flags survived construction.
    assert orch.cfg.tournaments.plan.enabled
    assert orch.cfg.tournaments.impl.enabled
    assert orch.cfg.tournaments.phase_review.enabled
