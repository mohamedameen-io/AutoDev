"""``autodev reset`` — clear plan state, optionally also per-run artifacts.

Default scope: ``plan.json`` + ``plan-ledger.jsonl`` (the minimum to free
``autodev plan`` to write a fresh plan).

``--hard``: additionally remove ``evidence/``, ``tournaments/``,
``sessions/``, ``debug/``, the orphan ``.lock`` file, the
``execute_worktrees`` pool directories, and (legacy v0.25.x migration
cleanup) ``delegations/``, ``responses/``, and ``inline-state.json``.

Always preserved (both modes): ``config.json``, ``spec.md``,
``secretscan-baseline.json``, ``.gitignore``, ``knowledge.jsonl``,
``rejected_lessons.jsonl``, and the v0.25.0 file index (``index.db``,
``index.db-shm``, ``index.db-wal``, ``index.state.json``). The file index
is durable and expensive to rebuild on huge repos; the knowledge ledger
is cross-run learning that should survive a reset.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from state.paths import (
    autodev_root,
    debug_dir,
    delegations_dir,
    evidence_dir,
    inline_state_path,
    ledger_path,
    lock_path,
    plan_path,
    responses_dir,
    sessions_dir,
    tournaments_dir,
)


def _default_targets(cwd: Path) -> list[Path]:
    """Paths cleared by ``autodev reset`` (no flags)."""
    return [
        plan_path(cwd),
        ledger_path(cwd),
    ]


def _hard_extra_targets(cwd: Path) -> list[Path]:
    """Additional paths cleared by ``autodev reset --hard``.

    The ``delegations_dir`` / ``responses_dir`` / ``inline_state_path``
    entries are legacy v0.25.x paths retained for migration cleanup:
    InlineAdapter was removed in v0.26.0 so these are no longer written,
    but pre-existing workspaces may carry residue that ``--hard`` should
    sweep. Scheduled for removal from this list in v0.27.0 alongside the
    hard-removal of ``platform: inline`` from the schema Literal.
    """
    root = autodev_root(cwd)
    return [
        evidence_dir(cwd),
        # Legacy v0.25.x paths — migration cleanup only; remove in v0.27.0.
        delegations_dir(cwd),
        responses_dir(cwd),
        inline_state_path(cwd),
        tournaments_dir(cwd),
        sessions_dir(cwd),
        debug_dir(cwd),
        lock_path(cwd),
        root / "execute_worktrees",
        root / "execute_worktrees_pool",
    ]


def _remove_paths(paths: list[Path]) -> list[tuple[Path, str]]:
    """Remove every path that exists. Returns ``(path, kind)`` tuples for
    the subset actually removed so callers can render an audit table.

    ``kind`` is ``"dir"`` or ``"file"`` and is captured BEFORE removal so
    the table is accurate even though the path is gone afterwards.

    Directories are removed recursively; files are unlinked; missing
    paths are no-ops (so ``reset`` is idempotent on partially-empty
    workspaces).
    """
    removed: list[tuple[Path, str]] = []
    for p in paths:
        if not p.exists():
            continue
        kind = "dir" if p.is_dir() else "file"
        if kind == "dir":
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                p.unlink()
            except OSError:
                continue
        removed.append((p, kind))
    return removed


@click.command("reset")
@click.option(
    "--hard",
    is_flag=True,
    help=(
        "Also remove evidence, tournaments, sessions, debug, .lock, "
        "execute_worktrees pool directories, and (legacy v0.25.x "
        "migration cleanup) delegations, responses, inline-state.json."
    ),
)
def reset(hard: bool) -> None:
    """Clear .autodev/plan* (destructive). ``--hard`` widens scope."""
    console = Console()
    cwd = Path.cwd()
    root = autodev_root(cwd)
    if not root.exists():
        console.print(
            "[yellow]autodev reset:[/yellow] .autodev/ not found — "
            "nothing to reset."
        )
        sys.exit(0)

    targets = _default_targets(cwd)
    if hard:
        targets += _hard_extra_targets(cwd)

    removed = _remove_paths(targets)

    if not removed:
        console.print(
            "[yellow]autodev reset:[/yellow] nothing to reset "
            "(no plan state on disk)."
        )
        sys.exit(0)

    table = Table(title="Removed", show_header=True, header_style="bold red")
    table.add_column("Path", overflow="fold")
    table.add_column("Kind", style="dim")
    for p, kind in removed:
        try:
            rel = p.relative_to(cwd)
        except ValueError:
            rel = p
        table.add_row(str(rel), kind)
    console.print(table)
    suffix = " --hard" if hard else ""
    console.print(
        f"[green]autodev reset{suffix}:[/green] removed "
        f"{len(removed)} path(s)."
    )
