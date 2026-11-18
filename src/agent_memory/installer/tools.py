"""The per-tool facts as data. Correcting a path or format is a one-line change here.

Sources are secondary (blog posts, vendor pages summarised by search) except for Claude Code's
.mcp.json and Cursor's mcp.json, which use the common `mcpServers` shape. Entries marked
`verified=False` should be re-checked against each vendor's own documentation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class ToolSpec:
    key: str
    label: str
    instruction_path: str          # repo-relative
    mode: str                      # "mcp" or "cli": which protocol text to write
    mcp_json_path: str = ""        # repo-relative JSON config with an `mcpServers` object ("" = none)
    mcp_command: bool = False      # print a registration command instead of writing a file
    front_matter: str = ""         # written once, when the instruction file is created
    verified: bool = True


TOOLS: Dict[str, ToolSpec] = {t.key: t for t in (
    ToolSpec("claude-code", "Claude Code", "CLAUDE.md", "mcp", mcp_json_path=".mcp.json"),
    ToolSpec("cursor", "Cursor", ".cursor/rules/agent-memory.mdc", "mcp",
             mcp_json_path=".cursor/mcp.json",
             front_matter="---\ndescription: Project memory protocol\nalwaysApply: true\n---\n\n"),
    ToolSpec("codex", "Codex CLI", "AGENTS.md", "mcp", mcp_command=True),
    ToolSpec("devin", "Devin", "AGENTS.md", "mcp", mcp_command=True, verified=False),
    ToolSpec("generic", "Any agent with a shell", "AGENTS.md", "cli"),
)}
