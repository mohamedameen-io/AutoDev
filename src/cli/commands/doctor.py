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
@click.option(
    "--repair-worktrees",
    is_flag=True,
    help=(
        "List orphaned executor worktrees (does NOT delete). Use "
        "`autodev prune --executor-only --all` to actually remove them."
    ),
)
def doctor(repair_worktrees: bool) -> None:
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
                "max_invocations_per_task", str(gr.max_invocations_per_task)
            )
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

        # --- Index section (v0.25.0) ---
        console.print()
        _render_index_section(console, cwd, cfg_path)

    # --- v0.31.0 (Phase 5.6) extensions ---
    console.print()
    _render_language_profile_section(console, cwd)
    console.print()
    _render_adapter_fitness_section(console, cwd, claude_ok, cursor_ok)
    console.print()
    _render_orphan_worktree_section(console, cwd, repair_worktrees)
    console.print()
    _render_stale_editor_agent_files_section(console, cwd)

    exit_code = 0 if (either_ok and results[2].ok) else 1
    sys.exit(exit_code)


def _render_index_section(console: Console, cwd: Path, cfg_path: Path) -> None:
    """Render the v0.25.0 file/symbol index summary table.

    On missing index, render a one-line action ("MISSING — run autodev
    init --rebuild-index"). On query failure, surface the error but never
    abort doctor — the index is informational.
    """
    try:
        cfg = load_config(cfg_path)
    except ConfigError:
        return
    if not cfg.index_enabled:
        console.print(
            "[yellow]Index:[/yellow] disabled "
            "(cfg.index_enabled = False)"
        )
        return

    db_path = cwd / cfg.index_path
    table = Table(title="Index", show_lines=False)
    table.add_column("Field", no_wrap=True)
    table.add_column("Value", no_wrap=False)
    table.add_row("path", str(cfg.index_path))

    if not db_path.exists():
        table.add_row(
            "status",
            "[yellow]MISSING — run autodev init --rebuild-index[/yellow]",
        )
        console.print(table)
        return

    try:
        from state.file_index import IndexQuery

        meta = IndexQuery(db_path).meta_summary()
    except Exception as exc:  # noqa: BLE001 - informational table only
        table.add_row("status", f"[red]error: {exc}[/red]")
        console.print(table)
        return

    for key in (
        "file_count",
        "symbol_count",
        "last_indexed_sha",
        "last_indexed_at",
        "index_version",
    ):
        table.add_row(key, str(meta.get(key, "-")))
    console.print(table)


# ---------------------------------------------------------------------------
# v0.31.0 (Phase 5.6) extensions
# ---------------------------------------------------------------------------


def _render_language_profile_section(console: Console, cwd: Path) -> None:
    """Show top-5 languages with percentages."""
    try:
        from runtime.language_profile import compute_language_profile, top_n

        profile = compute_language_profile(cwd)
    except Exception as exc:  # noqa: BLE001 - never block doctor
        console.print(f"[yellow]Language profile:[/yellow] error: {exc}")
        return

    table = Table(title="Codebase language profile", show_lines=False)
    table.add_column("Language", no_wrap=True)
    table.add_column("Share", justify="right", no_wrap=True)
    for lang, share in top_n(profile, 5):
        table.add_row(lang, f"{share:.1%}")
    console.print(table)


def _render_adapter_fitness_section(
    console: Console, cwd: Path, claude_ok: bool, cursor_ok: bool
) -> None:
    """Score the currently-selected adapter against the language profile."""
    try:
        from adapters.fitness import (
            WARNING_THRESHOLD,
            compute_fitness_score,
            get_fitness_warning,
        )
        from runtime.language_profile import compute_language_profile

        profile = compute_language_profile(cwd)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Adapter fitness:[/yellow] error: {exc}")
        return

    cfg_path = cwd / ".autodev" / "config.json"
    selected = "auto"
    if cfg_path.exists():
        try:
            cfg = load_config(cfg_path)
            selected = str(getattr(cfg, "platform", "auto"))
        except ConfigError:
            pass

    if selected == "auto":
        if claude_ok:
            adapter_name = "claude_code"
        elif cursor_ok:
            adapter_name = "cursor"
        else:
            adapter_name = "claude_code"
    else:
        adapter_name = selected

    score = compute_fitness_score(adapter_name, profile)
    warn = get_fitness_warning(adapter_name, profile)

    table = Table(title="Adapter fitness", show_lines=False)
    table.add_column("Field", no_wrap=True)
    table.add_column("Value", no_wrap=False)
    table.add_row("selected", adapter_name)
    color = "green" if score >= WARNING_THRESHOLD else "yellow"
    table.add_row("score", f"[{color}]{score:.0f}/100[/{color}]")
    console.print(table)
    if warn is not None:
        console.print(f"[yellow]{warn}[/yellow]")


def _render_orphan_worktree_section(
    console: Console, cwd: Path, repair_worktrees: bool
) -> None:
    """Read the worktree manifest and report on orphans."""
    try:
        from orchestrator.worktree_state import find_orphans
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Worktrees:[/yellow] error: {exc}")
        return

    autodev_root = cwd / ".autodev"
    if not autodev_root.exists():
        return

    orphans = find_orphans(autodev_root)
    missing = orphans["manifest_missing_on_disk"]
    on_disk = orphans["on_disk_not_in_manifest"]
    total = len(missing) + len(on_disk)

    table = Table(title="Orphan worktrees", show_lines=False)
    table.add_column("Kind", no_wrap=True)
    table.add_column("Count", justify="right", no_wrap=True)
    table.add_row("manifest_missing_on_disk", str(len(missing)))
    table.add_row("on_disk_not_in_manifest", str(len(on_disk)))
    console.print(table)

    if total == 0:
        console.print("[green]No orphan worktrees detected.[/green]")
        return

    if repair_worktrees:
        list_table = Table(title="Orphan paths (NOT deleted)", show_lines=False)
        list_table.add_column("Kind", no_wrap=True)
        list_table.add_column("Path", overflow="fold")
        for p in missing:
            list_table.add_row("manifest_missing", p)
        for p in on_disk:
            list_table.add_row("on_disk_only", p)
        console.print(list_table)
        console.print(
            "[yellow]Note:[/yellow] doctor never deletes. "
            "Use `autodev prune --executor-only --all` to remove on-disk "
            "orphans, or `autodev reset --hard` for nuclear cleanup."
        )
    else:
        console.print(
            f"[yellow]{total} orphan(s) detected.[/yellow] "
            "Re-run with `--repair-worktrees` to list paths, then "
            "`autodev prune --executor-only --all` to clean up."
        )


def _render_stale_editor_agent_files_section(
    console: Console, cwd: Path
) -> None:
    """Warn when ``.claude/agents/`` or ``.cursor/rules/`` is older than
    ``.autodev/config.json`` (suggests a config change wasn't followed
    by an ``autodev init``)."""
    cfg_path = cwd / ".autodev" / "config.json"
    if not cfg_path.exists():
        return
    try:
        config_mtime = cfg_path.stat().st_mtime
    except OSError:
        return

    targets = [
        cwd / ".claude" / "agents",
        cwd / ".cursor" / "rules",
    ]
    rows: list[tuple[str, str, str]] = []
    stale_paths: list[str] = []
    for target in targets:
        if not target.exists():
            rows.append((str(target.relative_to(cwd)), "missing", "-"))
            continue
        try:
            t_mtime = max(
                (p.stat().st_mtime for p in target.rglob("*") if p.is_file()),
                default=target.stat().st_mtime,
            )
        except OSError:
            rows.append((str(target.relative_to(cwd)), "unreadable", "-"))
            continue
        if t_mtime < config_mtime:
            rows.append((str(target.relative_to(cwd)), "STALE", "older than config"))
            stale_paths.append(str(target.relative_to(cwd)))
        else:
            rows.append((str(target.relative_to(cwd)), "fresh", "newer than config"))

    table = Table(title="Editor agent files", show_lines=False)
    table.add_column("Path", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Detail")
    for path, status, detail in rows:
        if status == "STALE":
            color = "yellow"
        elif status == "fresh":
            color = "green"
        else:
            color = "dim"
        table.add_row(path, f"[{color}]{status}[/{color}]", detail)
    console.print(table)

    if stale_paths:
        console.print(
            "[yellow]Stale editor agent files detected[/yellow] "
            f"({', '.join(stale_paths)}). Re-run `autodev init --force` to "
            "regenerate them from the current config."
        )
