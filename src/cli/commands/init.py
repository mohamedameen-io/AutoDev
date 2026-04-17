"""``autodev init`` — scaffold ``.autodev/`` and render agent files.

Creates:

- ``.autodev/config.json`` — persisted :class:`AutodevConfig`
- ``.autodev/spec.md`` — placeholder intent file
- ``.claude/agents/<role>.md`` — Claude Code agent definitions
- ``.cursor/rules/<role>.mdc`` — Cursor rules

Idempotency:

- If ``.autodev/`` exists and ``--force`` is not set, exit non-zero with a
  clear message.
- With ``--force``, overwrite all generated files in place.
"""

from __future__ import annotations

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
def init(platform: str, force: bool, inline: bool) -> None:
    """Scaffold ``.autodev/`` and render platform-native agent files."""
    cwd = Path.cwd()
    console = Console()

    autodev_dir = cwd / ".autodev"
    if autodev_dir.exists() and not force:
        console.print(
            f"[red]autodev init: {autodev_dir} already exists.[/red] "
            "Use --force to overwrite."
        )
        sys.exit(1)

    autodev_dir.mkdir(parents=True, exist_ok=True)

    # Build config, overriding platform if the user asked for a specific one.
    cfg = default_config()
    if inline:
        cfg.platform = "inline"
    else:
        platform_normalized = platform.lower()
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

    # For inline mode, also initialise the inline workspace.
    if inline:
        import asyncio

        from adapters.inline import InlineAdapter

        adapter = InlineAdapter(cwd=cwd, platform_hint="claude_code")
        asyncio.run(adapter.init_workspace(cwd, list(specs.values())))

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
    console.print(table)
    console.print(
        f"[green]autodev initialized.[/green] Platform: [bold]{cfg.platform}[/bold]."
    )
    sys.exit(0)
