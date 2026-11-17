"""Reads Pi sessions from its JSONL session files.

Pi keeps one append-only JSONL file for each session, under
`<session dir>/--<cwd>--/<timestamp>_<id>.jsonl`. The first line is a header;
every later line is an entry with its own `id` and `parentId`, which form a
tree. A `message` entry holds one message: a person's, the model's, or a tool's
result. Usage is on each assistant message. This reader is OpenAIDR's own
(`docs/specs/pi.md`, ADR-0043).

**An entry is a turn, keyed by its own id.** Pi writes each entry once and
never moves it, so a growing file cannot move a span (ADR-0001). A tool result
updates the call it names and adds no turn of its own, unless a `codemode`
script ran tools: those calls are recorded on the result, and they become a
turn keyed by the result's entry id.

**Every branch is read.** Pi keeps the entries of a branch a person left with
`/tree`. The agent did that work, so its calls are in the session, in file
order.

**An outcome is Pi's.** A result's status is its `isError` flag. Only Pi's own
fixed words make it a refusal or an interruption.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
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
from openaidr.toolnames import split_tool_name

SOURCE = "pi"
SESSIONS = "sessions"
MCP_CONFIG = "mcp.json"
PROJECT_DIR = ".pi"

#: The reason Pi writes when an extension blocks a call and gives none.
_BLOCKED = "Tool execution was blocked"
#: Pi's own words for a call stopped before or while it ran.
_ABORTED = frozenset({"Operation aborted", "aborted", "Command aborted"})
#: What Pi's shell tools append when a command exits non-zero.
_EXIT = re.compile(r"(?:^|\n)Command exited with code (-?\d+)\s*$")
_SHELLS = frozenset({"bash", "powershell"})
#: A nested call's own status words, as Pi records them.
_NESTED: dict[str, Status] = {"ok": "ok", "error": "error", "unfinished": "unknown"}
#: The system-prompt sections whose material comes from somewhere other than
#: Pi itself, and the context source each one is.
_SECTIONS = {"skills": "skill_listing", "mcp_servers": "mcp_instructions"}
_MCP_PREFIX = "mcp__"
_SANITIZE = re.compile(r"[^A-Za-z0-9_]")


def default_agent_dir() -> Path:
    """`$PI_CODING_AGENT_DIR`, else `~/.pi/agent`."""
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".pi" / "agent"


def default_root() -> Path:
    """`$PI_CODING_AGENT_SESSION_DIR`, else the agent directory's `sessions`."""
    configured = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    return Path(configured).expanduser() if configured else default_agent_dir() / SESSIONS


# --- one file --------------------------------------------------------------------


@dataclass
class _File:
    """What has been read of one session file, and where the read stopped."""

    header: bytes = b""
    offset: int = 0
    #: Complete lines read, blank ones included: an entry with no `id`, from
    #: Pi's first file version, is keyed by its line.
    lines: int = 0
    entries: list[tuple[int, dict[str, object]]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _read(path: Path, known: _File | None) -> _File:
    """Every complete line of the file, reading on from `known` when the file
    only grew. A file that shrank or whose header changed, as Pi's migration
    of an older version rewrites it, is read again from the start. Raises
    `OSError`."""
    with path.open("rb") as handle:
        header = handle.readline()
        size = os.fstat(handle.fileno()).st_size
        if known is None or known.header != header or size < known.offset:
            known = _File(header=header)
        handle.seek(known.offset)
        while True:
            raw = handle.readline()
            if not raw.endswith(b"\n"):
                # A line still being written is read when it is complete.
                break
            known.offset += len(raw)
            known.lines += 1
            line = known.lines
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except (ValueError, RecursionError):
                known.warnings.append(f"line {line} is not JSON")
                continue
            if isinstance(record, dict):
                known.entries.append((line, record))
            else:
                known.warnings.append(f"line {line} is not an object")
    return known


# --- the session -------------------------------------------------------------------


@dataclass
class _Call:
    turn: int
    index: int
    call_id: str | None
    name: str
    arguments: dict[str, object]
    result: dict[str, object] | None = None


@dataclass
class _Turn:
    key: str
    role: str
    text: str
    at: datetime | None
    calls: list[_Call] = field(default_factory=list)
    nested: list[ToolCall] = field(default_factory=list)
    response_id: str | None = None


def _session(
    path: Path, file: _File, servers: Mapping[str, str], failures: list[ReaderFailure]
) -> Session | None:
    for warning in file.warnings:
        failures.append(ReaderFailure(SOURCE, f"{path}: {warning}"))
    if not file.entries or file.entries[0][1].get("type") != "session":
        if file.entries:
            failures.append(ReaderFailure(SOURCE, f"{path}: no session header"))
        return None
    header = file.entries[0][1]
    native = _string(header.get("id"))
    if native is None:
        failures.append(ReaderFailure(SOURCE, f"{path}: the session header names no id"))
        return None
    session_id = f"{SOURCE}:{native}"
    cwd = _string(header.get("cwd"))
    started = _instant(header.get("timestamp"))
    last = started
    turns: list[_Turn] = []
    calls: dict[str, _Call] = {}
    context: list[ContextItem] = []
    compactions: list[Compaction] = []
    responses: dict[str, ModelResponse] = {}
    title: str | None = None
    initial: str | None = None
    for line, record in file.entries[1:]:
        key = _string(record.get("id")) or f"L{line}"
        at = _instant(record.get("timestamp"))
        if at is not None and (last is None or at > last):
            last = at
        kind = record.get("type")
        if kind == "message":
            message = _mapping(record.get("message"))
            role = message.get("role")
            if role == "user":
                text = _joined(message.get("content"))
                turns.append(_Turn(key, "user", text, at))
                if initial is None and text:
                    initial = text
            elif role == "assistant":
                turn = _Turn(key, "assistant", _joined(message.get("content")), at)
                for part in _parts(message.get("content")):
                    if part.get("type") != "toolCall":
                        continue
                    name = _string(part.get("name"))
                    if name is None:
                        continue
                    call = _Call(
                        turn=len(turns),
                        index=len(turn.calls),
                        call_id=_string(part.get("id")),
                        name=name,
                        arguments=dict(_mapping(part.get("arguments"))),
                    )
                    turn.calls.append(call)
                    if call.call_id is not None:
                        calls.setdefault(call.call_id, call)
                response = _response(message, key, at, responses)
                if response is not None:
                    responses[response.response_id] = response
                    turn.response_id = response.response_id
                turns.append(turn)
                error = _string(message.get("errorMessage"))
                if message.get("stopReason") == "error" and error is not None:
                    turns.append(_Turn(f"{key}.error", "error", error, at))
            elif role == "toolResult":
                call = calls.get(_string(message.get("toolCallId")) or "")
                if call is not None and call.result is None:
                    call.result = dict(message)
                    nested = _nested(session_id, key, message, cwd, servers)
                    if nested:
                        turns.append(_Turn(key, "assistant", "", at, nested=nested))
            elif role == "bashExecution":
                # A command the person ran with `!`. Its output reaches the
                # model as their words unless they kept it out.
                text = _string(message.get("command")) or ""
                output = _string(message.get("output"))
                if output and not message.get("excludeFromContext"):
                    text = f"{text}\n{output}"
                turns.append(_Turn(key, "user", text, at))
            elif role == "custom":
                _context(context, session_id, "injected", message, at)
            elif role == "system":
                sections = _mapping(message.get("sections"))
                for section, source in _SECTIONS.items():
                    text = sections.get(section)
                    if isinstance(text, str) and text:
                        context.append(
                            ContextItem(
                                # Items are appended in file order, so the count
                                # so far is stable as the file grows.
                                span=span_id(session_id, "context", len(context)),
                                source=source,
                                name=section,
                                text=text,
                                occurred_at=at,
                            )
                        )
        elif kind == "custom_message":
            _context(context, session_id, "injected", record, at)
        elif kind == "compaction":
            compactions.append(
                Compaction(
                    trigger="unknown",
                    pre_tokens=_count(record.get("tokensBefore")),
                    dropped_tokens=None,
                    occurred_at=at,
                )
            )
            _side_usage(record, key, at, responses)
        elif kind in ("branch_summary", "usage"):
            _side_usage(record, key, at, responses)
        elif kind == "session_info":
            title = _string(record.get("name"))
    built: list[Turn] = []
    for position, turn in enumerate(turns):
        tool_calls = tuple(
            _tool_call(session_id, turn.key, call, cwd, servers) for call in turn.calls
        ) + tuple(turn.nested)
        built.append(
            Turn(
                position=position,
                key=turn.key,
                role=turn.role,
                text=turn.text,
                is_sidechain=False,
                tool_calls=tool_calls,
                occurred_at=turn.at,
                response_id=turn.response_id,
            )
        )
    ordered = tuple(responses.values())
    model = next((r.model for r in reversed(ordered) if r.model), None)
    return Session(
        session_id=session_id,
        agent_kind=map_source(SOURCE),
        source=SOURCE,
        started_at=started,
        model=model,
        working_directory=cwd,
        machine=socket.gethostname() or None,
        user=_user(),
        turns=tuple(built),
        initial_prompt=initial,
        context_items=tuple(context),
        compactions=tuple(compactions),
        last_activity_at=last,
        title=title,
        responses=ordered,
        compactions_recorded=True,
        subagents_recorded=False,
    )


def _tool_call(
    session_id: str, key: str, call: _Call, cwd: str | None, servers: Mapping[str, str]
) -> ToolCall:
    server, tool = _split(call.name, servers)
    result = call.result
    body: str | None = None
    status: Status = "pending"
    denial: str | None = None
    exit_code: int | None = None
    duration: int | None = None
    truncated = False
    if result is not None:
        body = _joined(result.get("content"))
        failed = result.get("isError") is True
        status, denial = _status(body, failed)
        duration = _count(result.get("durationMs"))
        details = _mapping(result.get("details"))
        truncated = bool(details.get("truncation")) or "fullOutputPath" in details
        if server is None and tool in _SHELLS and status in ("ok", "error"):
            exit_code = 0 if not failed else _exit_code(body)
    return ToolCall(
        span=span_id(session_id, key, call.index),
        tool_name=tool,
        mcp_server=server,
        status=status,
        arguments=call.arguments,
        result=body,
        result_size=len(body) if body is not None else None,
        error_text=body if status == "error" else None,
        truncated=truncated,
        provider_call_id=call.call_id,
        duration_ms=duration,
        denial_kind=denial,
        working_directory=cwd if server is None and tool in _SHELLS else None,
        tool_class=tool_class(SOURCE, tool, server),
        canonical_arguments=canonical_arguments(SOURCE, tool, server, call.arguments),
        exit_code=exit_code,
    )


def _nested(
    session_id: str,
    key: str,
    message: Mapping[str, object],
    cwd: str | None,
    servers: Mapping[str, str],
) -> list[ToolCall]:
    """The calls a `codemode` script made, each its own call (ADR-0036)."""
    record = _mapping(message.get("nestedCalls"))
    found: list[ToolCall] = []
    for index, nested in enumerate(_parts(record.get("calls"))):
        name = _string(nested.get("name"))
        if name is None:
            continue
        server, tool = _split(name, servers)
        arguments = dict(_mapping(nested.get("arguments")))
        status = _NESTED.get(_string(nested.get("status")) or "", "unknown")
        error = _string(nested.get("error"))
        found.append(
            ToolCall(
                span=span_id(session_id, key, index),
                tool_name=tool,
                mcp_server=server,
                status=status,
                arguments=arguments,
                result=None,
                result_size=None,
                error_text=error if status == "error" else None,
                # Pi leaves out arguments over its size limit, and says how large.
                truncated="argumentsBytes" in nested,
                provider_call_id=_string(nested.get("id")),
                duration_ms=_count(nested.get("durationMs")),
                working_directory=cwd if server is None and tool in _SHELLS else None,
                tool_class=tool_class(SOURCE, tool, server),
                canonical_arguments=canonical_arguments(SOURCE, tool, server, arguments),
            )
        )
    return found


def _status(body: str, failed: bool) -> tuple[Status, str | None]:
    if not failed:
        return "ok", None
    stripped = body.strip()
    if stripped == _BLOCKED:
        return "rejected", "permission-rule"
    last = stripped.rsplit("\n\n", 1)[-1]
    if stripped in _ABORTED or last == "Command aborted":
        return "interrupted", None
    return "error", None


def _exit_code(body: str) -> int | None:
    found = _EXIT.search(body)
    return int(found.group(1)) if found else None


def _split(raw: str, servers: Mapping[str, str]) -> tuple[str | None, str]:
    """`(server, tool)`, the server named as it is configured.

    Pi writes `mcp__<server>__<tool>`, with the server's name sanitized. A
    configured name that writes to the start of `raw` splits it; two that do
    leave the call unsplit, since either could be right. With none, Pi's own
    delimiter splits it and the written server name stands."""
    if not raw.startswith(_MCP_PREFIX):
        return None, raw
    matches = [
        (written, name)
        for written, name in servers.items()
        if raw.startswith(f"{_MCP_PREFIX}{written}__") and len(raw) > len(written) + 7
    ]
    if len(matches) == 1:
        written, name = matches[0]
        return name, raw[len(_MCP_PREFIX) + len(written) + 2 :]
    if len(matches) > 1:
        return None, raw
    return split_tool_name(SOURCE, raw)


def _response(
    message: Mapping[str, object],
    key: str,
    at: datetime | None,
    seen: Mapping[str, ModelResponse],
) -> ModelResponse | None:
    usage = _usage(message.get("usage"))
    if usage is None:
        return None
    response_id = _string(message.get("responseId"))
    if response_id is None or response_id in seen:
        response_id = key
    return ModelResponse(
        response_id=response_id,
        request_id=None,
        model=_string(message.get("responseModel")) or _string(message.get("model")),
        occurred_at=at,
        final=True,
        is_sidechain=False,
        usage=usage,
    )


def _side_usage(
    record: Mapping[str, object],
    key: str,
    at: datetime | None,
    responses: dict[str, ModelResponse],
) -> None:
    """Usage Pi records beside the conversation: a summary's, a cache warm's."""
    usage = _usage(record.get("usage"))
    if usage is None:
        return
    responses[key] = ModelResponse(
        response_id=key,
        request_id=None,
        model=_string(record.get("model")),
        occurred_at=at,
        final=True,
        is_sidechain=False,
        usage=usage,
    )


def _usage(value: object) -> TokenUsage | None:
    """Pi's buckets are disjoint: `input` is uncached input. `reasoning` is the
    part of `output` spent thinking, and `cacheWrite1h` the part of
    `cacheWrite` kept for an hour."""
    if not isinstance(value, Mapping):
        return None
    write = _count(value.get("cacheWrite"))
    long = _count(value.get("cacheWrite1h"))
    return TokenUsage(
        input_tokens=_count(value.get("input")),
        cache_read_input_tokens=_count(value.get("cacheRead")),
        cache_creation_input_tokens=write,
        cache_creation_5m_input_tokens=write - long
        if write is not None and long is not None
        else None,
        cache_creation_1h_input_tokens=long,
        output_tokens=_count(value.get("output")),
        reasoning_tokens=_count(value.get("reasoning")),
    )


def _context(
    items: list[ContextItem],
    session_id: str,
    source: str,
    record: Mapping[str, object],
    at: datetime | None,
) -> None:
    text = _joined(record.get("content"))
    if text:
        items.append(
            ContextItem(
                span=span_id(session_id, "context", len(items)),
                source=source,
                name=_string(record.get("customType")),
                text=text,
                occurred_at=at,
            )
        )


# --- the reader --------------------------------------------------------------------


class PiReader:
    """Reads Pi's session files."""

    agent_kind = "pi"

    def __init__(self, root: Path | None = None, *, agent_dir: Path | None = None) -> None:
        self._root = (root if root is not None else default_root()).absolute()
        self._agent_dir = (agent_dir if agent_dir is not None else default_agent_dir()).absolute()
        self._files: dict[Path, _File] = {}
        self._digests: dict[str, str] = {}

    def locations(self) -> tuple[Path, ...]:
        return (self._root,)

    def collect(self, window: Window) -> tuple[list[Session], list[ReaderFailure]]:
        paths, failures = self._discover()
        sessions: list[Session] = []
        for path in paths:
            if window.since is not None:
                try:
                    modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
                except OSError as error:
                    failures.append(self._failure(f"{path}: {error.strerror or error}"))
                    continue
                # A line is written no later than the file's last change.
                if modified < window.since:
                    continue
            session = self._session(path, failures)
            if session is None:
                continue
            if window.since is not None and (
                session.last_activity_at is None or session.last_activity_at < window.since
            ):
                continue
            sessions.append(session)
        return sessions, failures

    def collect_file(self, path: Path) -> tuple[list[Session], list[ReaderFailure]]:
        """The session one changed file holds, if it differs from what was last
        returned for it."""
        path = path.absolute()
        if path.suffix != ".jsonl" or not path.is_relative_to(self._root):
            return [], []
        if len(path.relative_to(self._root).parts) > 2:
            return [], []
        failures: list[ReaderFailure] = []
        session = self._session(path, failures)
        if session is None:
            return [], failures
        digest = hashlib.sha256(repr(session).encode("utf-8")).hexdigest()
        if self._digests.get(session.session_id) == digest:
            return [], failures
        self._digests[session.session_id] = digest
        return [session], failures

    def _session(self, path: Path, failures: list[ReaderFailure]) -> Session | None:
        try:
            file = _read(path, self._files.get(path))
        except FileNotFoundError:
            self._files.pop(path, None)
            return None
        except OSError as error:
            failures.append(self._failure(f"{path}: {error.strerror or error}"))
            return None
        self._files[path] = file
        cwd = _string(file.entries[0][1].get("cwd")) if file.entries else None
        return _session(path, file, self._servers(cwd, failures), failures)

    def _servers(self, cwd: str | None, failures: list[ReaderFailure]) -> dict[str, str]:
        """Each configured MCP server's written name, mapped to its configured
        name: from the agent directory's `mcp.json` and the project's. A written
        name two configured names share maps to neither."""
        files = [self._agent_dir / MCP_CONFIG]
        if cwd:
            files.append(Path(cwd) / PROJECT_DIR / MCP_CONFIG)
        names: dict[str, set[str]] = {}
        for file in files:
            for name in _configured(file, failures):
                names.setdefault(_SANITIZE.sub("_", name), set()).add(name)
        return {written: next(iter(found)) for written, found in names.items() if len(found) == 1}

    def _discover(self) -> tuple[list[Path], list[ReaderFailure]]:
        """Every `.jsonl` in the session directory and one level below it."""
        paths: list[Path] = []
        failures: list[ReaderFailure] = []
        errors: list[OSError] = []
        for dirpath, dirnames, filenames in os.walk(self._root, onerror=errors.append):
            if Path(dirpath) != self._root:
                dirnames[:] = []
            paths.extend(Path(dirpath) / name for name in filenames if name.endswith(".jsonl"))
        for error in errors:
            if isinstance(error, FileNotFoundError) and Path(str(error.filename)) == self._root:
                continue
            failures.append(self._failure(f"{error.filename}: {error.strerror or error}"))
        return sorted(paths), failures

    def _failure(self, message: str) -> ReaderFailure:
        return ReaderFailure(agent_kind=self.agent_kind, message=message)


def _configured(path: Path, failures: list[ReaderFailure]) -> list[str]:
    """The server names one `mcp.json` configures; none for a missing file."""
    try:
        mode = path.stat().st_mode
        if not stat.S_ISREG(mode):
            return []
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError, RecursionError) as error:
        failures.append(
            ReaderFailure(
                SOURCE, f"{path}: not read, MCP servers keep their written names ({error})"
            )
        )
        return []
    servers = document.get("mcpServers") if isinstance(document, dict) else None
    return [name for name in servers if isinstance(name, str)] if isinstance(servers, dict) else []


# --- small helpers -------------------------------------------------------------------


def _parts(content: object) -> list[Mapping[str, object]]:
    return [p for p in content if isinstance(p, Mapping)] if isinstance(content, list) else []


def _joined(content: object) -> str:
    if isinstance(content, str):
        return content
    texts = [p.get("text") for p in _parts(content) if p.get("type") == "text"]
    return "\n".join(t for t in texts if isinstance(t, str))


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _user() -> str | None:
    try:
        return getpass.getuser() or None
    except (KeyError, OSError):
        return None
