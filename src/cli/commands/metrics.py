"""``autodev metrics`` CLI subgroup (v0.22.0 Phase 6).

Operator-facing wrapper around ``scripts/anti_bloat_metrics.py``: runs the
script for a commit range and renders the resulting JSONL ledger as
JSONL (default — pipe-friendly), markdown (trend table for a code review
or PR comment), or CSV (for spreadsheet ingest).

Mirrors the registration pattern of :mod:`cli.commands.secretscan_baseline`
so the orchestrator entry point is consistent across QA tooling.
"""

from __future__ import annotations

import csv
import io
import json
import subprocess
import sys
from pathlib import Path

import click


_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "anti_bloat_metrics.py"


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # Skip malformed records rather than crashing the rendered table —
            # a partially-corrupt ledger should still render the good rows.
            continue
    return out


def _render_markdown(records: list[dict]) -> str:
    """Trend table: commit | bohr_quad columns | static columns.

    Columns are deliberately compact so the table fits in a PR comment
    without wrapping. Float columns are quantised in the script already
    (round=4) so we just stringify here.
    """
    if not records:
        return "_No records — run `autodev metrics anti-bloat --from <sha>` first._\n"

    header = (
        "| commit | task | tokens | def_ratio | doc_dens | fns | loc | cc_max | dead | yap |\n"
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|\n"
    )
    rows: list[str] = []
    for r in records:
        bq = r.get("bohr_quad", {})
        st = r.get("static", {})
        rows.append(
            "| {sha} | {task} | {tok} | {dr} | {dd} | {fns} | {loc} | {cc} | {dead} | {yap} |".format(
                sha=(r.get("merged_sha", "") or "")[:7],
                task=(r.get("task_id", "") or "").replace("|", "/"),
                tok=bq.get("token_count", 0),
                dr=bq.get("defensive_ratio", 0.0),
                dd=bq.get("doc_density", 0.0),
                fns=bq.get("functions_per_file", 0),
                loc=st.get("loc_executable", 0),
                cc=st.get("cyclomatic_max", 0),
                dead=st.get("dead_symbols", 0),
                yap=r.get("yap_score", 0),
            )
        )
    return header + "\n".join(rows) + "\n"


def _render_csv(records: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "merged_sha",
            "task_id",
            "timestamp",
            "token_count",
            "defensive_ratio",
            "doc_density",
            "functions_per_file",
            "loc_executable",
            "cyclomatic_max",
            "cyclomatic_mean",
            "n_abstractions",
            "dead_symbols",
            "commented_out_blocks",
            "duplicate_clusters",
            "yap_score",
        ]
    )
    for r in records:
        bq = r.get("bohr_quad", {})
        st = r.get("static", {})
        writer.writerow(
            [
                r.get("merged_sha", ""),
                r.get("task_id", ""),
                r.get("timestamp", ""),
                bq.get("token_count", 0),
                bq.get("defensive_ratio", 0.0),
                bq.get("doc_density", 0.0),
                bq.get("functions_per_file", 0),
                st.get("loc_executable", 0),
                st.get("cyclomatic_max", 0),
                st.get("cyclomatic_mean", 0.0),
                st.get("n_abstractions", 0),
                st.get("dead_symbols", 0),
                st.get("commented_out_blocks", 0),
                st.get("duplicate_clusters", 0),
                r.get("yap_score", 0),
            ]
        )
    return buf.getvalue()


@click.group(name="metrics")
def metrics() -> None:
    """Longitudinal code-size / anti-bloat metrics (v0.22.0)."""


@metrics.command(name="anti-bloat")
@click.option(
    "--from",
    "from_sha",
    required=True,
    help="Range start SHA (exclusive).",
)
@click.option(
    "--to",
    "to_sha",
    default="HEAD",
    show_default=True,
    help="Range end SHA (inclusive).",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=None,
    help="JSONL ledger path. Defaults to .autodev/anti_bloat_history.jsonl.",
)
@click.option(
    "--cache-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Per-(sha,file) metric cache. Defaults to ~/.cache/autodev/code_size.",
)
@click.option(
    "--cwd",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd,
    show_default="current directory",
    help="Repository root.",
)
@click.option(
    "--report",
    type=click.Choice(["jsonl", "markdown", "csv"], case_sensitive=False),
    default="jsonl",
    show_default=True,
    help="Output rendering. JSONL is pipe-friendly; markdown for PR comments.",
)
@click.option(
    "--no-run",
    is_flag=True,
    default=False,
    help="Skip running the metrics script — render the existing ledger only.",
)
def anti_bloat_cmd(
    from_sha: str,
    to_sha: str,
    out_path: Path | None,
    cache_dir: Path | None,
    cwd: Path,
    report: str,
    no_run: bool,
) -> None:
    """Compute and render code-size metrics over a commit range.

    Wraps ``scripts/anti_bloat_metrics.py``. With ``--report jsonl`` (the
    default) this just runs the script and prints "Wrote N records to
    PATH"; with ``--report markdown`` or ``--report csv`` it then re-reads
    the ledger and renders it.
    """
    cwd = cwd.resolve()
    out = out_path if out_path is not None else (cwd / ".autodev" / "anti_bloat_history.jsonl")

    if not no_run:
        cmd = [
            sys.executable,
            str(_SCRIPT),
            "--from",
            from_sha,
            "--to",
            to_sha,
            "--out",
            str(out),
            "--cwd",
            str(cwd),
        ]
        if cache_dir is not None:
            cmd.extend(["--cache-dir", str(cache_dir)])
        proc = subprocess.run(cmd, check=False)
        if proc.returncode != 0:
            click.echo(f"anti_bloat_metrics.py exited {proc.returncode}", err=True)
            sys.exit(proc.returncode)

    if report == "jsonl":
        # Default path is "the script already wrote it" — nothing to do.
        # When --no-run, dump the ledger so the user can pipe it.
        if no_run and out.is_file():
            click.echo(out.read_text(encoding="utf-8"), nl=False)
        return

    records = _read_jsonl(out)
    if report == "markdown":
        click.echo(_render_markdown(records), nl=False)
    elif report == "csv":
        click.echo(_render_csv(records), nl=False)


__all__ = ["metrics", "anti_bloat_cmd"]
