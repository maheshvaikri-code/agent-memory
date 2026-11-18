"""Agent-facing commands (added to the `amem` group by cli.register_agent_commands)."""
from __future__ import annotations

import sys

import click

from .service import resolve_config


@click.command("serve")
@click.option("--store", default=None, help="Memory store path.")
@click.option("--repo", default=None, help="Repository root.")
@click.option("--agent", default=None, help="Default agent name (an MCP client's own name wins).")
@click.option("--allow-must", is_flag=True, help="Let agents record priority 'must' notes.")
def serve(store, repo, agent, allow_must):
    """Serve the memory over MCP (stdio). Started by an MCP client, not by hand."""
    try:
        from .mcp_server import serve as run_server
    except ImportError as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)
    run_server(resolve_config(store=store, repo=repo, agent=agent, allow_must=allow_must))


COMMANDS = [serve]


def register(group: click.Group) -> None:
    for command in COMMANDS:
        group.add_command(command)
