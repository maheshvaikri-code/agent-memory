"""Wire a repository to an agent tool: MCP config plus a marked instruction block."""
from __future__ import annotations


class InstallError(Exception):
    """The install cannot be done safely; nothing was written."""
