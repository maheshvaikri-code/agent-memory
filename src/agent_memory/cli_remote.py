"""`amem token ...` (manage bearer tokens) and the helper behind `amem serve --http`."""
from __future__ import annotations

import json
import os
import sys
from typing import Optional

import click

from .cli_agent import _emit, _fail
from .remote.tokens import ROLES, TokenError, TokenFile
from .service import ServiceError, resolve_config


def _tokens_path(tokens: Optional[str], store: Optional[str], repo: Optional[str]) -> str:
    chosen = tokens or os.environ.get("AGENT_MEMORY_TOKENS")
    if chosen:
        return os.path.abspath(chosen)
    config = resolve_config(store=store, repo=repo)
    return os.path.join(os.path.dirname(config.store_path), "tokens.json")


def _options(fn):
    for option in reversed([
        click.option("--tokens", default=None, help="Token file (default: tokens.json beside "
                     "the store, or $AGENT_MEMORY_TOKENS)."),
        click.option("--store", default=None, help="Memory store path (locates tokens.json)."),
        click.option("--repo", default=None, help="Repository root (locates the store)."),
        click.option("--json", "as_json", is_flag=True, help="Machine-readable output."),
    ]):
        fn = option(fn)
    return fn


def _die(message: str, as_json: bool) -> None:
    _fail(ServiceError("invalid_argument", message), as_json)


@click.group("token")
def token_group():
    """Create, list and revoke the bearer tokens of the remote server."""


@token_group.command("create")
@click.argument("name")
@click.option("--role", type=click.Choice(ROLES), required=True,
              help="read-only: recall, check, status. read-write: everything an agent may do.")
@click.option("--expires-in", type=int, default=None, help="Days until the token stops working.")
@click.option("--url", default="http://127.0.0.1:8765", show_default=True,
              help="The server's public URL, for the snippet.")
@_options
def token_create(name, role, expires_in, url, tokens, store, repo, as_json):
    """Make a token. It is shown once; only its hash is kept."""
    file = TokenFile(_tokens_path(tokens, store, repo))
    try:
        token, entry = file.create_entry(name, role, expires_in_days=expires_in)
    except TokenError as exc:
        _die(str(exc), as_json)
        return
    if as_json:
        click.echo(json.dumps({"name": name, "role": role, "token": token,
                               "expires_at": entry.expires_at, "file": str(file.path)}, indent=2))
        return
    endpoint = url.rstrip("/") + "/mcp"
    _emit(f"Token for '{name}' ({role}), shown once - store it now:\n\n  {token}\n\n"
          f"MCP server URL:  {endpoint}\n"
          f"Header:          Authorization: Bearer {token}\n"
          f"Expires:         {entry.expires_at or 'never'}\n"
          f"Token file:      {file.path}")


@token_group.command("list")
@_options
def token_list(tokens, store, repo, as_json):
    """List tokens (name, role, created, expiry; never a token or its hash)."""
    file = TokenFile(_tokens_path(tokens, store, repo))
    if file.error:
        _die(file.error, as_json)
        return
    rows = [{"name": e.name, "role": e.role, "created_at": e.created_at,
             "expires_at": e.expires_at} for e in file.entries()]
    if as_json:
        click.echo(json.dumps({"tokens": rows}, indent=2))
        return
    if not rows:
        _emit("no tokens")
        return
    for row in rows:
        _emit(f"{row['name']}  {row['role']}  created {row['created_at']}  "
              f"expires {row['expires_at'] or 'never'}")


@token_group.command("revoke")
@click.argument("name")
@_options
def token_revoke(name, tokens, store, repo, as_json):
    """Stop a token from working (a running server notices on its next request)."""
    file = TokenFile(_tokens_path(tokens, store, repo))
    try:
        removed = file.revoke(name)
    except TokenError as exc:
        _die(str(exc), as_json)
        return
    if not removed:
        _die(f"no token named {name!r}", as_json)
        return
    _emit(f"revoked {name}")


def serve_http(store, repo, host, port, tokens, public_url, behind_tls) -> None:
    try:
        from .remote.server import StartupError, run_http
    except ImportError as exc:
        click.echo(f"the remote server needs the optional 'mcp' package (Python 3.10+): {exc}",
                   err=True)
        sys.exit(1)
    for option, value in (("--host", host), ("--tokens", tokens)):
        if value is not None and not value.strip():
            click.echo(f"error: {option} is empty; give a value or leave the option out", err=True)
            sys.exit(2)
    config = resolve_config(store=store, repo=repo, agent="remote", allow_must=False)
    file = TokenFile(_tokens_path(tokens, store, repo))
    try:
        run_http(config, file, "127.0.0.1" if host is None else host, 8765 if port is None else port,
                 "" if public_url is None else public_url, behind_tls)
    except StartupError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(2)


COMMANDS = [token_group]


def register(group: click.Group) -> None:
    for command in COMMANDS:
        group.add_command(command)
