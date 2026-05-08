"""``autodev secretscan`` CLI subgroup (v0.19.0)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from qa.secretscan_baseline import compute_baseline


@click.group(name="secretscan")
def secretscan() -> None:
    """Secret-scan utilities."""


@secretscan.command(name="baseline")
@click.option(
    "--cwd",
    "cwd",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd,
    show_default="current directory",
    help="Repository root to scan.",
)
def baseline_cmd(cwd: Path) -> None:
    """Refresh the per-repo secretscan baseline.

    Scans the repository and writes the current finding keys to
    ``.autodev/secretscan-baseline.json``. Subsequent gate runs (when
    ``cfg.qa_gates.secretscan_baseline_enabled``) report only net-new
    findings vs. this snapshot.
    """
    cwd_resolved = cwd.resolve()
    keys = asyncio.run(compute_baseline(cwd_resolved))
    target = cwd_resolved / ".autodev" / "secretscan-baseline.json"
    click.echo(
        f"Baseline written: {target.relative_to(cwd_resolved)} "
        f"({len(keys)} key{'s' if len(keys) != 1 else ''})"
    )


__all__ = ["secretscan", "baseline_cmd"]
