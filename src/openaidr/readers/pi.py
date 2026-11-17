"""Pi (`pi.dev`, `@mariozechner/pi-coding-agent`) sessions, read from its JSONL files.

Pi writes one file per session under `<agent dir>/sessions/--<cwd>--/`, where the
agent dir is `PI_CODING_AGENT_DIR`, else `~/.pi/agent`. The first line is a
`session` header; every later line is an entry with an 8-hex `id` and a
`parentId`, forming a tree. See `docs/specs/pi.md`.

Every `message` entry in the file is read, on every branch: a tool call on a
branch the person later left still ran (ADR-0043). An entry's own `id` is its
turn key, so a span survives appends and branching (ADR-0001).
"""

from __future__ import annotations

import getpass
import json
import os
import socket
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from openaidr.kinds import map_source
from openaidr.model import (
    Compaction,
    ContextItem,
    ModelResponse,
    Session,
    Status,
    TokenUsage,
    ToolCall,
    Turn,
    span_id,
)
from openaidr.readers.base import ReaderFailure, Window
from openaidr.toolclass import canonical_arguments, tool_class

SOURCE = "pi"
SESSIONS = "sessions"

#: The `customType` of the entry an extension writes with `pi.appendEntry` when
#: it blocks a call: `{"toolCallId": "<the call's id>"}`. Pi writes a blocked
#: call as an ordinary error result, so this entry is the only record that the
#: call never ran (ADR-0044).
BLOCKED = "openaidr.tool-call-blocked"

#: The session versions this reader has seen Pi write. Version 3 is current
#: in 0.73; 1 and 2 are migrated by Pi itself on load, never rewritten on disk.
_VERSIONS = frozenset({3})


def default_root() -> Path:
    """Pi's agent directory: `PI_CODING_AGENT_DIR`, else `~/.pi/agent`."""
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".pi" / "agent"


@dataclass
class _Call:
    key: str
    index: int
    call_id: str | None
    name: str
    arguments: dict[str, object]
    issued_ms: int | None


@dataclass
class _Result:
    text: str
    is_error: bool
    at_ms: int | None


@dataclass
class _Projection:
    header: dict[str, object] | None = None
    turns: list[tuple[str, str, str, datetime | None, str | None, list[_Call]]] = field(
        default_factory=list
    )
    #: Results by (the assistant entry that issued the call, provider call id).
    #: A provider call id is unique within one response, not within a session,
    #: and Pi's tree can hold two branches that reuse one (ADR-0004).
    results: dict[tuple[str, str], list[_Result]] = field(default_factory=dict)
    #: Per assistant entry and each later entry before the next message, the
    #: assistant entry it descends from.
    anchors: dict[str, str] = field(default_factory=dict)
    #: Calls an extension recorded blocking, as (assistant entry, call id).
    blocked: set[tuple[str, str]] = field(default_factory=set)
    responses: dict[str, ModelResponse] = field(default_factory=dict)
    context: list[ContextItem] = field(default_factory=list)
    compactions: list[Compaction] = field(default_factory=list)
    initial_prompt: str | None = None
    title: str | None = None
    model: str | None = None
    last_activity_at: datetime | None = None
    line: int = 0

    @property
    def header_id(self) -> str:
        return str(self.header["id"]) if self.header is not None else ""

    def project(self, entry: dict[str, object], line: int) -> None:
        self.line = line
        kind = entry.get("type")
        at = _instant(entry.get("timestamp"))
        if at is not None and (self.last_activity_at is None or at > self.last_activity_at):
            self.last_activity_at = at
        if kind == "session":
            if self.header is None:
                self.header = entry
            return
        key = _string(entry.get("id"))
        if key is None:
            return
        if kind == "message":
            self._message(key, _string(entry.get("parentId")), _mapping(entry.get("message")), at)
        elif kind == "compaction":
            self.compactions.append(
                Compaction(
                    # Pi records who wrote the summary (`fromHook`), not what
                    # caused it, so the cause is unknown.
                    trigger="unknown",
                    pre_tokens=_count(entry.get("tokensBefore")),
                    dropped_tokens=None,
                    occurred_at=at,
                )
            )
        elif kind == "custom_message":
            self.context.append(
                ContextItem(
                    span=span_id(f"{SOURCE}:{self.header_id}", "context", self.line),
                    source="injected",
                    name=_string(entry.get("customType")),
                    text=_text(entry.get("content")),
                    occurred_at=at,
                )
            )
        elif kind == "session_info":
            self.title = _string(entry.get("name")) or self.title
        if kind != "message":
            # An extension's entry can sit between a call and its result, so it
            # passes the assistant entry on to the entries after it.
            parent = _string(entry.get("parentId"))
            anchor = self.anchors.get(parent) if parent is not None else None
            if anchor is not None:
                self.anchors[key] = anchor
                if kind == "custom" and entry.get("customType") == BLOCKED:
                    call_id = _string(_mapping(entry.get("data")).get("toolCallId"))
                    if call_id is not None:
                        self.blocked.add((anchor, call_id))

    def _message(
        self, key: str, parent: str | None, message: Mapping[str, object], at: datetime | None
    ) -> None:
        role = message.get("role")
        if role == "user":
            text = _text(message.get("content"))
            if self.initial_prompt is None and text:
                self.initial_prompt = text
            self.turns.append((key, "user", text, at, None, []))
        elif role == "assistant":
            content = message.get("content")
            blocks = (
                [b for b in content if isinstance(b, Mapping)] if isinstance(content, list) else []
            )
            text = "".join(_string(b.get("text")) or "" for b in blocks if b.get("type") == "text")
            issued = _count(message.get("timestamp"))
            calls = [
                _Call(
                    key=key,
                    index=index,
                    call_id=_string(block.get("id")),
                    name=_string(block.get("name")) or "",
                    arguments=_mapping(block.get("arguments")),
                    issued_ms=issued,
                )
                for index, block in enumerate(b for b in blocks if b.get("type") == "toolCall")
            ]
            response_id = _string(message.get("responseId"))
            model = _string(message.get("model"))
            self.model = model or self.model
            if response_id is not None:
                self.responses[response_id] = ModelResponse(
                    response_id=response_id,
                    request_id=None,
                    model=model,
                    occurred_at=at,
                    final=True,
                    is_sidechain=False,
                    usage=_usage(_mapping(message.get("usage"))),
                )
            self.anchors[key] = key
            self.turns.append((key, "assistant", text, at, response_id, calls))
        elif role == "toolResult":
            call_id = _string(message.get("toolCallId"))
            anchor = self.anchors.get(parent) if parent is not None else None
            if anchor is not None:
                self.anchors[key] = anchor
            if call_id is not None and anchor is not None:
                self.results.setdefault((anchor, call_id), []).append(
                    _Result(
                        text=_text(message.get("content")),
                        is_error=message.get("isError") is True,
                        at_ms=_count(message.get("timestamp")),
                    )
                )

    def session(self) -> Session | None:
        if self.header is None:
            return None
        session_id = f"{SOURCE}:{self.header['id']}"
        cwd = _string(self.header.get("cwd"))
        turns = tuple(
            Turn(
                position=position,
                key=key,
                role=role,
                text=text,
                is_sidechain=False,
                tool_calls=tuple(self._tool_call(session_id, call, cwd) for call in calls),
                occurred_at=at,
                response_id=response_id,
            )
            for position, (key, role, text, at, response_id, calls) in enumerate(self.turns)
        )
        return Session(
            session_id=session_id,
            agent_kind=map_source(SOURCE),
            source=SOURCE,
            started_at=_instant(self.header.get("timestamp")),
            model=self.model,
            working_directory=cwd,
            machine=socket.gethostname() or None,
            user=_user(),
            turns=turns,
            initial_prompt=self.initial_prompt,
            context_items=tuple(self.context),
            compactions=tuple(self.compactions),
            last_activity_at=self.last_activity_at,
            title=self.title,
            responses=tuple(self.responses.values()),
            compactions_recorded=True,
        )

    def _tool_call(self, session_id: str, call: _Call, cwd: str | None) -> ToolCall:
        results = self.results.get((call.key, call.call_id), []) if call.call_id is not None else []
        # Two results for one call cannot be told apart, so neither is given.
        result = results[0] if len(results) == 1 else None
        status: Status
        denial: str | None = None
        if len(results) > 1:
            status = "unknown"
        elif (
            call.call_id is not None
            and (call.key, call.call_id) in self.blocked
            and (result is None or result.is_error)
        ):
            # A block entry and no successful result: the call never ran. A
            # result that says it succeeded outweighs the entry (ADR-0044).
            status, denial = "rejected", "extension-blocked"
        elif result is None:
            status = "pending"
        else:
            # Without a block entry, Pi's error result for a block cannot be
            # told from a tool's own failure.
            status = "error" if result.is_error else "ok"
        body = result.text if result is not None else None
        duration = (
            result.at_ms - call.issued_ms
            if result is not None and result.at_ms is not None and call.issued_ms is not None
            else None
        )
        return ToolCall(
            span=span_id(session_id, call.key, call.index),
            tool_name=call.name,
            mcp_server=None,
            status=status,
            arguments=call.arguments,
            result=body,
            result_size=len(body) if body is not None else None,
            error_text=body if status == "error" else None,
            truncated=False,
            provider_call_id=call.call_id,
            duration_ms=duration if duration is not None and duration >= 0 else None,
            denial_kind=denial,
            working_directory=cwd,
            tool_class=tool_class(SOURCE, call.name, None),
            canonical_arguments=canonical_arguments(SOURCE, call.name, None, call.arguments),
        )


class PiReader:
    """Reads Pi session files under `<agent dir>/sessions/`."""

    agent_kind = "pi"

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root if root is not None else default_root()).absolute()

    def locations(self) -> tuple[Path, ...]:
        return (self._root / SESSIONS,)

    def collect(self, window: Window) -> tuple[list[Session], list[ReaderFailure]]:
        paths, failures = self._discover()
        sessions: list[Session] = []
        seen: dict[str, Path] = {}
        for path in paths:
            if window.since is not None:
                try:
                    modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
                except (OSError, OverflowError, ValueError) as error:
                    failures.append(self._failure(f"{path}: {error}"))
                    continue
                if modified < window.since:
                    continue
            session = self._read(path, failures)
            if session is None:
                continue
            if window.since is not None and (
                session.last_activity_at is None or session.last_activity_at < window.since
            ):
                continue
            if session.session_id in seen:
                failures.append(
                    self._failure(
                        f"{session.session_id}: claimed by {seen[session.session_id]} and {path};"
                        " the second is not read"
                    )
                )
                continue
            seen[session.session_id] = path
            sessions.append(session)
        return sessions, failures

    def collect_file(self, path: Path) -> tuple[list[Session], list[ReaderFailure]]:
        """Read one session file whole. Pi files are small; an incremental cursor
        (ADR-0011) is left until one is measured to need it."""
        path = path.absolute()
        if path.suffix != ".jsonl" or not path.is_relative_to(self._root / SESSIONS):
            return [], []
        failures: list[ReaderFailure] = []
        session = self._read(path, failures)
        return ([session] if session is not None else []), failures

    def _read(self, path: Path, failures: list[ReaderFailure]) -> Session | None:
        try:
            if not stat.S_ISREG(path.stat().st_mode):
                failures.append(self._failure(f"{path}: not a regular file"))
                return None
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            failures.append(self._failure(f"{path}: {error}"))
            return None
        projection = _Projection()
        lines = raw.split(b"\n")
        # A last line with no newline is a record Pi is still writing.
        complete = lines[:-1]
        for number, line in enumerate(complete, start=1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    raise TypeError("entry is not an object")
            except (ValueError, TypeError, UnicodeError, RecursionError):
                # Never the record's content in a failure.
                failures.append(
                    self._failure(f"{path}: line {number}: malformed entry; session withheld")
                )
                return None
            # The header is the first record, not line 1: a blank line before it
            # must not let an unchecked one through.
            first = projection.header is None
            if first and (entry.get("type") != "session" or not _string(entry.get("id"))):
                failures.append(self._failure(f"{path}: no session header; not read"))
                return None
            if first and entry.get("version") not in _VERSIONS:
                failures.append(
                    self._failure(
                        f"{path}: session version {entry.get('version')!r} is not one this"
                        " reader knows; not read"
                    )
                )
                return None
            projection.project(entry, number)
        return projection.session()

    def _discover(self) -> tuple[list[Path], list[ReaderFailure]]:
        paths: list[Path] = []
        failures: list[ReaderFailure] = []
        location = self._root / SESSIONS
        errors: list[OSError] = []
        for dirpath, _dirnames, filenames in os.walk(location, onerror=errors.append):
            paths.extend(Path(dirpath) / name for name in filenames if name.endswith(".jsonl"))
        for error in errors:
            if isinstance(error, FileNotFoundError) and Path(str(error.filename)) == location:
                continue
            failures.append(self._failure(f"{error.filename}: {error.strerror or error}"))
        return sorted(paths), failures

    def _failure(self, message: str) -> ReaderFailure:
        return ReaderFailure(agent_kind=self.agent_kind, message=message)


def _usage(usage: Mapping[str, object]) -> TokenUsage:
    """Pi's `input`, `cacheRead`, `cacheWrite` and `output` are already disjoint:
    `pi-ai` subtracts both cache buckets from the prompt count before storing
    `input`, for every provider it speaks."""
    return TokenUsage(
        input_tokens=_count(usage.get("input")),
        cache_read_input_tokens=_count(usage.get("cacheRead")),
        cache_creation_input_tokens=_count(usage.get("cacheWrite")),
        output_tokens=_count(usage.get("output")),
    )


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            text
            for block in content
            if isinstance(block, Mapping)
            and block.get("type") == "text"
            and (text := _string(block.get("text"))) is not None
        )
    return ""


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _user() -> str | None:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return None
