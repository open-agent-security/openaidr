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
    started: datetime = T0
    name: str = "New Agent"
    title: str | None = None
    subagent: dict[str, str] | None = None
    last_used_model: str | None = None
    messages: list[dict[str, object]] = field(default_factory=list)
    turns: list[tuple[bytes, list[bytes]]] = field(default_factory=list)
    _blobs: dict[str, bytes] = field(default_factory=dict)
    _clock: datetime = T0
    _counter: int = 0

    def __post_init__(self) -> None:
        self._clock = self.started
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
        text: str | None = None,
        case: int = 1,
        call_id: str | None = None,
        duration: int = 50,
    ) -> str:
        """An assistant message with one tool call, and its result when
        `output` (Cursor's typed outcome) is given."""
        call_id = call_id or self.next_id("call_")
        message_id = self.next_id("msg-")
        content: list[dict[str, object]] = []
        if text:
            content.append({"type": "text", "text": text})
        content.append({"type": "tool-call", "toolCallId": call_id, "toolName": tool, "args": args})
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

    def write(self, chats: Path, *, meta_json: bool = True) -> Path:
        """Write the store under `chats/<hash>/<agent_id>/`; returns its path."""
        directory = chats / hashlib.md5(self.cwd.encode()).hexdigest() / self.agent_id
        directory.mkdir(parents=True, exist_ok=True)
        message_ids = [self.blob(json.dumps(m).encode("utf-8")) for m in self.messages]
        turn_ids = []
        for user_id, steps in self.turns:
            agent_turn = bytes_field(1, user_id) + b"".join(bytes_field(2, s) for s in steps)
            turn_ids.append(self.blob(bytes_field(1, agent_turn)))
        root = (
            b"".join(bytes_field(1, i) for i in message_ids)
            + b"".join(bytes_field(8, i) for i in turn_ids)
            + bytes_field(9, f"file://{self.cwd}")
            + varint_field(26, ms(self.started))
        )
        root_id = self.blob(root)
        meta: dict[str, object] = {
            "agentId": self.agent_id,
            "latestRootBlobId": root_id.hex(),
            "name": self.name,
            "mode": "default",
            "createdAt": ms(self.started),
        }
        if self.subagent is not None:
            meta["subagentInfo"] = self.subagent
        if self.last_used_model is not None:
            meta["lastUsedModel"] = self.last_used_model
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
        if meta_json and self.subagent is None:
            sidecar: dict[str, object] = {
                "schemaVersion": 1,
                "createdAtMs": ms(self.started),
                "hasConversation": True,
                "updatedAtMs": ms(self._clock),
                "cwd": self.cwd,
            }
            if self.title is not None:
                sidecar["title"] = self.title
            (directory / "meta.json").write_text(json.dumps(sidecar), encoding="utf-8")
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


def turn_error(at: datetime, conversation: str, request: str, text: str) -> str:
    return _line(
        at,
        "structured-log.info",
        {
            "key": "agent_cli",
            "message": "agent_cli.turn.outcome",
            "metadata": {
                "request_id": request,
                "conversation_id": conversation,
                "surface": "headless",
                "outcome": "error",
                "error_type": "non_retriable",
                "error_code": "unknown",
                "error_text": text,
            },
        },
    )


def turn_outcome(
    at: datetime,
    conversation: str,
    request: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
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
        "cache_write_tokens": str(cache_write),
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


def write_log(
    log_root: Path, lines: list[str], *, name: str = "session-2026-10-21T00-00-00-000Z-100-1.log"
) -> Path:
    log_root.mkdir(parents=True, exist_ok=True)
    path = log_root / name
    header = (
        f"--- Cursor Agent Debug Session {T0.isoformat()} ---\n"
        + json.dumps({"event": "debug-session-start", "pid": 100})
        + "\n"
    )
    path.write_text(header + "".join(lines), encoding="utf-8")
    return path


# --- the transcript ----------------------------------------------------------


def write_transcript(
    data_root: Path, cwd: str, agent_id: str, rows: list[dict[str, object]]
) -> Path:
    slug = "".join(c if c.isalnum() else "-" for c in cwd)
    while "--" in slug:
        slug = slug.replace("--", "-")
    directory = data_root / "projects" / slug.strip("-") / "agent-transcripts" / agent_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{agent_id}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path
