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
    if agent_kind in ("claude-code", "devin", "pi"):
        # Devin CLI names MCP tools by Claude Code's convention, verified in its
        # own hook matchers (`^mcp__`), so the same rule is correct for both.
        # Pi names them `mcp__<server>__<tool>` too, with each character of the
        # server's name that is not a letter, digit or `_` written as `_`. The
        # server returned is that written form; the Pi reader maps it back to
        # the configured name where exactly one configured name writes to it.
        return _split_claude_code(raw)
    if agent_kind == "codex" and "." not in raw:
        # Codex names an MCP tool by a namespace and a name, which its reader
        # splits from the record. A flat `mcp__<server>__<tool>`, from builds
        # that had no namespaces, uses Claude Code's delimiter exactly.
        return _split_claude_code(raw)
    # OpenCode joins a server and a tool with one `_`, and both may contain
    # `_`, so no rule on the name alone can split it. Its reader splits it
    # against the server names OpenCode is configured with (`readers.opencode`).
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
