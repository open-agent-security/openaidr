"""Recovering `(server, tool)` from an agent's tool name — per kind, never globally.

Upstream parsers do not reliably supply the MCP server name, and it is the key
the whole downstream join depends on, so recovering it is part of this contract.
"""

from __future__ import annotations

_CLAUDE_PREFIX = "mcp__"
_CLAUDE_SEP = "__"


def split_tool_name(agent_kind: str | None, raw: str) -> tuple[str | None, str]:
    """Split a raw tool name into `(mcp_server, tool_name)` for one agent kind.

    An unknown kind returns the name whole with no server. There is deliberately
    no general fallback pattern: applied to the wrong kind it does not fail to
    find a server, it finds the wrong one — which then resolves to the wrong
    component downstream.
    """
    if agent_kind == "claude-code":
        return _split_claude_code(raw)
    # Codex's legacy flattened MCP names use this exact delimiter. New
    # callable namespaces may be sanitized/hashed aliases, not server ids;
    # preserve those whole rather than guessing a configured server.
    if agent_kind == "codex" and "." not in raw and raw.startswith("mcp__"):
        server, separator, tool = raw[5:].partition("__")
        if separator and server and tool:
            return server, tool
    return None, raw


def _split_claude_code(raw: str) -> tuple[str | None, str]:
    """Claude Code prefixes MCP tools and separates with a doubled underscore."""
    if not raw.startswith(_CLAUDE_PREFIX):
        return None, raw
    remainder = raw[len(_CLAUDE_PREFIX) :]
    server, separator, tool = remainder.partition(_CLAUDE_SEP)
    if not separator or not server or not tool:
        return None, raw
    return server, tool
