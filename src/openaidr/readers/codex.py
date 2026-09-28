"""Read Codex rollout JSONL without the dependency's lossy Codex projection.

A response record is a turn; output records resolve earlier calls, never add
turns. Physical line numbers survive appended records and skipped event types.
See ADR-0014 for identity, outcome and coverage boundaries.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from openaidr.kinds import map_source
from openaidr.model import Session, ToolCall, Turn, span_id
from openaidr.readers.base import ReaderFailure, Window
from openaidr.readers.common import directory_present, discover_files
from openaidr.readers.common import timestamp as _time
from openaidr.toolnames import split_tool_name

_CALLS = {"function_call", "custom_tool_call"}
_OUTPUTS = {"function_call_output", "custom_tool_call_output"}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _body(value: object) -> str:
    # Keep structured content whole (including non-text blocks), never pass a
    # list through the model's string field or silently drop its non-text data.
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _message_text(value: object) -> str:
    if not isinstance(value, list):
        raise TypeError("message content is not an array")
    return "\n".join(
        part["text"]
        for part in value
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


@dataclass
class _Projection:
    meta: dict[str, object] | None = None
    turns: list[Turn] = field(default_factory=list)
    # None marks a reused id: all its calls stay pending, with no result join.
    calls: dict[tuple[str, str], int | None] = field(default_factory=dict)
    outputs: set[tuple[str, str]] = field(default_factory=set)
    started_at: datetime | None = None
    last_activity_at: datetime | None = None
    model: str | None = None
    cwd: str | None = None
    warnings: set[str] = field(default_factory=set)

    def project(self, record: dict[str, object], line: int) -> None:
        kind = record.get("type")
        payload = record.get("payload")
        if not isinstance(kind, str) or not isinstance(payload, dict):
            raise TypeError("record must contain a type and object payload")
        if self.meta is None:
            if kind != "session_meta" or not _text(payload.get("id")):
                raise ValueError("first record must contain session_meta.id")
            self.meta = payload
            self.cwd = _text(payload.get("cwd"))
            self.started_at = _time(payload.get("timestamp"))
            if payload.get("history_base") is not None:
                self.warnings.add("history_base prefix is not read; session history is incomplete")
            boundary = payload.get("subagent_history_start_ordinal")
            if boundary is not None and (type(boundary) is not int or boundary < 0):
                raise ValueError("invalid subagent history boundary")
        else:
            boundary = self.meta.get("subagent_history_start_ordinal")
            if isinstance(boundary, int):
                ordinal = record.get("ordinal")
                # Copied session_meta records have no ordinal. The child's
                # first metadata record above remains authoritative.
                if kind == "session_meta":
                    return
                if type(ordinal) is not int:
                    raise ValueError("subagent history boundary requires record ordinals")
                if ordinal < boundary:
                    return
            if kind == "session_meta":
                if payload.get("id") != self.meta.get("id"):
                    raise ValueError("conflicting session_meta identity")
                return
        timestamp = _time(record.get("timestamp"))
        if timestamp is not None:
            self.started_at = min(self.started_at, timestamp) if self.started_at else timestamp
            self.last_activity_at = (
                max(self.last_activity_at, timestamp) if self.last_activity_at else timestamp
            )
        if kind == "turn_context":
            self.model = _text(payload.get("model")) or self.model
            self.cwd = _text(payload.get("cwd")) or self.cwd
        elif kind == "response_item":
            self._response(payload, line, timestamp)
        # event_msg duplicates response items and does not add turns. Compacted
        # replacement_history is model context, not newly executed tool calls.

    def _response(self, payload: dict[str, object], line: int, timestamp: datetime | None) -> None:
        item = payload.get("type")
        if not isinstance(item, str):
            raise TypeError("response item has no type")
        if item in _OUTPUTS:
            cid = _text(payload.get("call_id"))
            if not cid or "output" not in payload:
                raise ValueError("tool output requires call_id and output")
            key = (item.removesuffix("_output"), cid)
            duplicate = key in self.outputs
            self.outputs.add(key)
            position = self.calls.get(key)
            if position is None:
                return
            if not duplicate:
                turn = self.turns[position]
                call = turn.tool_calls[0]
                duration = None
                if timestamp is not None and turn.occurred_at is not None:
                    elapsed = int((timestamp - turn.occurred_at).total_seconds() * 1000)
                    duration = elapsed if elapsed >= 0 else None
                result = _body(payload["output"])
                self.turns[position] = replace(
                    turn,
                    tool_calls=(
                        replace(
                            call,
                            result=result,
                            result_size=len(result),
                            status="unknown",
                            duration_ms=duration,
                        ),
                    ),
                )
            else:
                self._withhold(position)
            return
        if item not in _CALLS and item != "message":
            if item.endswith(("_call", "_output")):
                self.warnings.add(
                    f"unsupported response item {item!r}; tool coverage is incomplete"
                )
            return
        assert self.meta is not None
        key = f"line_{line}"
        session_id = f"codex:{self.meta['id']}"
        role = "assistant"
        text = ""
        tool_calls: tuple[ToolCall, ...] = ()
        if item == "message":
            role = _text(payload.get("role")) or "unknown"
            if role not in {"user", "assistant"}:
                return
            text = _message_text(payload.get("content"))
        else:
            name = _text(payload.get("name"))
            if not name:
                raise ValueError("tool call has no name")
            namespace = _text(payload.get("namespace"))
            raw_name = f"{namespace}.{name}" if namespace else name
            server, tool = split_tool_name("codex", raw_name)
            cid = _text(payload.get("call_id"))
            if item == "custom_tool_call":
                arguments = {"input": payload.get("input")}
            else:
                raw = payload.get("arguments", "{}")
                if isinstance(raw, str):
                    try:
                        decoded = json.loads(raw)
                    except ValueError:
                        decoded = raw
                else:
                    decoded = raw
                arguments = decoded if isinstance(decoded, dict) else {"raw": decoded}
            tool_calls = (
                ToolCall(
                    span=span_id(session_id, key, 0),
                    tool_name=tool,
                    mcp_server=server,
                    status="pending",
                    arguments=arguments,
                    result=None,
                    result_size=None,
                    error_text=None,
                    truncated=False,
                    provider_call_id=cid,
                    working_directory=self.cwd,
                ),
            )
            if cid:
                call_key = (item, cid)
                if call_key in self.calls:
                    previous = self.calls[call_key]
                    if previous is not None:
                        self._withhold(previous)
                    self.calls[call_key] = None
                else:
                    self.calls[call_key] = len(self.turns)
        source = self.meta.get("source")
        sidechain = (
            (isinstance(source, dict) and "subagent" in source)
            or bool(self.meta.get("parent_thread_id"))
            or self.meta.get("thread_source") == "subagent"
        )
        self.turns.append(
            Turn(
                position=len(self.turns),
                key=key,
                role=role,
                text=text,
                is_sidechain=sidechain,
                tool_calls=tool_calls,
                occurred_at=timestamp,
            )
        )

    def _withhold(self, position: int) -> None:
        turn = self.turns[position]
        if turn.tool_calls[0].status == "pending":
            return
        call = replace(
            turn.tool_calls[0], status="pending", result=None, result_size=None, duration_ms=None
        )
        self.turns[position] = replace(turn, tool_calls=(call,))

    def session(self) -> Session | None:
        if self.meta is None:
            return None
        source = self.meta.get("source")
        entrypoint = _text(source)
        if isinstance(source, dict) and "subagent" in source:
            entrypoint = "subagent"
        git = self.meta.get("git")
        return Session(
            session_id=f"codex:{self.meta['id']}",
            agent_kind=map_source("codex"),
            source="codex",
            started_at=self.started_at,
            last_activity_at=self.last_activity_at,
            model=self.model,
            working_directory=_text(self.meta.get("cwd")),
            machine=None,
            user=None,
            turns=tuple(self.turns),
            agent_version=_text(self.meta.get("cli_version")),
            entrypoint=entrypoint,
            git_branch=_text(git.get("branch")) if isinstance(git, dict) else None,
        )


@dataclass
class _Cursor:
    identity: tuple[int, int] | None = None
    offset: int = 0
    line: int = 0
    projection: _Projection = field(default_factory=_Projection)
    error: str | None = None

    def read(self, path: Path) -> None:
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            identity = (info.st_dev, info.st_ino)
            if self.identity != identity or info.st_size < self.offset:
                self.identity = identity
                self.offset = self.line = 0
                self.projection = _Projection()
                self.error = None
            if self.error:
                return
            stream.seek(self.offset)
            for raw in stream:
                # The committed offset stops before a torn record, including a
                # torn UTF-8 codepoint; next time it is read again as bytes.
                if not raw.endswith(b"\n"):
                    break
                self.offset += len(raw)
                self.line += 1
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                    if not isinstance(record, dict):
                        raise TypeError("record is not an object")
                    self.projection.project(record, self.line)
                except (ValueError, TypeError, UnicodeError):
                    # Never put transcript fragments in a failure: the CLI
                    # serializes these messages. A malformed record can hide a
                    # duplicate call id, so do not publish a partial projection.
                    self.error = f"line {self.line}: malformed rollout record; session withheld"
                    return


class CodexReader:
    """Cold collection and in-memory append cursors for Codex rollout files.

    `root` / OPENAIDR_CODEX_ROOT names the sessions directory itself. Otherwise
    use CODEX_HOME/sessions, with CODEX_HOME defaulting to ~/.codex.
    """

    agent_kind = "codex"

    def __init__(self, root: Path | None = None) -> None:
        home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        self._root = (
            root
            if root is not None
            else Path(os.environ.get("OPENAIDR_CODEX_ROOT", str(home / "sessions")))
        )
        self._cursors: dict[Path, _Cursor] = {}

    def collect(self, window: Window) -> tuple[list[Session], list[ReaderFailure]]:
        try:
            if not directory_present(self._root):
                return [], []
        except OSError as error:
            return [], [self._failure(self._root, str(error))]
        sessions: list[Session] = []
        paths, walk_errors = discover_files(
            self._root, prefix="rollout-", suffixes=(".jsonl", ".jsonl.zst")
        )
        failures = [self._failure(self._root, str(error)) for error in walk_errors]
        for path in paths:
            if path.name.endswith(".jsonl.zst"):
                failures.append(self._failure(path, "compressed rollout is not read"))
                continue
            try:
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    failures.append(self._failure(path, "not a regular file"))
                    continue
                if window.since and info.st_mtime < window.since.timestamp():
                    continue
            except OSError as error:
                failures.append(self._failure(path, str(error)))
                continue
            found, errors = self._read(path, _Cursor())
            sessions.extend(found)
            failures.extend(errors)
        return sessions, failures

    def collect_file(self, path: Path) -> tuple[list[Session], list[ReaderFailure]]:
        path = path.absolute()
        return self._read(path, self._cursors.setdefault(path, _Cursor()))

    def _read(self, path: Path, cursor: _Cursor) -> tuple[list[Session], list[ReaderFailure]]:
        try:
            cursor.read(path)
        except OSError as error:
            return [], [self._failure(path, str(error))]
        if cursor.error:
            return [], [self._failure(path, cursor.error)]
        session = cursor.projection.session()
        failures = [self._failure(path, warning) for warning in sorted(cursor.projection.warnings)]
        return [session] if session else [], failures

    def _failure(self, path: Path, message: str) -> ReaderFailure:
        return ReaderFailure(self.agent_kind, f"{path}: {message}")
