"""Builders for Devin CLI's local state, shaped like the real files.

The schema is the one Devin CLI 3000.11.3 applies (migrations V1 to V17), copied
from a store the program wrote. Message JSON follows the field names the binary
serialises. Nothing here is a real session: every value is synthetic.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE refinery_schema_history(
    version int4 PRIMARY KEY, name VARCHAR(255), applied_on VARCHAR(255), checksum VARCHAR(255));
CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  working_directory TEXT NOT NULL,
  backend_type TEXT NOT NULL,
  model TEXT NOT NULL,
  agent_mode TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  last_activity_at INTEGER NOT NULL, title TEXT, main_chain_id INTEGER,
  shell_last_seen_index INTEGER DEFAULT 0, cogs_json TEXT, workspace_dirs TEXT,
  hidden INTEGER NOT NULL DEFAULT 0, metadata TEXT);
CREATE TABLE prompt_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, timestamp INTEGER NOT NULL,
  session_id TEXT NOT NULL, is_shell INTEGER NOT NULL DEFAULT 0);
CREATE TABLE message_nodes (
  row_id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  node_id INTEGER NOT NULL,
  parent_node_id INTEGER,
  chat_message TEXT NOT NULL,
  created_at INTEGER NOT NULL, metadata TEXT,
  UNIQUE(session_id, node_id));
CREATE TABLE tool_call_state (
  session_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
  tool_call_json TEXT, tool_call_update_json TEXT,
  PRIMARY KEY (session_id, tool_call_id));
CREATE TABLE subagent_heads (
  session_id TEXT NOT NULL, agent_id TEXT NOT NULL, chain_node_id INTEGER NOT NULL,
  updated_at INTEGER NOT NULL, PRIMARY KEY (session_id, agent_id));
CREATE TABLE app_state (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL);
"""

#: 2026-01-01T00:00:00Z, in the epoch seconds the store writes.
T0 = 1767225600


def create_store(root: Path, *, migrations: int = 17) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "sessions.db"
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    # refinery stamps each migration with the moment it ran, so two stores
    # created one after the other differ here even where an inode is reused.
    applied = datetime.now(UTC).isoformat()
    db.executemany(
        "INSERT INTO refinery_schema_history VALUES (?, ?, ?, '')",
        [(n, f"V{n}", applied) for n in range(1, migrations + 1)],
    )
    db.execute("PRAGMA journal_mode=WAL")
    db.commit()
    db.close()
    return path


@dataclass
class Node:
    node_id: int
    parent: int | None
    message: dict
    created_at: int = T0
    metadata: dict | None = None


@dataclass
class StoredSession:
    id: str
    working_directory: str = "/work/project"
    model: str = "swe-2"
    agent_mode: str = "normal"
    created_at: int = T0
    last_activity_at: int = T0 + 60
    title: str | None = None
    main_chain_id: int | None = None
    hidden: int = 0
    nodes: list[Node] = field(default_factory=list)
    subagents: list[tuple[str, int]] = field(default_factory=list)
    prompts: list[tuple[str, int]] = field(default_factory=list)


def write_session(store: Path, session: StoredSession) -> None:
    db = sqlite3.connect(store)
    db.execute(
        "INSERT INTO sessions (id, working_directory, backend_type, model, agent_mode,"
        " created_at, last_activity_at, title, main_chain_id, hidden)"
        " VALUES (?, ?, 'local', ?, ?, ?, ?, ?, ?, ?)",
        (
            session.id,
            session.working_directory,
            session.model,
            session.agent_mode,
            session.created_at,
            session.last_activity_at,
            session.title,
            session.main_chain_id,
            session.hidden,
        ),
    )
    for node in session.nodes:
        add_node(db, session.id, node)
    for agent_id, head in session.subagents:
        db.execute(
            "INSERT INTO subagent_heads VALUES (?, ?, ?, ?)",
            (session.id, agent_id, head, session.last_activity_at),
        )
    for content, is_shell in session.prompts:
        db.execute(
            "INSERT INTO prompt_history (content, timestamp, session_id, is_shell) VALUES (?, ?, ?, ?)",
            (content, session.created_at * 1000, session.id, is_shell),
        )
    db.commit()
    db.close()


def add_node(db: sqlite3.Connection, session_id: str, node: Node) -> None:
    db.execute(
        "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message,"
        " created_at, metadata) VALUES (?, ?, ?, ?, ?, ?)",
        (
            session_id,
            node.node_id,
            node.parent,
            json.dumps(node.message),
            node.created_at,
            json.dumps(node.metadata) if node.metadata is not None else None,
        ),
    )


def append_nodes(store: Path, session_id: str, nodes: list[Node], main_chain_id: int) -> None:
    db = sqlite3.connect(store)
    for node in nodes:
        add_node(db, session_id, node)
    db.execute("UPDATE sessions SET main_chain_id = ? WHERE id = ?", (main_chain_id, session_id))
    db.commit()
    db.close()


def user(text: str, *, typed: bool | None = True) -> dict:
    metadata: dict = {}
    if typed is not None:
        metadata["is_user_input"] = typed
    return {"role": "user", "content": text, "metadata": metadata}


def assistant(
    text: str = "",
    *,
    calls: list[dict] | None = None,
    request_id: str | None = "req-1",
    model: str = "claude-sonnet-4-5",
    metrics: dict | None = None,
    finish_reason: str | None = "tool_calls",
    created_at: object = None,
) -> dict:
    metadata: dict = {"generation_model": model}
    if request_id is not None:
        metadata["request_id"] = request_id
    if metrics is not None:
        metadata["metrics"] = metrics
    if finish_reason is not None:
        metadata["finish_reason"] = finish_reason
    if created_at is not None:
        metadata["created_at"] = created_at
    message: dict = {"role": "assistant", "content": text, "metadata": metadata}
    if calls:
        message["tool_calls"] = calls
    return message


def call(call_id: str, name: str, arguments: dict | str, index: int = 0) -> dict:
    encoded = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id, "name": name, "arguments": encoded, "index": index, "kind": "function"}


def result(call_id: str, content: str, **meta: object) -> dict:
    message: dict = {"role": "tool", "tool_call_id": call_id, "content": content}
    if meta:
        message["metadata"] = {"extensions": {"chisel/tool_result_meta": meta}}
    return message


def metrics(
    input_tokens: int = 100,
    output_tokens: int = 20,
    cache_read: int | None = 1000,
    cache_creation: int | None = 50,
) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "ttft_ms": 300,
        "total_time_ms": 1500,
    }


def lock(root: Path, session_id: str, pid: int) -> None:
    locks = root / "session_locks"
    locks.mkdir(parents=True, exist_ok=True)
    (locks / f"{session_id}.lock").write_text(f"{pid}\n")


def run_log(root: Path, pid: int, lines: list[str], stamp: str = "20260101-000000") -> Path:
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / f"devin_{stamp}_{pid}.log"
    path.write_text("".join(f"{line}\n" for line in lines))
    return path


def log_line(second: int, level: str, target: str, message: str, micros: int = 0) -> str:
    """One line in the run log's format: `<RFC 3339 µs>Z  LEVEL target: message`."""
    minute, sec = divmod(second, 60)
    return f"2026-01-01T00:{minute:02d}:{sec:02d}.{micros:06d}Z  {level} {target}: {message}"


def startup(second: int = 0, *, acp: bool = False) -> list[str]:
    span = "run_acp_server" if acp else "run_repl_mode"
    return [
        log_line(second, "INFO", "chisel", "elapsed_since_main_ms=20 logging initialized"),
        log_line(
            second,
            "INFO",
            "init_cli: chisel",
            "version=3000.11.3 commit=9c803229faa4 os=linux arch=x86_64 binary=devin startup",
        ),
        log_line(second, "INFO", f"{span}: chisel", "dispatching"),
    ]


def mcp_stdio(second: int, server: str, outcome: str = "ok", micros: int = 0) -> list[str]:
    target = "toolbox::tools::mcp"
    channel = f'output_channel="MCP: {server}" server="{server}"'
    lines = [
        log_line(second, "INFO", target, f"{channel} Connecting to MCP server '{server}'", micros),
        log_line(
            second,
            "INFO",
            f"{target}::config",
            f'{channel} Starting stdio MCP server \'{server}\': "python3" ["-I", "s.py"]',
            micros + 10,
        ),
    ]
    return lines + [_outcome(second, server, outcome, micros + 250_000)]


def mcp_http(second: int, server: str, url: str, outcome: str = "ok") -> list[str]:
    target = "toolbox::tools::mcp"
    channel = f'output_channel="MCP: {server}" server="{server}"'
    return [
        log_line(second, "INFO", target, f"{channel} Connecting to MCP server '{server}'"),
        log_line(
            second,
            "INFO",
            f"{target}::config",
            f"{channel} Connecting to streamable HTTP MCP server '{server}' at: {url}",
            10,
        ),
        _outcome(second, server, outcome, 120_000),
    ]


def _outcome(second: int, server: str, outcome: str, micros: int) -> str:
    channel = f'output_channel="MCP: {server}"'
    if outcome == "ok":
        return log_line(
            second,
            "INFO",
            "toolbox::tools::mcp",
            f"{channel} MCP server '{server}' connected successfully",
            micros,
        )
    return log_line(
        second,
        "WARN",
        "toolbox::tools::mcp",
        f"{channel} MCP server '{server}' connection failed: {outcome}",
        micros,
    )


def description_failure(second: int, server: str, reason: str) -> str:
    return log_line(
        second,
        "WARN",
        "chisel_mcp::mcp_description_cache",
        f"output_channel=\"MCP\" Failed to connect to MCP server '{server}' for description: {reason}",
        500_000,
    )
