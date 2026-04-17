"""`autodev plugins` - list discovered plugins."""

from __future__ import annotations

import click
from rich.console import Console
from rich.table import Table

from plugins.registry import discover_plugins


@click.command("plugins")
def plugins() -> None:
    """List all discovered autodev plugins."""
    console = Console()
    reg = discover_plugins()

    if reg.is_empty():
        console.print("[yellow]No plugins discovered.[/yellow]")
        console.print(
            "Install packages that declare [bold]autodev.plugins[/bold] entry points."
        )
        return

    table = Table(title="autodev plugins", show_lines=False)
    table.add_column("Name", no_wrap=True)
    table.add_column("Type", no_wrap=True)
    table.add_column("Module")

    for name, plugin in reg.qa_gates.items():
        table.add_row(name, "QA Gate", type(plugin).__module__)

    for name, plugin in reg.judges.items():
        table.add_row(name, "Judge Provider", type(plugin).__module__)

    for name, plugin in reg.agents.items():
        table.add_row(name, "Agent Extension", type(plugin).__module__)

    console.print(table)
    console.print(
        f"\nTotal: [bold]{reg.total}[/bold] plugin(s) — "
        f"{len(reg.qa_gates)} QA gate(s), "
        f"{len(reg.judges)} judge(s), "
        f"{len(reg.agents)} agent extension(s)"
    )
