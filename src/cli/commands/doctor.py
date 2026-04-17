"""`autodev doctor` - verify CLIs and config."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from config.loader import load_config
from errors import ConfigError
from plugins.registry import discover_plugins


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str


def _probe_cli(binary: str, args: list[str], timeout: float = 5.0) -> CheckResult:
    """Run `binary args` with timeout; return a CheckResult."""
    path = shutil.which(binary)
    if path is None:
        return CheckResult(
            name=f"{binary} CLI available",
            ok=False,
            detail=f"`{binary}` not found on PATH",
        )
    try:
        proc = subprocess.run(
            [path, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=f"{binary} CLI available",
            ok=False,
            detail=f"`{binary} {' '.join(args)}` timed out after {timeout}s",
        )
    except OSError as exc:
        return CheckResult(
            name=f"{binary} CLI available",
            ok=False,
            detail=f"could not execute `{binary}`: {exc}",
        )
    if proc.returncode != 0:
        return CheckResult(
            name=f"{binary} CLI available",
            ok=False,
            detail=f"`{binary} {' '.join(args)}` exited {proc.returncode}",
        )
    first_line = (proc.stdout or proc.stderr).strip().splitlines()
    version = first_line[0] if first_line else "ok"
    return CheckResult(
        name=f"{binary} CLI available", ok=True, detail=version
    )


def _check_config(cwd: Path) -> CheckResult:
    cfg_path = cwd / ".autodev" / "config.json"
    if not cfg_path.exists():
        return CheckResult(
            name=".autodev/config.json",
            ok=False,
            detail=f"not found at {cfg_path} (run `autodev init`)",
        )
    try:
        load_config(cfg_path)
    except ConfigError as exc:
        return CheckResult(
            name=".autodev/config.json",
            ok=False,
            detail=f"invalid: {exc}",
        )
    return CheckResult(
        name=".autodev/config.json", ok=True, detail=f"valid at {cfg_path}"
    )


@click.command("doctor")
def doctor() -> None:
    """Verify CLIs installed and config valid."""
    console = Console()
    cwd = Path.cwd()

    results: list[CheckResult] = [
        _probe_cli("claude", ["--version"]),
        _probe_cli("cursor", ["--version"]),
        _check_config(cwd),
    ]

    # Require at least one of the two CLIs to succeed.
    claude_ok = results[0].ok
    cursor_ok = results[1].ok
    either_ok = claude_ok or cursor_ok

    table = Table(title="autodev doctor", show_lines=False)
    table.add_column("check", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("detail")
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        style = "green" if r.ok else "red"
        table.add_row(r.name, f"[{style}]{status}[/{style}]", r.detail)
    console.print(table)

    if not either_ok:
        console.print(
            "[red]No supported CLI (claude / cursor) is available.[/red]"
        )
    if not results[2].ok:
        console.print(
            "[yellow]Tip:[/yellow] run `autodev init` to create .autodev/config.json"
        )

    # --- Plugins section ---
    console.print()
    reg = discover_plugins()
    plugins_table = Table(title="Plugins", show_lines=False)
    plugins_table.add_column("Kind", no_wrap=True)
    plugins_table.add_column("Count", no_wrap=True)
    plugins_table.add_row("QA Gates", str(len(reg.qa_gates)))
    plugins_table.add_row("Judge Providers", str(len(reg.judges)))
    plugins_table.add_row("Agent Extensions", str(len(reg.agents)))
    console.print(plugins_table)

    # --- Guardrails section ---
    cfg_path = cwd / ".autodev" / "config.json"
    if cfg_path.exists():
        try:
            cfg = load_config(cfg_path)
            gr = cfg.guardrails
            guardrails_table = Table(title="Guardrails", show_lines=False)
            guardrails_table.add_column("Cap", no_wrap=True)
            guardrails_table.add_column("Value", no_wrap=True)
            guardrails_table.add_row(
                "max_tool_calls_per_task", str(gr.max_tool_calls_per_task)
            )
            guardrails_table.add_row(
                "max_duration_s_per_task", f"{gr.max_duration_s_per_task}s"
            )
            guardrails_table.add_row(
                "max_diff_bytes",
                f"{gr.max_diff_bytes:,} bytes",
            )
            cost_str = (
                f"${gr.cost_budget_usd_per_plan:.2f}"
                if gr.cost_budget_usd_per_plan is not None
                else "unlimited"
            )
            guardrails_table.add_row("cost_budget_usd_per_plan", cost_str)
            console.print(guardrails_table)
        except ConfigError:
            console.print("[yellow]Guardrails: config unavailable[/yellow]")

    exit_code = 0 if (either_ok and results[2].ok) else 1
    sys.exit(exit_code)
