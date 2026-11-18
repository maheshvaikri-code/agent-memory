"""Add an MCP server entry to a client's JSON config without disturbing anything else."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple, Union

from . import InstallError

_BOM = "﻿"


def _load(text: Optional[str]) -> Tuple[Dict[str, Any], str]:
    """(parsed object, leading BOM or ""). A missing or blank file is an empty object."""
    if text is None:
        return {}, ""
    bom = _BOM if text.startswith(_BOM) else ""
    body = text[len(bom):]
    if not body.strip():
        return {}, bom
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise InstallError(f"the existing config is not valid JSON ({exc}); fix it first") from None
    if not isinstance(data, dict):
        raise InstallError("the existing config is not a JSON object")
    return data, bom


def _indent(text: Optional[str]) -> Union[int, str]:
    """The indentation the existing file uses (a tab, or a space count); 2 when unknown."""
    for line in (text or "").splitlines()[1:]:
        stripped = line.lstrip(" \t")
        if stripped and stripped != line:
            lead = line[:len(line) - len(stripped)]
            return "\t" if lead[0] == "\t" else len(lead)
    return 2


def merge_mcp_servers(existing_text: Optional[str], name: str, entry: Dict[str, Any]) -> str:
    """The config text with `mcpServers[name] = entry`. Other servers and every other key are
    kept (in their order, unicode unescaped, same indentation). An entry that is already exactly
    right returns the existing text untouched. Invalid or oddly shaped JSON is refused."""
    data, bom = _load(existing_text)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise InstallError("'mcpServers' in the existing config is not an object")
    if existing_text is not None and name in servers and servers[name] == entry:
        return existing_text
    servers[name] = entry
    return bom + json.dumps(data, indent=_indent(existing_text), ensure_ascii=False) + "\n"


def has_server(text: Optional[str], name: str) -> bool:
    try:
        servers = _load(text)[0].get("mcpServers", {})
    except InstallError:
        return False
    return isinstance(servers, dict) and name in servers
