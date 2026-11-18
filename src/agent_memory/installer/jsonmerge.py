"""Add an MCP server entry to a client's JSON config without disturbing anything else."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple, Union

from . import InstallError

_BOM = "﻿"


def _no_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    seen: Dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise InstallError(f"the existing config has the key {key!r} more than once; "
                               "remove the duplicate by hand first")
        seen[key] = value
    return seen


def _load(text: Optional[str]) -> Tuple[Dict[str, Any], str]:
    """(parsed object, leading BOM or ""). A missing or blank file is an empty object."""
    if text is None:
        return {}, ""
    bom = _BOM if text.startswith(_BOM) else ""
    body = text[len(bom):]
    if not body.strip():
        return {}, bom
    try:
        data = json.loads(body, object_pairs_hook=_no_duplicates)
    except json.JSONDecodeError as exc:
        raise InstallError(f"the existing config is not valid JSON ({exc}); comments and trailing "
                           "commas are not supported, so the file must be fixed by hand first") from None
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


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):
        return repr(value)


def merge_mcp_servers(existing_text: Optional[str], name: str, entry: Dict[str, Any]) -> str:
    """The config text with `mcpServers[name] = entry`. Other servers and every other key are
    kept (in their order, unicode unescaped, same indentation). When `name` already exists only
    the keys in `entry` are set on it; the user's other keys (env, cwd, disabled, ...) stay. A
    result equal to the existing text is returned as that same text. Invalid JSON, duplicate
    keys, non-finite numbers and oddly shaped configs are refused."""
    data, bom = _load(existing_text)
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise InstallError("'mcpServers' in the existing config is not an object")
    current = servers.get(name)
    before = _canonical(current)
    if isinstance(current, dict):
        current.update(entry)
    else:
        servers[name] = dict(entry)
    if existing_text is not None and _canonical(servers[name]) == before:
        return existing_text
    try:
        dumped = json.dumps(data, indent=_indent(existing_text), ensure_ascii=False,
                            allow_nan=False)
    except ValueError:
        raise InstallError("the existing config contains a number JSON cannot represent "
                           "(NaN, Infinity or out of range); fix it by hand first") from None
    return bom + dumped + "\n"


def has_server(text: Optional[str], name: str) -> bool:
    try:
        servers = _load(text)[0].get("mcpServers", {})
    except InstallError:
        return False
    return isinstance(servers, dict) and name in servers
