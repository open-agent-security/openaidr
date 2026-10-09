"""Read Codex rollout JSONL without the dependency's lossy Codex projection.

A response record is a turn; output records resolve earlier calls, never add
turns. Physical line numbers survive appended records and skipped event types.
See ADR-0024 for identity, outcome and coverage boundaries.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from openaidr.kinds import map_source
from openaidr.model import Compaction, ContextItem, Session, ToolCall, Turn, span_id
from openaidr.readers.base import ReaderFailure, Window
from openaidr.readers.common import directory_present, discover_files
from openaidr.readers.common import timestamp as _time
from openaidr.toolnames import split_tool_name

_CALLS = {"function_call", "custom_tool_call", "tool_search_call"}
_OUTPUTS = {"function_call_output", "custom_tool_call_output", "tool_search_output"}


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
    context: list[ContextItem] = field(default_factory=list)
    compactions: list[Compaction] = field(default_factory=list)
    permission_mode: str | None = None
    initial_prompt: str | None = None
    event_calls: dict[str, int] = field(default_factory=dict)

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
            instructions = payload.get("base_instructions")
            if isinstance(instructions, dict):
                self._context("base_instructions", _text(instructions.get("text")), line)
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
            approval = payload.get("approval_policy")
            sandbox = payload.get("sandbox_policy")
            modes = [approval] if isinstance(approval, str) else []
            if isinstance(sandbox, dict) and isinstance(sandbox.get("type"), str):
                modes.append(sandbox["type"])
            self.permission_mode = "; ".join(modes) or None
        elif kind == "compacted":
            self._context("compaction", _text(payload.get("message")), line)
            self.compactions.append(Compaction("unknown", None, None))
        elif kind == "event_msg":
            self._event(payload, line, timestamp)
        elif kind == "response_item":
            self._response(payload, line, timestamp)
        # Compaction replacement_history is context, never fresh executions.

    def _response(self, payload: dict[str, object], line: int, timestamp: datetime | None) -> None:
        item = payload.get("type")
        if not isinstance(item, str):
            raise TypeError("response item has no type")
        if item == "web_search_call":
            self._event(
                {
                    "type": "item_completed",
                    "item": {
                        **payload,
                        "type": "WebSearch",
                        "arguments": payload.get("action", {}),
                    },
                },
                line,
                timestamp,
            )
            return
        if item in _OUTPUTS:
            cid = _text(payload.get("call_id"))
            if item == "tool_search_output":
                if cid is None:
                    return
                payload = {**payload, "output": payload.get("tools", [])}
            if not cid or "output" not in payload:
                raise ValueError("tool output requires call_id and output")
            key = (
                "tool_search_call"
                if item == "tool_search_output"
                else item.removesuffix("_output"),
                cid,
            )
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
                status = "unknown" if call.status == "pending" else call.status
                if item == "tool_search_output":
                    status = {"completed": "ok", "failed": "error"}.get(
                        str(payload.get("status")), "unknown"
                    )
                self.turns[position] = replace(
                    turn,
                    tool_calls=(
                        replace(
                            call,
                            result=result,
                            result_size=len(result),
                            status=status,
                            duration_ms=duration,
                        ),
                    ),
                )
            else:
                self._withhold(position)
                self.calls[key] = None
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
            if role in {"developer", "system"}:
                self._context(role, _message_text(payload.get("content")), line)
                return
            if role not in {"user", "assistant"}:
                return
            text = _message_text(payload.get("content"))
        else:
            name = "tool_search" if item == "tool_search_call" else _text(payload.get("name"))
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
                permission_mode=self.permission_mode,
            )
        )

    def _context(self, source: str, text: str | None, line: int) -> None:
        if text:
            assert self.meta is not None
            self.context.append(
                ContextItem(
                    span_id(f"codex:{self.meta['id']}", f"line_{line}", 0), source, None, text
                )
            )

    def _event(self, payload: dict[str, object], line: int, timestamp: datetime | None) -> None:
        kind = payload.get("type")
        if kind == "user_message":
            if self.initial_prompt is None:
                self.initial_prompt = _text(payload.get("message"))
            return
        if kind == "item_completed":
            assert self.meta is not None
            if payload.get("thread_id", self.meta["id"]) != self.meta["id"]:
                return
            item = payload.get("item")
            if not isinstance(item, dict):
                return
            if item.get("type") == "UserMessage" and self.initial_prompt is None:
                self.initial_prompt = _message_text(item.get("content"))
                return
        elif kind == "patch_apply_end":
            item = {**payload, "type": "FileChange", "id": payload.get("call_id")}
        elif kind == "mcp_tool_call_end":
            result = payload.get("result")
            invocation = payload.get("invocation")
            if not isinstance(result, dict) or not isinstance(invocation, dict):
                return
            item = {
                **invocation,
                "type": "McpToolCall",
                "id": payload.get("call_id"),
                "status": "failed" if "Err" in result else "completed",
                "result": result.get("Ok"),
                "error": result.get("Err"),
            }
        else:
            return
        names = {
            "CommandExecution": "CommandExecution",
            "FileChange": "FileChange",
            "McpToolCall": "McpToolCall",
            "WebSearch": "web_search",
        }
        name = names.get(str(item.get("type")))
        cid = _text(item.get("id"))
        if name is None:
            return
        matches = (
            [self.calls[(family, cid)] for family in _CALLS if (family, cid) in self.calls]
            if cid
            else []
        )
        if len(matches) > 1 or (matches and matches[0] is None):
            return
        position = matches[0] if matches else (self.event_calls.get(cid) if cid else None)
        if position is None:
            arguments = item.get("arguments", {})
            if item.get("type") == "CommandExecution":
                arguments = {"command": item.get("command", [])}
            elif item.get("type") == "FileChange":
                arguments = {"changes": item.get("changes", {})}
            self._response(
                {"type": "function_call", "name": name, "arguments": arguments}, line, timestamp
            )
            position = len(self.turns) - 1
            if cid:
                self.event_calls[cid] = position
        turn = self.turns[position]
        call = turn.tool_calls[0]
        status = {"completed": "ok", "failed": "error", "declined": "rejected"}.get(
            str(item.get("status")), "unknown"
        )
        result = item.get("result")
        exit_code = item.get("exit_code")
        if (isinstance(result, dict) and result.get("isError") is True) or (
            isinstance(exit_code, int) and exit_code != 0
        ):
            status = "error"
        error = item.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        body = call.result
        if body is None:
            raw = (
                result if result is not None else item.get("aggregated_output", item.get("stdout"))
            )
            body = _body(raw) if raw is not None else None
        self.turns[position] = replace(
            turn,
            tool_calls=(
                replace(
                    call,
                    provider_call_id=cid,
                    status=status,
                    result=body,
                    result_size=len(body) if body is not None else None,
                    error_text=(_text(error) or _text(item.get("stderr")))
                    if status == "error"
                    else None,
                    denial_kind="declined" if status == "rejected" else None,
                    working_directory=_text(item.get("cwd")) or call.working_directory,
                    attributed_plugin=_text(item.get("plugin_id")) or _text(item.get("pluginId")),
                    mcp_server=_text(item.get("server")) or call.mcp_server,
                    tool_name=_text(item.get("tool")) or call.tool_name,
                ),
            ),
        )

    def _withhold(self, position: int) -> None:
        turn = self.turns[position]
        call = replace(
            turn.tool_calls[0],
            status="pending",
            result=None,
            result_size=None,
            duration_ms=None,
            error_text=None,
            denial_kind=None,
            attributed_plugin=None,
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
            initial_prompt=self.initial_prompt,
            context_items=tuple(self.context),
            compactions=tuple(self.compactions),
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
