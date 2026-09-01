"""Builders for Claude Code's MCP connection logs, shaped like the real files.

One JSONL file per server per run, under
`<cache>/<mangled-project-dir>/mcp-logs-<server>/<timestamp>.jsonl`. Every
record is `{debug, timestamp, sessionId, cwd}`; the `debug` string is the whole
payload, which is why these builders write the client's exact phrasing.
"""

from __future__ import annotations

import json
from pathlib import Path


def _record(message: str, session_id: str) -> dict:
    return {
        "debug": message,
        "timestamp": "2026-01-01T00:00:00.000Z",
        "sessionId": session_id,
        "cwd": "/work/project",
    }


def connected(session_id: str, transport: str = "stdio", ms: int = 451) -> dict:
    return _record(f"Successfully connected (transport: {transport}) in {ms}ms", session_id)


def http_transport(session_id: str, url: str) -> dict:
    return _record(f"Initializing HTTP transport to {url}", session_id)


def capabilities(session_id: str, name: str, version: str) -> dict:
    blob = json.dumps({"hasTools": True, "serverVersion": {"name": name, "version": version}})
    return _record(f"Connection established with capabilities: {blob}", session_id)


def connect_failed(session_id: str, ms: int, status: str | None, detail: str) -> dict:
    code = f" ({status})" if status else ""
    return _record(f"Connection failed after {ms}ms{code}: {detail}", session_id)


def calling(session_id: str, tool: str) -> dict:
    return _record(f"Calling MCP tool: {tool}", session_id)


def completed(session_id: str, tool: str, ms: int = 37) -> dict:
    return _record(f"Tool '{tool}' completed successfully in {ms}ms", session_id)


def failed(session_id: str, tool: str, ms: int = 12) -> dict:
    return _record(f"Tool '{tool}' failed in {ms}ms", session_id)


def write_server_log(
    cache_root: Path, server: str, records: list[dict], project: str = "-work-project"
) -> Path:
    directory = cache_root / project / f"mcp-logs-{server}"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "2026-01-01T00-00-00-000Z.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path
