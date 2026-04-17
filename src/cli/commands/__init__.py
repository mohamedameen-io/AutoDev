"""Subcommand registration."""

from __future__ import annotations

import click

from cli.commands import (
    doctor,
    execute,
    init,
    logs,
    plan,
    plugins,
    prune,
    reset,
    resume,
    status,
    tournament,
)


def register_commands(group: click.Group) -> None:
    """Attach all subcommands to the top-level click group."""
    group.add_command(init.init)
    group.add_command(plan.plan)
    group.add_command(execute.execute)
    group.add_command(resume.resume)
    group.add_command(status.status)
    group.add_command(tournament.tournament)
    group.add_command(doctor.doctor)
    group.add_command(logs.logs)
    group.add_command(reset.reset)
    group.add_command(prune.prune)
    group.add_command(plugins.plugins)
