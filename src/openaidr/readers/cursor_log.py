"""Reads the Cursor CLI's debug log: usage, entrypoints, client errors, MCP failures.

Cursor writes no token usage to its chat store. Its debug log, on by default,
writes one line for each model response with the provider's counts, and names
the conversation and request each turn belongs to (ADR-0038). One file per
process, under `<os tmp>/cursor-agent-logs-<uid>/`, append-only; Cursor keeps
the 50 newest and none older than 7 days.

A line is `[<ISO time>] <channel> <JSON>`. Only these are read:

- `agent_cli.turn.start`: `conversation_id`, `request_id`, `surface`. Every
  line after it belongs to that turn until the next one.
- `Setting token details for client token ring`: one model response. With
  `toolName: "TASK"` and a `toolId`, the response is a sub-agent's: the child
  that call started.
- `agent_cli.turn.outcome` with `outcome: "error"`: a client error's time.
- `Failed to load MCP server`, and the `MCP HTTP exchange failed` lines before
  it: a connection that failed.

Positions are counted only from the start of a file, which only grows, so a
response id does not move as the file grows.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from openaidr.model import MCPConnection

LOG_PREFIX = "session-"
LOG_SUFFIX = ".log"

_LINE = re.compile(r"^\[(?P<at>[^\]]+)\] (?P<channel>\S+) (?P<body>\{.*\})\s*$")
_USAGE = "Setting token details for client token ring"
_TURN_START = "agent_cli.turn.start"
_TURN_OUTCOME = "agent_cli.turn.outcome"
_MCP_FAILED = "Failed to load MCP server"
_MCP_EXCHANGE = "MCP HTTP exchange failed"

#: How a failed connection's own words map to the category a consumer acts on,
#: first match wins. The words are Node's and the MCP SDK's, as Cursor passes
#: them through.
_FAILURES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:401|403)\b|unauthori[sz]ed|forbidden|needs? auth", re.IGNORECASE), "auth"),
    (re.compile(r"timed? ?out|ETIMEDOUT", re.IGNORECASE), "timeout"),
    (re.compile(r"\bspawn\b|ENOENT|EACCES", re.IGNORECASE), "spawn"),
    (
        re.compile(
            r"ECONNREFUSED|ECONNRESET|ENOTFOUND|EAI_AGAIN|EHOSTUNREACH|fetch failed", re.IGNORECASE
        ),
        "network",
    ),
    (re.compile(r"\bHTTP \d{3}\b|status code \d{3}", re.IGNORECASE), "http_status"),
)


def default_log_root() -> Path:
    """Where the CLI writes its debug logs: Node's `os.tmpdir()` and the user
    id, exactly as Cursor names it (`./src/debug.ts`). `os.tmpdir()` reads
    `TMPDIR`, `TMP` and `TEMP` on every call, so this does too, rather than
    `tempfile`'s value cached at first use."""
    named = next((os.environ[v] for v in ("TMPDIR", "TMP", "TEMP") if os.environ.get(v)), None)
    base = Path(named.rstrip("/") or "/") if named else Path(tempfile.gettempdir())
    uid = os.getuid() if hasattr(os, "getuid") else None
    return base / f"cursor-agent-logs-{uid if uid is not None else 'default'}"


@dataclass(frozen=True)
class Usage:
    """One model response, as one usage line stated it."""

    request_id: str | None
    #: 1-based, counting this owner's usage lines within the request.
    ordinal: int
    model: str | None
    occurred_at: datetime | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    max_tokens: int | None


@dataclass(frozen=True)
class Outcome:
    """A turn that ended in an error, as the log stated it."""

    request_id: str | None
    occurred_at: datetime | None
    text: str | None


@dataclass
class _File:
    """One log's committed prefix and what it has placed so far (ADR-0011's
    discipline, for a log)."""

    identity: tuple[int, int] | None = None
    offset: int = 0
    conversation: str | None = None
    request: str | None = None
    #: Conversations in the order their first turn starts in this file.
    conversations: list[str] = field(default_factory=list)
    surfaces: dict[str, str] = field(default_factory=dict)
    requests: dict[str, list[str]] = field(default_factory=dict)
    usage: dict[str, list[Usage]] = field(default_factory=dict)
    subagent_usage: dict[str, list[Usage]] = field(default_factory=dict)
    errors: dict[str, list[Outcome]] = field(default_factory=dict)
    failures: dict[str, MCPConnection] = field(default_factory=dict)
    transports: dict[str, str] = field(default_factory=dict)
    counts: dict[tuple[str, str | None], int] = field(default_factory=dict)
    first_at: datetime | None = None

    def read(self, path: Path) -> None:
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            identity = (info.st_dev, info.st_ino)
            if self.identity != identity or info.st_size < self.offset:
                fresh = _File(identity=identity)
                self.__dict__.update(fresh.__dict__)
            stream.seek(self.offset)
            for raw in stream:
                if not raw.endswith(b"\n"):
                    break
                self.offset += len(raw)
                self._line(raw.decode("utf-8", errors="replace"))

    def _line(self, text: str) -> None:
        match = _LINE.match(text)
        if match is None:
            return
        try:
            body = json.loads(match.group("body"))
        except (ValueError, RecursionError):
            return
        if not isinstance(body, dict):
            return
        message = body.get("message")
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            return
        at = _instant(match.group("at"))
        if self.first_at is None:
            self.first_at = at
        if message == _TURN_START:
            self._turn_start(metadata)
        elif message == _USAGE:
            self._usage(metadata, at)
        elif message == _TURN_OUTCOME:
            self._outcome(metadata, at)
        elif message == _MCP_EXCHANGE:
            self._exchange(metadata)
        elif message == _MCP_FAILED:
            self._mcp_failed(metadata)

    def _turn_start(self, metadata: dict[str, object]) -> None:
        conversation = _string(metadata.get("conversation_id"))
        if conversation is None:
            return
        self.conversation = conversation
        self.request = _string(metadata.get("request_id"))
        if conversation not in self.conversations:
            self.conversations.append(conversation)
        surface = _string(metadata.get("surface"))
        if surface is not None:
            self.surfaces.setdefault(conversation, surface)
        if self.request is not None:
            self.requests.setdefault(conversation, []).append(self.request)

    def _usage(self, metadata: dict[str, object], at: datetime | None) -> None:
        tool_id = _string(metadata.get("toolId"))
        # A sub-agent's lines name the call that started it; Cursor tags them
        # `TASK` (`Using client-side subagent execution`).
        child = tool_id if metadata.get("toolName") == "TASK" else None
        owner = child if child is not None else self.conversation
        if owner is None:
            return
        key = (owner, self.request)
        self.counts[key] = self.counts.get(key, 0) + 1
        maximum = _count(metadata.get("maxTokens"))
        usage = Usage(
            request_id=self.request,
            ordinal=self.counts[key],
            model=_string(metadata.get("modelName")),
            occurred_at=at,
            input_tokens=_count(metadata.get("inputTokens")),
            output_tokens=_count(metadata.get("outputTokens")),
            cache_read_tokens=_count(metadata.get("cacheReadTokens")),
            cache_write_tokens=_count(metadata.get("cacheWriteTokens")),
            max_tokens=maximum if maximum else None,
        )
        target = self.subagent_usage if child is not None else self.usage
        target.setdefault(owner, []).append(usage)

    def _outcome(self, metadata: dict[str, object], at: datetime | None) -> None:
        conversation = _string(metadata.get("conversation_id"))
        if conversation is None or metadata.get("outcome") != "error":
            return
        self.errors.setdefault(conversation, []).append(
            Outcome(
                request_id=_string(metadata.get("request_id")),
                occurred_at=at,
                text=_string(metadata.get("error_text")),
            )
        )

    def _exchange(self, metadata: dict[str, object]) -> None:
        mcp = metadata.get("cursorMcp")
        transport = mcp.get("transport") if isinstance(mcp, dict) else None
        if not isinstance(transport, dict):
            return
        identifier = _string(transport.get("identifier"))
        kind = _string(transport.get("transportType"))
        if identifier is not None and kind is not None:
            # The last transport tried is the one the failure is about: Cursor
            # falls back from streamable HTTP to SSE before it gives up.
            self.transports[identifier] = kind

    def _mcp_failed(self, metadata: dict[str, object]) -> None:
        identifier = _string(metadata.get("identifier"))
        if identifier is None:
            return
        detail = _string(metadata.get("errorMessage"))
        self.failures[identifier] = MCPConnection(
            server=identifier,
            transport=self.transports.get(identifier),
            connected=False,
            failure_category=_category(detail),
            failure_detail=detail,
        )


@dataclass(frozen=True)
class Placement:
    """What the logs hold for one session."""

    usage: tuple[Usage, ...] = ()
    #: Request ids in the order the session's turns started.
    requests: tuple[str, ...] = ()
    surface: str | None = None
    errors: tuple[Outcome, ...] = ()
    connections: tuple[MCPConnection, ...] = ()
    #: An `MCPLogState`: whether a log was placed for this session, and why not.
    state: str = "not_attempted"
    last_at: datetime | None = None


class CursorLogs:
    """Every debug log under one root, each read from where it was last read."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._files: dict[Path, _File] = {}

    def refresh(self) -> tuple[str | None, list[str]]:
        """Read what every log appended. Returns the root's state, `None` when
        it was read, and a failure message for each log that could not be."""
        failures: list[str] = []
        try:
            info = self.root.stat()
        except FileNotFoundError:
            self._files.clear()
            return "no_log_root", failures
        except OSError as error:
            return "log_root_unreadable", [f"{self.root}: {error}"]
        if not stat.S_ISDIR(info.st_mode):
            return "log_root_unreadable", [f"{self.root}: not a directory"]
        try:
            names = sorted(os.listdir(self.root))
        except OSError as error:
            return "log_root_unreadable", [f"{self.root}: {error}"]
        present: set[Path] = set()
        for name in names:
            if not (name.startswith(LOG_PREFIX) and name.endswith(LOG_SUFFIX)):
                continue
            path = self.root / name
            present.add(path)
            state = self._files.setdefault(path, _File())
            try:
                state.read(path)
            except FileNotFoundError:
                present.discard(path)
            except OSError as error:
                failures.append(f"{path}: {error}; its usage and MCP failures are not read")
        for gone in set(self._files) - present:
            del self._files[gone]
        return None, failures

    def names(self, path: Path) -> tuple[set[str], set[str]]:
        """The conversations and sub-agent calls one log names, for a watcher
        event on that log."""
        state = self._files.get(path)
        if state is None:
            return set(), set()
        return set(state.conversations) | set(state.errors), set(state.subagent_usage)

    def place(
        self, conversation: str, subagent_call: str | None, root_state: str | None
    ) -> Placement:
        """The usage, entrypoint, errors and MCP failures for one session: a
        main conversation, or the sub-agent `subagent_call` started."""
        if root_state is not None:
            return Placement(state=root_state)
        usage: list[Usage] = []
        requests: list[str] = []
        errors: list[Outcome] = []
        connections: dict[str, MCPConnection] = {}
        surface: str | None = None
        placed = False
        ordered = sorted(
            self._files.values(),
            key=lambda f: f.first_at or datetime.min.replace(tzinfo=UTC),
        )
        for state in ordered:
            if subagent_call is not None:
                lines = state.subagent_usage.get(subagent_call)
                if lines:
                    placed = True
                    usage.extend(lines)
                    # A sub-agent's lines carry its parent's request.
                    for line in lines:
                        if line.request_id is not None and line.request_id not in requests:
                            requests.append(line.request_id)
                continue
            if conversation not in state.conversations and conversation not in state.errors:
                continue
            placed = True
            usage.extend(state.usage.get(conversation, ()))
            requests.extend(state.requests.get(conversation, ()))
            errors.extend(state.errors.get(conversation, ()))
            if surface is None:
                surface = state.surfaces.get(conversation)
            connections.update(state.failures)
        if not placed:
            return Placement(state="no_log_for_session")
        times = [u.occurred_at for u in usage if u.occurred_at] + [
            e.occurred_at for e in errors if e.occurred_at
        ]
        return Placement(
            usage=tuple(usage),
            requests=tuple(requests),
            surface=surface,
            errors=tuple(errors),
            connections=tuple(connections[name] for name in sorted(connections)),
            state="applied",
            last_at=max(times) if times else None,
        )


def _category(detail: str | None) -> str | None:
    if detail is None:
        return "unknown"
    for pattern, category in _FAILURES:
        if pattern.search(detail):
            return category
    return "unknown"


def _instant(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.astimezone(UTC) if moment.tzinfo else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
