"""Subcommand registration."""

from __future__ import annotations

import click

from cli.commands import (
    doctor,
    execute,
    init,
    intake,
    logs,
    metrics,
    plan,
    plugins,
    prune,
    requeue,
    reset,
    resume,
    rewind,
    secretscan_baseline,
    status,
    tournament,
)


def register_commands(group: click.Group) -> None:
    """Attach all subcommands to the top-level click group."""
    group.add_command(init.init)
    group.add_command(plan.plan)
    group.add_command(intake.intake)
    group.add_command(execute.execute)
    group.add_command(resume.resume)
    group.add_command(status.status)
    group.add_command(tournament.tournament)
    group.add_command(doctor.doctor)
    group.add_command(logs.logs)
    group.add_command(reset.reset)
    group.add_command(requeue.requeue)
    group.add_command(rewind.rewind)
    group.add_command(prune.prune)
    group.add_command(plugins.plugins)
    group.add_command(secretscan_baseline.secretscan)
    # v0.22.0 Phase 6: longitudinal anti-bloat metrics CLI.
    group.add_command(metrics.metrics)
