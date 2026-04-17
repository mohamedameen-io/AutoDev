"""Test helper utilities."""

from __future__ import annotations

from pathlib import Path

from config.defaults import default_config
from config.loader import save_config
from config.schema import AutodevConfig, QAGatesConfig


def make_autodev_config(repo: Path) -> AutodevConfig:
    """Write a default .autodev/config.json with tournaments and QA gates disabled."""
    cfg = default_config()
    cfg.platform = "claude_code"
    cfg.tournaments.plan.enabled = False
    cfg.tournaments.impl.enabled = False
    cfg.qa_gates = QAGatesConfig(
        syntax_check=False,
        lint=False,
        build_check=False,
        test_runner=False,
        secretscan=False,
        sast_scan=False,
        mutation_test=False,
    )
    save_config(cfg, repo / ".autodev" / "config.json")
    return cfg
