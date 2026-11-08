"""Reads Cursor sessions from the CLI's chat stores.

The Cursor CLI (`agent`, `cursor-agent`) keeps one SQLite store for each
conversation, a sub-agent's included, under
`<config root>/chats/<md5(cwd)>/<agentId>/store.db`: content-addressed blobs
and one `meta` row. The current root blob lists the **prompt messages**, the
JSON messages the model received, and the turns, protobuf messages that hold
each step's time. `adr-sensor`'s Cursor parser reads a different store and keys
messages by position, so this reader is OpenAIDR's own (`docs/specs/cursor.md`,
ADR-0037).

**A prompt message is a turn.** An assistant message is keyed by its own id,
any other by its blob id, so the list growing cannot move a span (ADR-0001). A
tool message updates the call it names and adds no turn.

**An outcome is Cursor's, never the text's.** A call's status is the typed
outcome Cursor writes beside each result (`highLevelToolCallResult`). What a
command printed is never read as a success, a failure or a refusal.

Usage, the entrypoint, client error times and MCP failures come from Cursor's
debug log (`cursor_log`, ADR-0038).
"""

from __future__ import annotations

import base64
import dataclasses
import getpass
import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from openaidr.kinds import map_source
from openaidr.model import (
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
from openaidr.readers.cursor_ide_log import LOG_NAME as IDE_LOG_NAME
from openaidr.readers.cursor_ide_log import CursorIdeLogs
from openaidr.readers.cursor_log import (
    CursorLogs,
    Outcome,
    Placement,
    Usage,
    default_log_root,
)
from openaidr.toolclass import canonical_arguments, tool_class

SOURCE = "cursor"
CHATS = "chats"
PROJECTS = "projects"
STORE = "store.db"
META_JSON = "meta.json"
TRANSCRIPTS = "agent-transcripts"
#: The client's record of each MCP server it loaded for a project: the
#: identifier a call names, and the name the server's configuration gives it.
MCPS = "mcps"
SERVER_METADATA = "SERVER_METADATA.json"

#: The files beside a store whose change can change its session.
_STORE_FILES = frozenset({STORE, f"{STORE}-wal", META_JSON})
#: The desktop app's database, under its user data directory (ADR-0039).
GLOBAL_STORAGE = Path("User") / "globalStorage"
STATE_DB = "state.vscdb"
IDE_LOGS = "logs"
_STATE_FILES = frozenset({STATE_DB, f"{STATE_DB}-wal"})
#: `cursorDiskKV` keys: a conversation, a blob, and a root's older pointer.
_COMPOSER = "composerData:"
_BLOB = "agentKv:blob:"
_CHECKPOINT = "agentKv:checkpoint:"
#: The entrypoint of a conversation a person started in the desktop app.
_IDE = "ide"
#: How long a pass waits for a store Cursor is writing before it reports it busy.
_BUSY_SECONDS = 2.0
#: The name Cursor gives a conversation nobody named (`agent-kv`).
_DEFAULT_NAME = "New Agent"
#: The tool that names its own MCP server and tool.
_MCP_TOOL = "CallMcpTool"

#: `ConversationStateStructure` fields read (agent.v1, CLI 2026.10.19).
_ROOT_PROMPT_MESSAGES = 1
_ROOT_TURNS = 8
_ROOT_WORKSPACE_URIS = 9
_ROOT_STARTED_MS = 26
#: `ConversationTurnStructure.agent_conversation_turn`, and its fields read.
_TURN_AGENT = 1
_AGENT_TURN_USER_MESSAGE = 1
_AGENT_TURN_STEPS = 2
#: `ConversationStep` cases, and the fields read in each.
_STEP_ASSISTANT = 1
_STEP_TOOL_CALL = 2
_ASSISTANT_TEXT = 1
_ASSISTANT_STARTED_MS = 2
_TOOL_CALL_ID = 57
_TOOL_CALL_STARTED_MS = 59
_TOOL_CALL_COMPLETED_MS = 60
#: `UserMessage.started_at_ms`.
_USER_STARTED_MS = 25

#: The outcomes Cursor writes that mean the call did not do what it was asked.
#: With `success`, `failure`, `rejected` and `permissionDenied` these are every
#: case of the result oneofs the reader names; a result can carry other fields
#: beside its case (a shell's `sandboxPolicy`, `isBackground`, `pid`).
_ERROR_CASES = frozenset(
    {
        "timeout",
        "spawnError",
        "error",
        "fileNotFound",
        "notFile",
        "invalidFile",
        "fileBusy",
        "readPermissionDenied",
        "writePermissionDenied",
        "toolNotFound",
        "serverNotFound",
    }
)
#: Who refused, from Cursor's own reason (`CLI/index.js` and the permission
#: and hook paths). First match wins. In print mode Cursor writes `User
#: rejected` for any call it does not approve.
_DENIALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^User rejected|^Rejected by user", re.IGNORECASE), "user-rejected"),
    (
        re.compile(
            r"permissions configuration|^Blocked by |cursorignore|Cursor internal file|"
            r"denylist|blocked by (?:a|preToolUse) hook",
            re.IGNORECASE,
        ),
        "permission-rule",
    ),
)
_CASES = _ERROR_CASES | {"success", "failure", "rejected", "permissionDenied"}
_USER_QUERY = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
_LEADING_TAG = re.compile(r"^\s*<([A-Za-z_][\w-]*)>")


def default_root() -> Path:
    """Cursor's configuration root, where `chats/` and `cli-config.json` live:
    `$CURSOR_CONFIG_DIR`, else `$XDG_CONFIG_HOME/cursor`, else `~/.cursor`."""
    named = os.environ.get("CURSOR_CONFIG_DIR")
    if named:
        return Path(named).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "cursor"
    return Path.home() / ".cursor"


def default_data_root() -> Path:
    """Cursor's data root, where `projects/` and the transcripts live."""
    named = os.environ.get("CURSOR_DATA_DIR")
    return Path(named).expanduser() if named else Path.home() / ".cursor"


def default_user_data_root() -> Path:
    """The desktop app's user data directory, resolved in Cursor's own order:
    `$VSCODE_PORTABLE/user-data`, else `$VSCODE_APPDATA/Cursor`, else the
    platform's application data directory and `Cursor`."""
    portable = os.environ.get("VSCODE_PORTABLE")
    if portable:
        return Path(portable).expanduser() / "user-data"
    appdata = os.environ.get("VSCODE_APPDATA")
    if appdata:
        return Path(appdata).expanduser() / "Cursor"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Cursor"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".config") / "Cursor"


# --- protobuf wire format, the few fields the reader needs -------------------


def _varint(data: bytes, index: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if index >= len(data) or shift > 63:
            raise ValueError("truncated varint")
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, index
        shift += 7


def _fields(data: bytes) -> list[tuple[int, int | bytes]]:
    """Every field of one protobuf message, as `(number, value)`: an int for a
    varint, bytes for a length-delimited field. Fixed-width fields are skipped.
    Raises `ValueError` on bytes that are not a message."""
    out: list[tuple[int, int | bytes]] = []
    index = 0
    while index < len(data):
        key, index = _varint(data, index)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, index = _varint(data, index)
            out.append((number, value))
        elif wire == 2:
            length, index = _varint(data, index)
            if index + length > len(data):
                raise ValueError("truncated field")
            out.append((number, data[index : index + length]))
            index += length
        elif wire == 1:
            index += 8
        elif wire == 5:
            index += 4
        else:
            raise ValueError(f"wire type {wire}")
    return out


def _first(fields: list[tuple[int, int | bytes]], number: int) -> int | bytes | None:
    return next((value for n, value in fields if n == number), None)


def _all_bytes(fields: list[tuple[int, int | bytes]], number: int) -> list[bytes]:
    return [value for n, value in fields if n == number and isinstance(value, bytes)]


def _text(value: int | bytes | None) -> str | None:
    if not isinstance(value, bytes):
        return None
    try:
        return value.decode("utf-8") or None
    except UnicodeDecodeError:
        return None


def _ms(value: int | bytes | None) -> datetime | None:
    if not isinstance(value, int) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


# --- one store -----------------------------------------------------------------


@dataclass
class _Steps:
    """What the protobuf turns say about times, keyed for the JSON messages."""

    calls: dict[str, tuple[datetime | None, datetime | None]] = field(default_factory=dict)
    texts: list[tuple[str, datetime | None]] = field(default_factory=list)
    users: list[datetime | None] = field(default_factory=list)


@dataclass
class _Store:
    """One conversation's store, read in one transaction."""

    agent_id: str
    meta: dict[str, object]
    messages: list[tuple[str, dict[str, object]]]
    steps: _Steps
    workspace: str | None
    started_at: datetime | None
    warnings: list[str] = field(default_factory=list)


def _read_store(path: Path) -> _Store | None:
    """The store's current root, its prompt messages and step times. None for a
    store with no conversation yet. Raises `sqlite3.Error` or `OSError`."""
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=_BUSY_SECONDS
    )
    try:
        connection.execute("BEGIN")
        row = connection.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
        if row is None or not isinstance(row[0], str):
            return None
        try:
            meta = json.loads(bytes.fromhex(row[0]).decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError) as error:
            raise ValueError(f"meta row is not hex JSON ({type(error).__name__})") from None
        if not isinstance(meta, dict):
            raise TypeError("meta row is not an object")
        agent_id = meta.get("agentId")
        root_id = meta.get("latestRootBlobId")
        if not isinstance(agent_id, str) or not isinstance(root_id, str) or not root_id:
            return None

        def blob(identifier: bytes | str) -> bytes | None:
            key = identifier.hex() if isinstance(identifier, bytes) else identifier
            found = connection.execute("SELECT data FROM blobs WHERE id = ?", (key,)).fetchone()
            return found[0] if found is not None and isinstance(found[0], bytes) else None

        root_data = blob(root_id)
        if root_data is None:
            return None
        return _decode(agent_id, meta, root_data, blob)
    finally:
        connection.close()


def _decode(
    agent_id: str,
    meta: dict[str, object],
    root_data: bytes,
    blob: Callable[[bytes | str], bytes | None],
) -> _Store:
    """A conversation from its root and its blobs, wherever they are kept: the
    CLI's store or the desktop app's database (ADR-0039). Raises `ValueError`
    on a root that does not decode."""
    warnings: list[str] = []
    root = _fields(root_data)
    messages: list[tuple[str, dict[str, object]]] = []
    for identifier in _all_bytes(root, _ROOT_PROMPT_MESSAGES):
        data = blob(identifier)
        if data is None:
            warnings.append(f"prompt message {identifier.hex()[:12]} is missing")
            continue
        try:
            message = json.loads(data)
        except (ValueError, UnicodeError, RecursionError):
            warnings.append(f"prompt message {identifier.hex()[:12]} is not JSON")
            continue
        if isinstance(message, dict):
            messages.append((identifier.hex(), message))
    steps = _Steps()
    for identifier in _all_bytes(root, _ROOT_TURNS):
        data = blob(identifier)
        if data is None:
            continue
        try:
            _read_turn(data, blob, steps)
        except ValueError:
            warnings.append(f"turn {identifier.hex()[:12]} does not decode")
    uris = [_text(value) for value in _all_bytes(root, _ROOT_WORKSPACE_URIS)]
    return _Store(
        agent_id=agent_id,
        meta=meta,
        messages=messages,
        steps=steps,
        workspace=_path_of(next((uri for uri in uris if uri), None)),
        started_at=_ms(_first(root, _ROOT_STARTED_MS)),
        warnings=warnings,
    )


@dataclass
class _Composer:
    """One desktop conversation: its decoded store, and what `composerData`
    names beside it in the CLI sidecar's names."""

    store: _Store
    meta_json: dict[str, object]
    entrypoint: str | None
    parent: str | None
    call: str | None


def _read_desktop(
    path: Path, cache: Mapping[str, tuple[str, _Composer | None, str | None]]
) -> tuple[dict[str, tuple[str, _Composer | None, str | None]], list[str]]:
    """Every conversation in the desktop app's database, by id, with the
    SHA-256 of its `composerData` value. One whose value is unchanged since
    `cache` reads no blobs, and repeats the warning a failed parse raised, so
    a later pass does not under-report a conversation it still omits. Raises
    `sqlite3.Error` or `OSError`."""
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=_BUSY_SECONDS
    )
    try:
        connection.execute("BEGIN")
        # `;` follows `:`, so this is every key that starts with the prefix.
        rows = connection.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key >= ? AND key < ?",
            (_COMPOSER, _COMPOSER[:-1] + ";"),
        ).fetchall()

        def value(key: str) -> object:
            found = connection.execute(
                "SELECT value FROM cursorDiskKV WHERE key = ?", (key,)
            ).fetchone()
            return found[0] if found is not None else None

        def blob(identifier: bytes | str) -> bytes | None:
            key = identifier.hex() if isinstance(identifier, bytes) else identifier
            return _blob_bytes(value(_BLOB + key))

        composers: dict[str, tuple[str, _Composer | None, str | None]] = {}
        warnings: list[str] = []
        for key, raw in rows:
            composer_id = key[len(_COMPOSER) :]
            data = raw.encode("utf-8") if isinstance(raw, str) else raw
            if not composer_id or not isinstance(data, bytes):
                continue
            digest = hashlib.sha256(data).hexdigest()
            cached = cache.get(composer_id)
            if cached is not None and cached[0] == digest:
                composers[composer_id] = cached
                if cached[2] is not None:
                    warnings.append(cached[2])
                continue
            composer: _Composer | None = None
            warning: str | None = None
            try:
                composer = _composer(composer_id, data, value, blob)
            except (ValueError, UnicodeError, RecursionError, TypeError) as error:
                warning = f"conversation {composer_id}: {error}"
                warnings.append(warning)
            composers[composer_id] = (digest, composer, warning)
        return composers, warnings
    finally:
        connection.close()


def _composer(
    composer_id: str,
    data: bytes,
    value: Callable[[str], object],
    blob: Callable[[bytes | str], bytes | None],
) -> _Composer | None:
    """One `composerData` value as a conversation, or None before it has one.
    Its facts take the CLI meta row's names, so one projection serves both."""
    document = json.loads(data)
    if not isinstance(document, dict):
        raise TypeError("composerData is not an object")
    root_data = _desktop_root(document, composer_id, value, blob)
    if not root_data:
        return None
    info = _mapping(document.get("subagentInfo"))
    root = _string(info.get("rootParentConversationId"))
    parent = _string(info.get("parentComposerId"))
    call = _string(info.get("toolCallId"))
    meta: dict[str, object] = {
        "agentId": composer_id,
        "name": _string(document.get("name")),
        "createdAt": _int(document.get("createdAt")),
        "lastUsedModel": _string(_mapping(document.get("modelConfig")).get("modelName")),
    }
    if root is not None:
        meta["subagentInfo"] = {
            "rootParentAgentId": root,
            "parentAgentId": parent,
            "toolCallId": call,
        }
    uri = _mapping(_mapping(document.get("workspaceIdentifier")).get("uri"))
    meta_json: dict[str, object] = {
        "cwd": _string(uri.get("fsPath")),
        "updatedAtMs": _int(document.get("lastUpdatedAt")),
    }
    # A conversation an automation or a background agent made may have no
    # person behind it, and none was seen (spec C12).
    started_by_program = document.get("createdFromBackgroundAgent") or document.get(
        "createdFromLocalScheduledTask"
    )
    return _Composer(
        store=_decode(composer_id, meta, root_data, blob),
        meta_json=meta_json,
        entrypoint=None if started_by_program else _IDE,
        parent=parent,
        call=call,
    )


def _desktop_root(
    document: Mapping[str, object],
    composer_id: str,
    value: Callable[[str], object],
    blob: Callable[[bytes | str], bytes | None],
) -> bytes | None:
    """The root: `conversationState` as Cursor decodes it, `~` and base64 else
    hex; before data version 11, the blob `agentKv:checkpoint:<id>` names."""
    state = document.get("conversationState")
    if isinstance(state, str) and state:
        if state.startswith("~"):
            return base64.b64decode(state[1:])
        return bytes.fromhex(state)
    pointer = value(_CHECKPOINT + composer_id)
    if isinstance(pointer, bytes):
        pointer = pointer.decode("ascii", errors="replace")
    if not isinstance(pointer, str) or not pointer:
        return None
    return blob(pointer)


def _blob_bytes(data: object) -> bytes | None:
    """A blob row's bytes. Cursor writes binary, or hex text in older rows,
    and one zero byte for an empty blob."""
    if isinstance(data, bytes):
        return b"" if data == b"\x00" else data
    if isinstance(data, str):
        try:
            return bytes.fromhex(data)
        except ValueError:
            return None
    return None


def _read_turn(data: bytes, blob: Callable[[bytes | str], bytes | None], steps: _Steps) -> None:
    agent = _first(_fields(data), _TURN_AGENT)
    if not isinstance(agent, bytes):
        return
    fields = _fields(agent)
    user = _first(fields, _AGENT_TURN_USER_MESSAGE)
    if isinstance(user, bytes):
        user_data = blob(user)
        steps.users.append(
            _ms(_first(_fields(user_data), _USER_STARTED_MS)) if user_data is not None else None
        )
    for identifier in _all_bytes(fields, _AGENT_TURN_STEPS):
        step = blob(identifier)
        if step is None:
            continue
        for case, value in _fields(step):
            if not isinstance(value, bytes):
                continue
            inner = _fields(value)
            if case == _STEP_TOOL_CALL:
                call_id = _text(_first(inner, _TOOL_CALL_ID))
                if call_id is not None:
                    steps.calls[call_id] = (
                        _ms(_first(inner, _TOOL_CALL_STARTED_MS)),
                        _ms(_first(inner, _TOOL_CALL_COMPLETED_MS)),
                    )
            elif case == _STEP_ASSISTANT:
                text = _text(_first(inner, _ASSISTANT_TEXT))
                if text is not None:
                    steps.texts.append((text, _ms(_first(inner, _ASSISTANT_STARTED_MS))))


# --- projection ----------------------------------------------------------------


@dataclass
class _Call:
    call_id: str
    tool_name: str
    mcp_server: str | None
    arguments: dict[str, object]


def _session(
    store: _Store,
    meta_json: Mapping[str, object],
    cwd: str | None,
    placement: Placement,
    transcript_errors: list[str],
    servers: Mapping[str, str],
) -> Session:
    meta = store.meta
    info = meta.get("subagentInfo")
    child = isinstance(info, dict) and isinstance(info.get("rootParentAgentId"), str)
    if child:
        assert isinstance(info, dict)
        session_id = f"{SOURCE}:{info['rootParentAgentId']}:{store.agent_id}"
    else:
        session_id = f"{SOURCE}:{store.agent_id}"
    sidechain = child

    results: dict[str, tuple[Mapping[str, object], Mapping[str, object]]] = {}
    for _, message in store.messages:
        if message.get("role") != "tool":
            continue
        options = _mapping(message.get("providerOptions"))
        outcome = _mapping(_mapping(options.get("cursor")).get("highLevelToolCallResult"))
        for part in _parts(message.get("content")):
            call_id = _string(part.get("toolCallId"))
            if part.get("type") == "tool-result" and call_id is not None:
                results.setdefault(call_id, (part, outcome))

    turns: list[Turn] = []
    contexts: list[ContextItem] = []
    seen: dict[str, int] = {}
    unused_texts = list(store.steps.texts)
    user_times = list(store.steps.users)
    initial_prompt: str | None = None
    #: Assistant turns, grouped by the user turn that preceded them.
    segments: list[list[int]] = []
    for blob_id, message in store.messages:
        role = message.get("role")
        content = message.get("content")
        if role == "system" or role == "tool":
            continue
        raw_key = _string(message.get("id")) if role == "assistant" else None
        base = raw_key or f"b{blob_id[:16]}"
        seen[base] = seen.get(base, 0) + 1
        key = base if seen[base] == 1 else f"{base}#{seen[base]}"
        if role == "user":
            text = content if isinstance(content, str) else _joined(content)
            query = _USER_QUERY.search(text)
            if isinstance(content, str) or query is None:
                tag = _LEADING_TAG.match(text)
                contexts.append(
                    ContextItem(
                        span=span_id(session_id, key, 0),
                        source="system" if isinstance(content, str) else "injected",
                        name=tag.group(1) if tag else None,
                        text=text,
                    )
                )
                continue
            words = query.group(1)
            if initial_prompt is None:
                initial_prompt = words
            at = user_times.pop(0) if user_times else None
            turns.append(
                Turn(
                    position=len(turns),
                    key=key,
                    role="user",
                    text=words,
                    is_sidechain=sidechain,
                    tool_calls=(),
                    occurred_at=at,
                )
            )
            segments.append([])
            continue
        if role != "assistant":
            continue
        text = _joined(content)
        calls = [call for call in (_call(part, servers) for part in _parts(content)) if call]
        if not text and not calls:
            continue
        times = [store.steps.calls.get(c.call_id, (None, None))[0] for c in calls]
        at = min((t for t in times if t is not None), default=None)
        if at is None and text:
            for index, (step_text, step_at) in enumerate(unused_texts):
                if step_text.strip() == text.strip():
                    at = step_at
                    del unused_texts[index]
                    break
        tool_calls = tuple(
            _tool_call(session_id, key, index, call, results.get(call.call_id), store.steps)
            for index, call in enumerate(calls)
        )
        if not segments:
            segments.append([])
        segments[-1].append(len(turns))
        turns.append(
            Turn(
                position=len(turns),
                key=key,
                role="assistant",
                text=text,
                is_sidechain=sidechain,
                tool_calls=tool_calls,
                occurred_at=at,
            )
        )

    responses = _responses(placement.usage, sidechain)
    turns = _link(turns, segments, placement)
    turns = _with_errors(turns, transcript_errors, placement.errors, sidechain)

    name = _string(meta.get("name"))
    title = _string(meta_json.get("title")) or (name if name != _DEFAULT_NAME else None)
    times = (
        [t.occurred_at for t in turns if t.occurred_at]
        + [r.occurred_at for r in responses if r.occurred_at]
        + [completed for _, completed in store.steps.calls.values() if completed is not None]
    )
    updated = _ms(_int(meta_json.get("updatedAtMs")))
    if updated is not None:
        times.append(updated)
    if placement.last_at is not None:
        times.append(placement.last_at)
    started = store.started_at or _ms(_int(meta.get("createdAt")))
    if started is None and times:
        started = min(times)
    model = next((r.model for r in reversed(responses) if r.model), None) or _string(
        meta.get("lastUsedModel")
    )
    return Session(
        session_id=session_id,
        agent_kind=map_source(SOURCE),
        source=SOURCE,
        started_at=started,
        model=model,
        working_directory=cwd,
        machine=socket.gethostname() or None,
        user=_user(),
        turns=tuple(turns),
        entrypoint=placement.surface,
        initial_prompt=initial_prompt,
        context_items=tuple(contexts),
        mcp_connections=placement.connections,
        mcp_log_state=placement.state,  # type: ignore[arg-type]
        last_activity_at=max(times) if times else started,
        title=title,
        responses=responses,
        compactions_recorded=False,
        subagents_recorded=True,
    )


def _call(part: Mapping[str, object], servers: Mapping[str, str]) -> _Call | None:
    if part.get("type") != "tool-call":
        return None
    call_id = _string(part.get("toolCallId"))
    name = _string(part.get("toolName"))
    if call_id is None or name is None:
        return None
    arguments = _mapping(part.get("args"))
    if name == _MCP_TOOL:
        server = _string(arguments.get("server"))
        tool = _string(arguments.get("toolName"))
        if server is not None and tool is not None:
            # The desktop app names a server by an identifier of its own,
            # `user-<name>` for `~/.cursor/mcp.json`; a configuration names it.
            server = servers.get(server, server)
            return _Call(call_id, tool, server, dict(_mapping(arguments.get("arguments"))))
    return _Call(call_id, name, None, dict(arguments))


def _tool_call(
    session_id: str,
    key: str,
    index: int,
    call: _Call,
    result: tuple[Mapping[str, object], Mapping[str, object]] | None,
    steps: _Steps,
) -> ToolCall:
    klass = tool_class(SOURCE, call.tool_name, call.mcp_server)
    started, completed = steps.calls.get(call.call_id, (None, None))
    duration = (
        int((completed - started).total_seconds() * 1000)
        if started is not None and completed is not None and completed >= started
        else None
    )
    status: Status = "pending"
    denial: str | None = None
    body_text: str | None = None
    error_text: str | None = None
    exit_code: int | None = None
    cwd: str | None = None
    if result is not None:
        part, outcome = result
        body_text = _body(part.get("result"))
        output = _mapping(outcome.get("output"))
        cases = [name for name in output if name in _CASES]
        case = cases[0] if len(cases) == 1 else None
        body = _mapping(output.get(case)) if case is not None else {}
        status, denial = _status(case, body, call.mcp_server is not None)
        if status in ("error", "rejected", "interrupted"):
            raw = outcome.get("rawErrorMessages")
            messages = [m for m in raw if isinstance(m, str) and m] if isinstance(raw, list) else []
            error_text = (
                "\n".join(messages)
                or _string(body.get("error"))
                or _string(body.get("reason"))
                or _string(body.get("stderr"))
                or (body_text if status == "error" else None)
            )
        if klass == "shell":
            if case == "success":
                exit_code = _int(body.get("exitCode"), default=0)
            elif case == "failure":
                exit_code = _int(body.get("exitCode"))
            cwd = _string(body.get("workingDirectory"))
    return ToolCall(
        span=span_id(session_id, key, index),
        tool_name=call.tool_name,
        mcp_server=call.mcp_server,
        status=status,
        arguments=call.arguments,
        result=body_text,
        result_size=len(body_text) if body_text is not None else None,
        error_text=error_text,
        truncated=False,
        provider_call_id=call.call_id,
        duration_ms=duration,
        denial_kind=denial,
        working_directory=cwd,
        tool_class=klass,
        canonical_arguments=canonical_arguments(
            SOURCE, call.tool_name, call.mcp_server, call.arguments
        ),
        exit_code=exit_code,
    )


def _status(case: str | None, body: Mapping[str, object], mcp: bool) -> tuple[Status, str | None]:
    """A call's status and refusal kind from Cursor's typed outcome alone."""
    if case is None:
        return "unknown", None
    if case == "success":
        return ("error" if mcp and body.get("isError") is True else "ok"), None
    if case == "failure":
        return ("interrupted" if body.get("aborted") is True else "error"), None
    if case == "rejected":
        reason = _string(body.get("reason")) or ""
        return "rejected", next(
            (kind for pattern, kind in _DENIALS if pattern.search(reason)), None
        )
    if case == "permissionDenied":
        return "rejected", "permission-rule"
    if case in _ERROR_CASES:
        return "error", None
    return "unknown", None


def _responses(usage: tuple[Usage, ...], sidechain: bool) -> tuple[ModelResponse, ...]:
    """One response for each usage line, input taken out of the cache counts
    (ADR-0038), in the order the log wrote them. A turn's totals are one
    response, their input already net of the cache."""
    responses: list[ModelResponse] = []
    seen: set[str] = set()
    for line in usage:
        response_id = line.response_id
        if response_id in seen:
            continue
        seen.add(response_id)
        cached = (line.cache_read_tokens or 0) + (line.cache_write_tokens or 0)
        uncached = line.input_tokens
        if line.input_tokens is not None and not line.turn_total:
            uncached = max(line.input_tokens - cached, 0)
        responses.append(
            ModelResponse(
                response_id=response_id,
                request_id=line.request_id,
                model=line.model,
                occurred_at=line.occurred_at,
                final=True,
                is_sidechain=sidechain,
                usage=TokenUsage(
                    input_tokens=uncached,
                    cache_read_input_tokens=line.cache_read_tokens,
                    cache_creation_input_tokens=line.cache_write_tokens or None,
                    output_tokens=line.output_tokens,
                ),
                context_window=line.max_tokens,
            )
        )
    return tuple(responses)


def _link(turns: list[Turn], segments: list[list[int]], placement: Placement) -> list[Turn]:
    """Each assistant turn's response, where its request's usage lines and its
    assistant turns agree in number; nothing where they do not."""
    by_request: dict[str | None, list[Usage]] = {}
    for line in placement.usage:
        by_request.setdefault(line.request_id, []).append(line)
    linked = list(turns)
    for segment, request in zip(segments, placement.requests, strict=False):
        lines = by_request.get(request, [])
        if not segment or len(lines) != len(segment):
            continue
        for index, line in zip(segment, lines, strict=True):
            turn = linked[index]
            linked[index] = Turn(
                position=turn.position,
                key=turn.key,
                role=turn.role,
                text=turn.text,
                is_sidechain=turn.is_sidechain,
                tool_calls=turn.tool_calls,
                permission_mode=turn.permission_mode,
                occurred_at=turn.occurred_at,
                response_id=line.response_id,
            )
    return linked


def _with_errors(
    turns: list[Turn], rows: list[str], outcomes: tuple[Outcome, ...], sidechain: bool
) -> list[Turn]:
    """A turn with role `error` for each turn that ended in a client error: the
    transcript's own text, the log's time paired by order. With no transcript
    row, the log's text."""
    if rows:
        texts: list[str] = list(rows)
        aligned = outcomes
    else:
        with_text = [(o.text, o) for o in outcomes if o.text]
        texts = [text for text, _ in with_text]
        aligned = tuple(o for _, o in with_text)
    if not texts:
        return turns
    paired = len(rows) == len(outcomes) or not rows
    result = list(turns)
    for index, text in enumerate(texts):
        at = aligned[index].occurred_at if paired and index < len(aligned) else None
        error = Turn(
            position=0,
            key=f"e{index + 1}",
            role="error",
            text=text,
            is_sidechain=sidechain,
            tool_calls=(),
            occurred_at=at,
        )
        where = len(result)
        if at is not None:
            later = [
                i for i, t in enumerate(result) if t.occurred_at is not None and t.occurred_at > at
            ]
            if later:
                where = later[0]
        result.insert(where, error)
    return [
        Turn(
            position=position,
            key=t.key,
            role=t.role,
            text=t.text,
            is_sidechain=t.is_sidechain,
            tool_calls=t.tool_calls,
            permission_mode=t.permission_mode,
            occurred_at=t.occurred_at,
            response_id=t.response_id,
        )
        for position, t in enumerate(result)
    ]


# --- the reader ----------------------------------------------------------------


class CursorReader:
    """Reads the Cursor CLI's chat stores, with usage and MCP failures placed
    from its debug log, and the desktop app's conversations from its state
    database, with usage placed from its structured log (ADR-0039)."""

    agent_kind = "cursor"

    def __init__(
        self,
        root: Path | None = None,
        *,
        data_root: Path | None = None,
        log_root: Path | None = None,
        user_data_root: Path | None = None,
    ) -> None:
        self._root = (root if root is not None else default_root()).absolute()
        # Cursor keeps both trees in `~/.cursor` unless relocated, so a root a
        # caller names is the data root too unless it names that as well.
        if data_root is None:
            data_root = root if root is not None else default_data_root()
        self._data_root = data_root.absolute()
        self._log_root = (log_root if log_root is not None else default_log_root()).absolute()
        self._logs = CursorLogs(self._log_root)
        self._user_data = (
            user_data_root if user_data_root is not None else default_user_data_root()
        ).absolute()
        self._ide_logs = CursorIdeLogs(self._user_data / IDE_LOGS)
        self._composers: dict[str, tuple[str, _Composer | None, str | None]] = {}
        self._digests: dict[str, str] = {}

    def locations(self) -> tuple[Path, ...]:
        """Where the stores, the transcripts and the debug logs live, and the
        desktop app's database and structured logs."""
        return (
            self._root / CHATS,
            self._data_root / PROJECTS,
            self._log_root,
            self._user_data / GLOBAL_STORAGE,
            self._user_data / IDE_LOGS,
        )

    def collect(self, window: Window) -> tuple[list[Session], list[ReaderFailure]]:
        stores, failures = self._discover()
        root_state = self._refresh_logs(failures)
        read = [self._read(path, root_state, failures) for path in stores]
        sessions: list[Session] = []
        for session in [*read, *self._desktop_sessions(failures)]:
            if session is None:
                continue
            if window.since is not None and (
                session.last_activity_at is None or session.last_activity_at < window.since
            ):
                continue
            sessions.append(session)
        return sessions, failures

    def collect_file(self, path: Path) -> tuple[list[Session], list[ReaderFailure]]:
        """Rebuild the sessions one changed file names, and return those that
        differ from what was last returned for them."""
        path = path.absolute()
        failures: list[ReaderFailure] = []
        chats = self._root / CHATS
        targets: list[Path] = []
        if (path.parent == self._user_data / GLOBAL_STORAGE and path.name in _STATE_FILES) or (
            path.is_relative_to(self._user_data / IDE_LOGS) and path.name == IDE_LOG_NAME
        ):
            return self._changed(self._desktop_sessions(failures)), failures
        if path.is_relative_to(chats) and path.name in _STORE_FILES:
            targets.append(path.parent / STORE)
            root_state = self._refresh_logs(failures)
        elif path.is_relative_to(self._log_root) and path.name.endswith(".log"):
            before_conversations, before_calls = self._logs.names(path)
            root_state = self._refresh_logs(failures)
            conversations, calls = self._logs.names(path)
            conversations |= before_conversations
            calls |= before_calls
            stores, discovery = self._discover()
            failures.extend(discovery)
            for store in stores:
                info = _sub_agent_call(store)
                if store.parent.name in conversations or (info is not None and info in calls):
                    targets.append(store)
        elif (
            path.is_relative_to(self._data_root / PROJECTS)
            and path.suffix == ".jsonl"
            and TRANSCRIPTS in path.parts
        ):
            root_state = self._refresh_logs(failures)
            stores, discovery = self._discover()
            failures.extend(discovery)
            targets.extend(store for store in stores if store.parent.name == path.stem)
            # The desktop app writes its transcripts to the same place.
            desktop = f"{SOURCE}:{path.stem}"
            return self._changed(
                [self._read(store, root_state, failures) for store in targets]
                + [s for s in self._desktop_sessions(failures) if s.session_id == desktop]
            ), failures
        elif (
            path.is_relative_to(self._data_root / PROJECTS)
            and path.name == SERVER_METADATA
            and path.parent.parent.name == MCPS
        ):
            # An MCP identifier's name can change for any project, and a
            # store names no project directory to narrow this to.
            root_state = self._refresh_logs(failures)
            stores, discovery = self._discover()
            failures.extend(discovery)
            return self._changed(
                [self._read(store, root_state, failures) for store in stores]
                + self._desktop_sessions(failures)
            ), failures
        else:
            return [], []
        return self._changed(
            [self._read(store, root_state, failures) for store in targets]
        ), failures

    def _changed(self, sessions: Sequence[Session | None]) -> list[Session]:
        """The sessions whose digest differs from the one last returned."""
        changed: list[Session] = []
        for session in sessions:
            if session is None:
                continue
            digest = hashlib.sha256(repr(session).encode("utf-8")).hexdigest()
            if self._digests.get(session.session_id) == digest:
                continue
            self._digests[session.session_id] = digest
            changed.append(session)
        return changed

    def _desktop_sessions(self, failures: list[ReaderFailure]) -> list[Session]:
        """Every desktop conversation, decoded from its database and placed on
        its structured logs. No database is no sessions and no failure."""
        failures.extend(self._failure(message) for message in self._ide_logs.refresh())
        path = self._user_data / GLOBAL_STORAGE / STATE_DB
        try:
            info = path.stat()
        except FileNotFoundError:
            self._composers = {}
            return []
        except OSError as error:
            failures.append(self._failure(f"{path}: {error}"))
            return []
        if not stat.S_ISREG(info.st_mode):
            failures.append(self._failure(f"{path}: not a regular file"))
            return []
        try:
            self._composers, warnings = _read_desktop(path, self._composers)
        except sqlite3.OperationalError as error:
            text = str(error)
            if "locked" in text or "busy" in text:
                failures.append(self._failure(f"{path}: database busy; not read this pass"))
            elif "no such table" in text:
                failures.append(self._failure(f"{path}: unsupported schema ({text})"))
            else:
                failures.append(self._failure(f"{path}: {text}"))
            return []
        except (sqlite3.Error, OSError) as error:
            failures.append(self._failure(f"{path}: {error}"))
            return []
        failures.extend(self._failure(f"{path}: {warning}") for warning in warnings)
        sessions: list[Session] = []
        servers: dict[str, dict[str, str]] = {}
        for composer_id, (_, composer, _) in sorted(self._composers.items()):
            if composer is None:
                continue
            failures.extend(
                self._failure(f"{path}: conversation {composer_id}: {warning}")
                for warning in composer.store.warnings
            )
            parent = self._composers.get(composer.parent or "", ("", None, None))[1]
            parent_cwd = (
                _string(parent.meta_json.get("cwd")) or parent.store.workspace
                if parent is not None
                else None
            )
            cwd = _string(composer.meta_json.get("cwd")) or composer.store.workspace or parent_cwd
            placement = dataclasses.replace(
                self._ide_logs.place(composer_id, composer.call), surface=composer.entrypoint
            )
            rows = (
                self._transcript_errors(composer_id, cwd, failures) if composer.call is None else []
            )
            if cwd is not None and cwd not in servers:
                servers[cwd] = self._server_names(cwd, failures)
            names = servers.get(cwd or "", {})
            sessions.append(
                _session(composer.store, composer.meta_json, cwd, placement, rows, names)
            )
        return sessions

    def _refresh_logs(self, failures: list[ReaderFailure]) -> str | None:
        state, messages = self._logs.refresh()
        failures.extend(self._failure(message) for message in messages)
        return state

    def _read(
        self, path: Path, root_state: str | None, failures: list[ReaderFailure]
    ) -> Session | None:
        try:
            info = path.stat()
        except FileNotFoundError:
            return None
        except OSError as error:
            failures.append(self._failure(f"{path}: {error}"))
            return None
        if not stat.S_ISREG(info.st_mode):
            failures.append(self._failure(f"{path}: not a regular file"))
            return None
        try:
            store = _read_store(path)
        except sqlite3.OperationalError as error:
            text = str(error)
            if "locked" in text or "busy" in text:
                failures.append(self._failure(f"{path}: store busy; not read this pass"))
            elif "no such table" in text:
                failures.append(self._failure(f"{path}: unsupported schema ({text})"))
            else:
                failures.append(self._failure(f"{path}: {text}"))
            return None
        except (sqlite3.Error, OSError, ValueError, TypeError) as error:
            failures.append(self._failure(f"{path}: {error}"))
            return None
        if store is None:
            return None
        failures.extend(self._failure(f"{path}: {warning}") for warning in store.warnings)
        meta_json = self._meta_json(path.parent / META_JSON, failures)
        info = store.meta.get("subagentInfo")
        parent_cwd: str | None = None
        call: str | None = None
        if isinstance(info, dict):
            call = _string(info.get("toolCallId"))
            parent = _string(info.get("parentAgentId"))
            if parent is not None:
                # A sub-agent's store has no `meta.json`; it runs where its
                # parent does, whose store is its sibling.
                parent_meta = self._meta_json(path.parent.parent / parent / META_JSON, failures)
                parent_cwd = _string(parent_meta.get("cwd"))
        placement = self._logs.place(store.agent_id, call, root_state)
        if call is not None and isinstance(info, dict):
            # A sub-agent runs in its parent's process and surface.
            parent_root = _string(info.get("parentAgentId"))
            if parent_root is not None and placement.surface is None:
                surface = self._logs.place(parent_root, None, root_state).surface
                placement = dataclasses.replace(placement, surface=surface)
        cwd = _string(meta_json.get("cwd")) or store.workspace or parent_cwd
        rows = self._transcript_errors(store.agent_id, cwd, failures) if call is None else []
        names = self._server_names(cwd, failures) if cwd is not None else {}
        return _session(store, meta_json, cwd, placement, rows, names)

    def _meta_json(self, path: Path, failures: list[ReaderFailure]) -> dict[str, object]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, UnicodeError, RecursionError) as error:
            failures.append(self._failure(f"{path}: {error}; its cwd and title are not read"))
            return {}
        return document if isinstance(document, dict) else {}

    def _transcript_errors(
        self, agent_id: str, cwd: str | None, failures: list[ReaderFailure]
    ) -> list[str]:
        """The client's own words for each turn that ended in an error, from the
        transcript's `turn_ended` rows."""
        if cwd is None:
            return []
        path = self._project(cwd) / TRANSCRIPTS / agent_id / f"{agent_id}.jsonl"
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeError) as error:
            failures.append(self._failure(f"{path}: {error}; client errors are not read"))
            return []
        rows: list[str] = []
        for line in raw.splitlines():
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if (
                isinstance(row, dict)
                and row.get("type") == "turn_ended"
                and row.get("status") == "error"
                and isinstance(row.get("error"), str)
                and row["error"]
            ):
                rows.append(row["error"])
        return rows

    def _project(self, cwd: str) -> Path:
        return self._data_root / PROJECTS / re.sub(r"[^A-Za-z0-9]+", "-", cwd).strip("-")

    def _server_names(self, cwd: str, failures: list[ReaderFailure]) -> dict[str, str]:
        """Each MCP server identifier the client recorded for the project at
        `cwd`, mapped to the name its configuration gives it."""
        directory = self._project(cwd) / MCPS
        try:
            entries = sorted(os.listdir(directory))
        except FileNotFoundError:
            return {}
        except OSError as error:
            failures.append(self._failure(f"{directory}: {error}; servers are named by identifier"))
            return {}
        names: dict[str, str] = {}
        for entry in entries:
            path = directory / entry / SERVER_METADATA
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, NotADirectoryError):
                continue
            except (OSError, ValueError, UnicodeError, RecursionError) as error:
                failures.append(
                    self._failure(f"{path}: {error}; its server is named by its identifier")
                )
                continue
            if not isinstance(document, dict):
                continue
            identifier = _string(document.get("serverIdentifier"))
            name = _string(document.get("serverName"))
            if identifier is not None and name is not None:
                names[identifier] = name
        return names

    def _discover(self) -> tuple[list[Path], list[ReaderFailure]]:
        """Every `store.db` two levels under `chats/`, and every directory that
        could not be listed (`os.walk(onerror=...)`, never `glob()`)."""
        chats = self._root / CHATS
        stores: list[Path] = []
        failures: list[ReaderFailure] = []
        errors: list[OSError] = []
        for dirpath, dirnames, filenames in os.walk(chats, onerror=errors.append):
            depth = len(Path(dirpath).relative_to(chats).parts)
            if depth >= 2:
                dirnames[:] = []
                if STORE in filenames:
                    stores.append(Path(dirpath) / STORE)
        for error in errors:
            if isinstance(error, FileNotFoundError) and Path(str(error.filename)) == chats:
                continue
            failures.append(self._failure(f"{error.filename}: {error.strerror or error}"))
        return sorted(stores), failures

    def _failure(self, message: str) -> ReaderFailure:
        return ReaderFailure(agent_kind=self.agent_kind, message=message)


def _sub_agent_call(store: Path) -> str | None:
    """The call that started a sub-agent's store, read from its meta row."""
    try:
        connection = sqlite3.connect(
            f"{store.resolve().as_uri()}?mode=ro", uri=True, timeout=_BUSY_SECONDS
        )
    except sqlite3.Error:
        return None
    try:
        row = connection.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
        meta = json.loads(bytes.fromhex(row[0]).decode("utf-8")) if row else None
    except (sqlite3.Error, ValueError, UnicodeError, TypeError, RecursionError):
        return None
    finally:
        connection.close()
    info = meta.get("subagentInfo") if isinstance(meta, dict) else None
    return _string(info.get("toolCallId")) if isinstance(info, dict) else None


# --- small helpers -------------------------------------------------------------


def _parts(content: object) -> list[Mapping[str, object]]:
    return [p for p in content if isinstance(p, Mapping)] if isinstance(content, list) else []


def _joined(content: object) -> str:
    if isinstance(content, str):
        return content
    texts = [p.get("text") for p in _parts(content) if p.get("type") == "text"]
    return "\n".join(t for t in texts if isinstance(t, str)).strip()


def _body(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, sort_keys=True)
    except (TypeError, ValueError, RecursionError):
        return None


def _path_of(uri: str | None) -> str | None:
    if uri is None:
        return None
    parsed = urlparse(uri)
    return unquote(parsed.path) if parsed.scheme == "file" else uri


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object, default: int | None = None) -> int | None:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
        return int(value)
    return default


def _user() -> str | None:
    try:
        return getpass.getuser() or None
    except (KeyError, OSError):
        return None
