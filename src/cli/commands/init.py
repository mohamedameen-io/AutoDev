"""``autodev init`` — scaffold ``.autodev/`` and render agent files.

Creates:

- ``.autodev/config.json`` — persisted :class:`AutodevConfig`
- ``.autodev/spec.md`` — placeholder intent file
- ``.claude/agents/<role>.md`` — Claude Code agent definitions
- ``.cursor/rules/<role>.mdc`` — Cursor rules
- ``.autodev/index.db`` — sqlite-FTS5 file/symbol index (v0.25.0)

Idempotency:

- If ``.autodev/`` exists and ``--force`` is not set, exit non-zero with a
  clear message.
- With ``--force``, overwrite all generated files in place.
- ``--rebuild-index`` forces a full index rebuild without otherwise
  touching scaffolding (gated independently of ``--force``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from agents import build_registry
from agents.render_claude import render_claude_agents
from agents.render_cursor import render_cursor_rules
from config.defaults import default_config
from config.loader import save_config


_SPEC_TEMPLATE = """# Project Intent

<!-- This is the autodev spec. Describe, in plain English, what you want built. -->

## Goal

(Replace this with a short statement of what you want to ship.)

## Constraints

- (Platforms, languages, frameworks, performance budgets, etc.)

## Non-goals

- (Things explicitly out of scope for this iteration.)

## Success criteria

- (Observable, testable outcomes that prove the goal is met.)
"""


@click.command("init")
@click.option(
    "--platform",
    type=click.Choice(["claude", "cursor", "auto"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="Target platform for rendered agent files.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite existing .autodev/ state and regenerate agent files.",
)
@click.option(
    "--inline",
    is_flag=True,
    help="Configure for inline (agent-embedded) mode.",
)
@click.option(
    "--rebuild-index",
    is_flag=True,
    help=(
        "Force a full rebuild of the file/symbol index (.autodev/index.db) "
        "even when .autodev/ already exists. Gated independently of --force."
    ),
)
def init(platform: str, force: bool, inline: bool, rebuild_index: bool) -> None:
    """Scaffold ``.autodev/`` and render platform-native agent files."""
    cwd = Path.cwd()
    console = Console()

    autodev_dir = cwd / ".autodev"
    if autodev_dir.exists() and not force and not rebuild_index:
        console.print(
            f"[red]autodev init: {autodev_dir} already exists.[/red] "
            "Use --force to overwrite, or --rebuild-index to refresh "
            "the index without touching the rest."
        )
        sys.exit(1)

    autodev_dir.mkdir(parents=True, exist_ok=True)

    platform_normalized = platform.lower()

    # Build config, overriding platform if the user asked for a specific one.
    cfg = default_config()
    if inline:
        cfg.platform = "inline"
    else:
        if platform_normalized == "claude":
            cfg.platform = "claude_code"
        elif platform_normalized == "cursor":
            cfg.platform = "cursor"
        else:
            cfg.platform = "auto"

    config_path = autodev_dir / "config.json"
    save_config(cfg, config_path)

    spec_path = autodev_dir / "spec.md"
    if force or not spec_path.exists():
        spec_path.write_text(_SPEC_TEMPLATE, encoding="utf-8")

    # Render platform-native agent files.
    specs = build_registry(cfg)
    claude_paths = render_claude_agents(specs, cwd)
    cursor_paths = render_cursor_rules(specs, cwd)

    slash_path: Path | None = None
    if platform_normalized != "cursor":
        from adapters.inline_config import render_claude_slash_command

        commands_dir = cwd / ".claude" / "commands"
        commands_dir.mkdir(parents=True, exist_ok=True)
        slash_path = commands_dir / "autodev.md"
        slash_path.write_text(render_claude_slash_command(), encoding="utf-8")

    # For inline mode, also initialise the inline workspace.
    if inline:
        import asyncio

        from adapters.inline import InlineAdapter

        adapter = InlineAdapter(cwd=cwd, platform_hint="claude_code")
        asyncio.run(adapter.init_workspace(cwd, list(specs.values())))

    # v0.25.0: build the file/symbol index (sqlite-FTS5 at .autodev/index.db).
    # Synchronous on small/medium repos; spawned in a background subprocess
    # on huge repos (RepoCapacity.is_huge AND cfg.index_huge_repo_async_init).
    # Failures here are surfaced but never block init — the per-trigger hook
    # in execute/plan/resume will retry on the next invocation.
    index_summary: str | None = None
    if cfg.index_enabled:
        try:
            from runtime.repo_probe import probe_repo
            from state.file_index import IndexBuilder
            from state.paths import index_db_path

            db_path = index_db_path(cwd)
            capacity = probe_repo(cwd)
            if capacity.is_huge and cfg.index_huge_repo_async_init:
                # Spawn a detached subprocess; the spawned process sets the
                # ``.autodev/index.db.building`` marker before schema creation
                # so the per-trigger hook in execute/plan/resume detects it
                # and skips the incremental refresh until the build completes.
                log_path = autodev_dir / "index-build.log"
                log_handle = log_path.open("ab")
                cmd = [
                    sys.executable,
                    "-m",
                    "state.file_index",
                    "build-full",
                    "--cwd",
                    str(cwd),
                    "--db",
                    str(db_path),
                ]
                subprocess.Popen(  # noqa: S603 - executable is sys.executable
                    cmd,
                    cwd=str(cwd),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                )
                index_summary = (
                    "background build (huge repo) — see "
                    f"{log_path.relative_to(cwd)}"
                )
                console.print(
                    "[yellow]Index build running in background.[/yellow] "
                    "Run [bold]autodev doctor[/bold] to check progress."
                )
            else:
                with console.status("Building file/symbol index..."):
                    stats = IndexBuilder.build_full(cwd, db_path)
                index_summary = (
                    f"{stats.file_count} files, {stats.symbol_count} symbols "
                    f"({stats.duration_ms} ms)"
                )
                console.print(
                    f"[green]Indexed {stats.file_count} files, "
                    f"{stats.symbol_count} symbols in "
                    f"{stats.duration_ms} ms.[/green]"
                )
        except Exception as exc:  # noqa: BLE001 - never block init on index errors
            console.print(
                f"[yellow]Index build failed:[/yellow] {exc} "
                "(execute/plan/resume will retry on next invocation)"
            )

    # Pretty console summary.
    table = Table(title="autodev init")
    table.add_column("File", style="cyan", no_wrap=False)
    table.add_column("Purpose")
    table.add_row(str(config_path.relative_to(cwd)), "autodev configuration")
    table.add_row(str(spec_path.relative_to(cwd)), "intent / spec stub")
    for p in claude_paths:
        table.add_row(str(p.relative_to(cwd)), "Claude Code agent")
    for p in cursor_paths:
        table.add_row(str(p.relative_to(cwd)), "Cursor rule")
    if slash_path is not None:
        table.add_row(str(slash_path.relative_to(cwd)), "slash command (/autodev)")
    if index_summary is not None:
        table.add_row(cfg.index_path, f"file/symbol index — {index_summary}")
    console.print(table)
    console.print(
        f"[green]autodev initialized.[/green] Platform: [bold]{cfg.platform}[/bold]."
    )
    sys.exit(0)
