"""Builders for the Cursor CLI's local state, shaped like the real files.

The shapes are the ones `agent-cli-local` 2026.10.19-6bbf2c4 wrote when run
against a mock model: a store's `meta` row is hex of UTF-8 JSON, its blobs are
keyed by the SHA-256 of their bytes, the root and turns are protobuf with the
field numbers of `agent.v1`, and the prompt messages are JSON with Cursor's
typed outcome under `providerOptions.cursor.highLevelToolCallResult`. The debug
log lines and transcript rows copy the probe's. Nothing here is a real session:
every value is synthetic.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: 2026-10-21T00:00:00Z, in the milliseconds the store writes.
T0 = datetime(2026, 10, 21, tzinfo=UTC)


def ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


# --- protobuf encoding -------------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def varint_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def bytes_field(number: int, value: bytes | str) -> bytes:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return _varint((number << 3) | 2) + _varint(len(data)) + data


# --- a conversation ----------------------------------------------------------


@dataclass
class Conversation:
    """One store's conversation, built a message at a time.

    `messages` are the JSON prompt messages in order; `turns` are protobuf
    turns, each a user message and its steps."""

    agent_id: str
    cwd: str = "/work/app"
    title: str | None = None
    subagent: dict[str, str] | None = None
    messages: list[dict[str, object]] = field(default_factory=list)
    turns: list[tuple[bytes, list[bytes]]] = field(default_factory=list)
    _blobs: dict[str, bytes] = field(default_factory=dict)
    _clock: datetime = T0
    _counter: int = 0

    def __post_init__(self) -> None:
        self.messages.append({"role": "system", "content": "You are an AI coding assistant."})
        self.messages.append(
            {
                "role": "user",
                "content": f"<user_info>\nOS Version: linux\n\nWorkspace Path: {self.cwd}\n</user_info>",
            }
        )

    def blob(self, data: bytes) -> bytes:
        identifier = hashlib.sha256(data).digest()
        self._blobs[identifier.hex()] = data
        return identifier

    def tick(self, millis: int = 100) -> datetime:
        self._clock += timedelta(milliseconds=millis)
        return self._clock

    def next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}{self._counter:04d}"

    def user(self, words: str) -> None:
        at = self.tick()
        self.messages.append(
            {
                "role": "user",
                "content": [{"type": "text", "text": f"<user_query>\n{words}\n</user_query>"}],
            }
        )
        user_message = (
            bytes_field(1, words) + bytes_field(2, self.next_id("um-")) + varint_field(25, ms(at))
        )
        self.turns.append((self.blob(user_message), []))

    def say(self, text: str) -> str:
        at = self.tick()
        message_id = self.next_id("msg-")
        self.messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": text}], "id": message_id}
        )
        step = bytes_field(
            1, bytes_field(1, text) + varint_field(2, ms(at)) + varint_field(3, ms(at))
        )
        self.turns[-1][1].append(self.blob(step))
        return message_id

    def call(
        self,
        tool: str,
        args: Mapping[str, object],
        output: Mapping[str, object] | None,
        *,
        result: str = "",
        raw_errors: list[str] | None = None,
        case: int = 1,
        duration: int = 50,
    ) -> str:
        """An assistant message with one tool call, and its result when
        `output` (Cursor's typed outcome) is given."""
        call_id = self.next_id("call_")
        message_id = self.next_id("msg-")
        content: list[dict[str, object]] = [
            {"type": "tool-call", "toolCallId": call_id, "toolName": tool, "args": args}
        ]
        self.messages.append({"role": "assistant", "content": content, "id": message_id})
        started = self.tick()
        completed = started + timedelta(milliseconds=duration)
        self.tick(duration)
        inner = (
            bytes_field(case, b"\x0a\x00")
            + bytes_field(57, call_id)
            + varint_field(59, ms(started))
        )
        if output is not None:
            inner += varint_field(60, ms(completed))
        self.turns[-1][1].append(self.blob(bytes_field(2, inner)))
        if output is not None:
            outcome: dict[str, object] = {"output": output, "isError": "success" not in output}
            if raw_errors:
                outcome["rawErrorMessages"] = raw_errors
            self.messages.append(
                {
                    "role": "tool",
                    "content": [
                        {
                            "type": "tool-result",
                            "toolCallId": call_id,
                            "toolName": tool,
                            "result": result,
                        }
                    ],
                    "id": call_id,
                    "providerOptions": {"cursor": {"highLevelToolCallResult": outcome}},
                }
            )
        return call_id

    def write(self, chats: Path) -> Path:
        """Write the store under `chats/<hash>/<agent_id>/`; returns its path."""
        directory = chats / hashlib.md5(self.cwd.encode()).hexdigest() / self.agent_id
        directory.mkdir(parents=True, exist_ok=True)
        root_id = self.blob(self.root())
        meta: dict[str, object] = {
            "agentId": self.agent_id,
            "latestRootBlobId": root_id.hex(),
            "name": "New Agent",
            "mode": "default",
            "createdAt": ms(T0),
        }
        if self.subagent is not None:
            meta["subagentInfo"] = self.subagent
        path = directory / "store.db"
        path.unlink(missing_ok=True)
        connection = sqlite3.connect(path)
        try:
            connection.executescript(
                "PRAGMA journal_mode = WAL;"
                "CREATE TABLE IF NOT EXISTS blobs (id TEXT PRIMARY KEY, data BLOB);"
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"
            )
            connection.executemany("INSERT INTO blobs VALUES (?, ?)", sorted(self._blobs.items()))
            connection.execute(
                "INSERT INTO meta VALUES ('0', ?)", (json.dumps(meta).encode("utf-8").hex(),)
            )
            connection.commit()
        finally:
            connection.close()
        if self.subagent is None:
            sidecar: dict[str, object] = {
                "schemaVersion": 1,
                "createdAtMs": ms(T0),
                "hasConversation": True,
                "updatedAtMs": ms(self._clock),
                "cwd": self.cwd,
            }
            if self.title is not None:
                sidecar["title"] = self.title
            (directory / "meta.json").write_text(json.dumps(sidecar), encoding="utf-8")
        return path

    def root(self) -> bytes:
        """The root blob's bytes, its prompt messages and turns stored."""
        message_ids = [self.blob(json.dumps(m).encode("utf-8")) for m in self.messages]
        turn_ids = []
        for user_id, steps in self.turns:
            agent_turn = bytes_field(1, user_id) + b"".join(bytes_field(2, s) for s in steps)
            turn_ids.append(self.blob(bytes_field(1, agent_turn)))
        return (
            b"".join(bytes_field(1, i) for i in message_ids)
            + b"".join(bytes_field(8, i) for i in turn_ids)
            + bytes_field(9, f"file://{self.cwd}")
            + varint_field(26, ms(T0))
        )

    def write_desktop(
        self,
        user_data: Path,
        *,
        state: str = "base64",
        extra: Mapping[str, object] | None = None,
    ) -> Path:
        """Write the conversation to the desktop app's `state.vscdb`, as
        Cursor 3.27.12 wrote it: a `composerData:<id>` row and one
        `agentKv:blob:<hex>` row for each blob. `state` is how the root is
        kept: `base64` (`~` and base64), `hex`, or `checkpoint` (the older
        `agentKv:checkpoint:<id>` pointer). `self.subagent` takes the desktop
        app's names: `rootParentConversationId`, `parentComposerId`,
        `toolCallId`."""
        root = self.root()
        document: dict[str, object] = {
            "_v": 18,
            "composerId": self.agent_id,
            "status": "completed",
            "createdAt": ms(T0),
            "lastUpdatedAt": ms(self._clock),
            "unifiedMode": "agent",
            "isNAL": True,
            "modelConfig": {"modelName": "default", "maxMode": False},
            "workspaceIdentifier": {
                "id": "fd9b89d16d6d68320d138c123fa21259",
                "uri": {"$mid": 1, "fsPath": self.cwd, "path": self.cwd, "scheme": "file"},
            },
        }
        if self.title is not None:
            document["name"] = self.title
        if self.subagent is not None:
            document["subagentInfo"] = {"subagentType": 3, **self.subagent}
        rows: dict[str, object] = {}
        if state == "base64":
            document["conversationState"] = "~" + base64.b64encode(root).decode("ascii")
        elif state == "hex":
            document["conversationState"] = root.hex()
        elif state == "checkpoint":
            rows[f"agentKv:checkpoint:{self.agent_id}"] = self.blob(root).hex()
        document.update(extra or {})
        rows[f"composerData:{self.agent_id}"] = json.dumps(document)
        for identifier, data in self._blobs.items():
            rows[f"agentKv:blob:{identifier}"] = data
        return write_state_db(user_data, rows)


def write_state_db(user_data: Path, rows: Mapping[str, object]) -> Path:
    """Add `rows` to the desktop app's `cursorDiskKV` table, in WAL mode."""
    directory = user_data / "User" / "globalStorage"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "state.vscdb"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            "PRAGMA journal_mode = WAL;"
            "CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);"
            "CREATE TABLE IF NOT EXISTS cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);"
        )
        connection.executemany("INSERT INTO cursorDiskKV VALUES (?, ?)", sorted(rows.items()))
        connection.commit()
    finally:
        connection.close()
    return path


# --- the debug log -----------------------------------------------------------


def _line(at: datetime, channel: str, body: dict[str, object]) -> str:
    stamp = at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return f"[{stamp}] {channel} {json.dumps(body)}\n"


def turn_start(at: datetime, conversation: str, request: str, surface: str = "headless") -> str:
    return _line(
        at,
        "structured-log.info",
        {
            "key": "agent_cli",
            "message": "agent_cli.turn.start",
            "metadata": {
                "turn_type": "new",
                "request_id": request,
                "conversation_id": conversation,
                "surface": surface,
            },
        },
    )


def usage(
    at: datetime,
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
    max_tokens: int = 0,
    subagent_call: str | None = None,
) -> str:
    metadata: dict[str, object] = {
        "action": "userMessageAction",
        "modelName": model,
        "usedTokens": input_tokens + 42,
        "maxTokens": max_tokens,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "cacheReadTokens": cache_read,
        "cacheWriteTokens": cache_write,
    }
    if subagent_call is not None:
        metadata = {"toolName": "TASK", "toolId": subagent_call, **metadata}
    return _line(
        at,
        "logger",
        {"message": "Setting token details for client token ring", "metadata": metadata},
    )


def turn_error(at: datetime, conversation: str, request: str, text: str | None) -> str:
    metadata: dict[str, object] = {
        "request_id": request,
        "conversation_id": conversation,
        "surface": "headless",
        "outcome": "error",
        "error_type": "non_retriable",
        "error_code": "unknown",
    }
    if text is not None:
        metadata["error_text"] = text
    return _line(
        at,
        "structured-log.info",
        {"key": "agent_cli", "message": "agent_cli.turn.outcome", "metadata": metadata},
    )


def turn_outcome(
    at: datetime,
    conversation: str,
    request: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    model_intent: str | None = None,
) -> str:
    """A turn that ended, with the turn's token totals as print mode writes
    them: strings, and input already net of the cache."""
    metadata: dict[str, object] = {
        "turn_type": "new",
        "request_id": request,
        "conversation_id": conversation,
        "surface": "headless",
        "outcome": "success",
        "turn_number": "1",
        "input_tokens": str(input_tokens),
        "output_tokens": str(output_tokens),
        "cache_read_tokens": str(cache_read),
        "cache_write_tokens": "0",
    }
    if model_intent is not None:
        metadata["model_intent"] = model_intent
    return _line(
        at,
        "structured-log.info",
        {"key": "agent_cli", "message": "agent_cli.turn.outcome", "metadata": metadata},
    )


def mcp_failed(at: datetime, identifier: str, error: str, transport: str | None = "sse") -> str:
    lines = ""
    if transport is not None:
        lines += _line(
            at,
            "logger",
            {
                "message": "MCP HTTP exchange failed",
                "metadata": {
                    "event": "mcp_http_exchange",
                    "outcome": "error",
                    "cursorMcp": {
                        "transport": {"identifier": identifier, "transportType": transport}
                    },
                },
            },
        )
    return lines + _line(
        at,
        "logger",
        {
            "message": "Failed to load MCP server",
            "metadata": {"identifier": identifier, "source": "user", "errorMessage": error},
        },
    )


def write_log(log_root: Path, lines: list[str]) -> Path:
    log_root.mkdir(parents=True, exist_ok=True)
    path = log_root / "session-2026-10-21T00-00-00-000Z-100-1.log"
    header = (
        f"--- Cursor Agent Debug Session {T0.isoformat()} ---\n"
        + json.dumps({"event": "debug-session-start", "pid": 100})
        + "\n"
    )
    path.write_text(header + "".join(lines), encoding="utf-8")
    return path


# --- the desktop app's structured log ----------------------------------------


def _ide_line(
    at: datetime, message: str, metadata: dict[str, object], key: str = "composer"
) -> str:
    """One line as the desktop app writes it: the machine's local time with no
    zone, a level, and the JSON body, with every value a string."""
    stamp = at.astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    body = {
        "level": "info",
        "key": key,
        "message": message,
        "metadata": {"client_version": "3.27.12", "layout": "unifiedAgent", **metadata},
    }
    return f"{stamp} [info] {json.dumps(body)}\n"


def ide_turn_start(at: datetime, conversation: str, request: str) -> str:
    return _ide_line(
        at,
        "agent.turn.start",
        {"turn_type": "new", "request_id": request, "conversation_id": conversation},
    )


def ide_turn_outcome(
    at: datetime, conversation: str, request: str, error_text: str | None = None
) -> str:
    metadata: dict[str, object] = {
        "request_id": request,
        "conversation_id": conversation,
        "outcome": "success" if error_text is None else "error",
    }
    if error_text is not None:
        metadata.update(error_type="non_retriable", error_code="unknown", error_text=error_text)
    return _ide_line(at, "agent.turn.outcome", metadata)


def ide_usage(
    at: datetime,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
) -> str:
    return _ide_line(
        at,
        "Setting token details for client token ring",
        {
            "action": "userMessageAction",
            "modelName": "default",
            "usedTokens": str(input_tokens + output_tokens),
            "maxTokens": "0",
            "inputTokens": str(input_tokens),
            "outputTokens": str(output_tokens),
            "cacheReadTokens": str(cache_read),
            "cacheWriteTokens": "0",
        },
        key="agent_exec",
    )


def ide_delegate(at: datetime, call: str) -> str:
    return _ide_line(
        at,
        "subagent proxy: delegating to renderer",
        {"toolName": "TASK", "toolId": call},
        key="agent_exec",
    )


def ide_settled(at: datetime, call: str) -> str:
    return _ide_line(
        at,
        "subagent proxy: renderer settled",
        {"toolName": "TASK", "toolId": call},
        key="agent_exec",
    )


def write_ide_log(user_data: Path, lines: list[str]) -> Path:
    directory = (
        user_data
        / "logs"
        / "20261021T000000"
        / "window1"
        / "exthost"
        / "anysphere.cursor-always-local"
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "Cursor Structured Logs.log"
    path.write_text("".join(lines), encoding="utf-8")
    return path


# --- the transcript ----------------------------------------------------------


def _project(data_root: Path, cwd: str) -> Path:
    slug = "".join(c if c.isalnum() else "-" for c in cwd)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return data_root / "projects" / slug.strip("-")


def write_server_metadata(data_root: Path, cwd: str, identifier: str, name: str) -> Path:
    """The desktop app's record of one MCP server it loaded for a workspace, as
    3.27.12 wrote it: `projects/<slug>/mcps/<identifier>/SERVER_METADATA.json`."""
    directory = _project(data_root, cwd) / "mcps" / identifier
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "SERVER_METADATA.json"
    document = {"serverIdentifier": identifier, "serverName": name}
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return path


def write_transcript(
    data_root: Path, cwd: str, agent_id: str, rows: list[dict[str, object]]
) -> Path:
    directory = _project(data_root, cwd) / "agent-transcripts" / agent_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{agent_id}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path
