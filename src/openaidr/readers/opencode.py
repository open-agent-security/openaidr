"""Reads OpenCode sessions from its SQLite database.

OpenCode 1.x keeps every session in one database, `<data dir>/opencode.db`: a
`session` row for each session, a sub-agent's included, a `message` row for each
message, and a `part` row for each part of a message. Each `data` column is the
message's or part's own JSON. This reader opens the database read-only, in one
transaction for each pass (`docs/specs/opencode.md`, ADR-0045).

**A message is a turn, keyed by its own id.** OpenCode gives a message and a
part an id that sorts by time and never changes, and updates a part in place as
its call runs. A call's span is its tool part's position among the message's
tool parts, so a later part cannot move it (ADR-0001).

**An outcome is OpenCode's.** A tool part's `state.status` is its status. Only
OpenCode's own fixed refusal words make an error a refusal.

**An MCP tool's server comes from configuration.** OpenCode names an MCP tool
`<server>_<tool>`, and both may hold `_`, so the name alone cannot be split.
The reader splits it against the server names OpenCode's configuration files
hold, and leaves it unsplit when none, or more than one, fits (ADR-0046).
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

SOURCE = "opencode"
DATABASE = "opencode.db"
_TABLES = frozenset({"session", "message", "part"})
_BUSY_SECONDS = 2.0
#: How far back a changed database is read again: a row can commit after a
#: later row's time was taken. Sessions read twice are returned once.
_MARGIN = timedelta(seconds=5)

#: OpenCode's own words, `PermissionV1` in `packages/core/src/v1/permission.ts`.
_REJECTED = "The user rejected permission to use this specific tool call"
_DENIED = "The user has specified a rule which prevents you from using this specific tool call"
_ABORTED = "Tool execution aborted"
#: The placeholder title OpenCode gives a session it has not named yet.
_PLACEHOLDER = re.compile(
    r"^(New session - |Child session - )\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)
#: OpenCode's `McpCatalog.sanitize`.
_SANITIZE = re.compile(r"[^a-zA-Z0-9_-]")
_CONFIG_FILES = ("config.json", "opencode.json", "opencode.jsonc")
_PROJECT_FILES = ("opencode.json", "opencode.jsonc")
_ABORT_ERROR = "MessageAbortedError"


def default_root() -> Path:
    """OpenCode's data directory: `$XDG_DATA_HOME/opencode`, else
    `~/.local/share/opencode`, on Linux and macOS alike."""
    return _xdg("XDG_DATA_HOME", Path(".local") / "share") / "opencode"


def default_config_root() -> Path:
    """OpenCode's global configuration directory."""
    return _xdg("XDG_CONFIG_HOME", Path(".config")) / "opencode"


def _xdg(variable: str, fallback: Path) -> Path:
    configured = os.environ.get(variable)
    if configured and Path(configured).is_absolute():
        return Path(configured)
    return Path.home() / fallback


# --- one database ----------------------------------------------------------------


@dataclass(frozen=True)
class _Row:
    session: dict[str, object]
    messages: list[tuple[str, int, dict[str, object]]]
    #: id, time_created, time_updated, data. A tool part's `data` is updated
    #: in place as its call runs, bumping `time_updated` alone (ADR-0001).
    parts: dict[str, list[tuple[str, int, int, dict[str, object]]]]


def _read(
    path: Path, changed_since: int | None, failures: list[ReaderFailure]
) -> tuple[list[_Row], dict[str, str], int]:
    """The sessions changed since `changed_since` (ms), every session's root,
    and the newest time any row carries. Raises `sqlite3.Error` or `OSError`."""
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=_BUSY_SECONDS
    )
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        missing = _TABLES - tables
        if missing:
            raise ValueError(f"unsupported schema: no {', '.join(sorted(missing))} table")
        parents = {
            row["id"]: row["parent_id"]
            for row in connection.execute("SELECT id, parent_id FROM session")
        }
        newest = max(
            (
                value
                for (value,) in connection.execute(
                    "SELECT MAX(time_updated) FROM session UNION ALL "
                    "SELECT MAX(time_updated) FROM message UNION ALL "
                    "SELECT MAX(time_updated) FROM part"
                )
                if isinstance(value, int)
            ),
            default=0,
        )
        if changed_since is None:
            chosen = list(parents)
        else:
            chosen = sorted(
                {
                    value
                    for (value,) in connection.execute(
                        "SELECT id FROM session WHERE time_updated >= ?1 UNION "
                        "SELECT session_id FROM message WHERE time_updated >= ?1 UNION "
                        "SELECT session_id FROM part WHERE time_updated >= ?1",
                        (changed_since,),
                    )
                }
            )
        rows: list[_Row] = []
        for session_id in chosen:
            session = connection.execute(
                "SELECT * FROM session WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                continue
            messages = [
                (row["id"], row["time_created"], data)
                for row in connection.execute(
                    "SELECT id, time_created, data FROM message WHERE session_id = ? "
                    "ORDER BY time_created, id",
                    (session_id,),
                )
                if (data := _json(row["data"], f"message {row['id']}", path, failures)) is not None
            ]
            parts: dict[str, list[tuple[str, int, int, dict[str, object]]]] = {}
            for row in connection.execute(
                "SELECT id, message_id, time_created, time_updated, data FROM part "
                "WHERE session_id = ? ORDER BY message_id, id",
                (session_id,),
            ):
                data = _json(row["data"], f"part {row['id']}", path, failures)
                if data is not None:
                    parts.setdefault(row["message_id"], []).append(
                        (row["id"], row["time_created"], row["time_updated"], data)
                    )
            rows.append(_Row(dict(session), messages, parts))
        roots = {session_id: _root(session_id, parents) for session_id in parents}
        return rows, roots, newest
    finally:
        connection.close()


def _root(session_id: str, parents: Mapping[str, object]) -> str:
    """The session at the top of `session_id`'s chain of parents."""
    seen = {session_id}
    current = session_id
    while isinstance(parent := parents.get(current), str) and parent not in seen:
        seen.add(parent)
        current = parent
    return current


def _json(
    value: object, what: str, path: Path, failures: list[ReaderFailure]
) -> dict[str, object] | None:
    try:
        parsed = json.loads(value) if isinstance(value, str | bytes) else None
    except (ValueError, RecursionError):
        parsed = None
    if not isinstance(parsed, dict):
        failures.append(ReaderFailure(SOURCE, f"{path}: {what} is not a JSON object; skipped"))
        return None
    return parsed


# --- the session -------------------------------------------------------------------


def _session(row: _Row, root: str, servers: Mapping[str, str]) -> Session:
    native = str(row.session["id"])
    sidechain = root != native
    session_id = f"{SOURCE}:{native}" if not sidechain else f"{SOURCE}:{root}:{native}"
    directory = _string(row.session.get("directory"))
    started = _ms(row.session.get("time_created"))
    last = _later(started, _ms(row.session.get("time_updated")))
    turns: list[Turn] = []
    context: list[ContextItem] = []
    compactions: list[Compaction] = []
    responses: list[ModelResponse] = []
    initial: str | None = None
    for message_id, created, message in row.messages:
        parts = row.parts.get(message_id, [])
        role = message.get("role")
        time = _mapping(message.get("time"))
        at = _ms(time.get("created")) or _ms(created)
        last = _later(last, at)
        last = _later(last, _ms(time.get("completed")))
        for _, part_created, part_updated, _ in parts:
            last = _later(last, _ms(part_created))
            last = _later(last, _ms(part_updated))
        texts: list[str] = []
        for _, _, _, part in parts:
            kind = part.get("type")
            text = _string(part.get("text"))
            if kind == "text" and text is not None and not part.get("ignored"):
                if part.get("synthetic"):
                    # Text OpenCode adds to a message itself: a file a person
                    # attached, the result of a command they ran.
                    context.append(
                        ContextItem(
                            span=span_id(session_id, "context", len(context)),
                            source="injected",
                            name=None,
                            text=text,
                            occurred_at=at,
                        )
                    )
                else:
                    texts.append(text)
            elif kind == "compaction":
                compactions.append(
                    Compaction(
                        trigger="auto" if part.get("auto") is True else "manual",
                        pre_tokens=None,
                        dropped_tokens=None,
                        occurred_at=at,
                    )
                )
        text = "\n".join(texts)
        if role == "user":
            if text:
                turns.append(_turn(len(turns), message_id, "user", text, sidechain, (), at))
                if initial is None:
                    initial = text
            continue
        if role != "assistant":
            continue
        tool_parts = [part for _, _, _, part in parts if part.get("type") == "tool"]
        calls = tuple(
            _tool_call(session_id, message_id, index, part, message, servers)
            for index, part in enumerate(tool_parts)
        )
        steps = [
            _response(part_id, part_created, part, message, sidechain)
            for part_id, part_created, _, part in parts
            if part.get("type") == "step-finish"
        ]
        responses.extend(steps)
        turns.append(
            _turn(
                len(turns),
                message_id,
                "assistant",
                text,
                sidechain,
                calls,
                at,
                steps[0].response_id if len(steps) == 1 else None,
            )
        )
        error = _mapping(message.get("error"))
        reason = _string(_mapping(error.get("data")).get("message"))
        if reason is not None and error.get("name") != _ABORT_ERROR:
            turns.append(
                _turn(len(turns), f"{message_id}.error", "error", reason, sidechain, (), at)
            )
    stated = _mapping(_loaded(row.session.get("model")))
    model = next((r.model for r in reversed(responses) if r.model), None) or _string(
        stated.get("id")
    )
    title = _string(row.session.get("title"))
    return Session(
        session_id=session_id,
        agent_kind=map_source(SOURCE),
        source=SOURCE,
        started_at=started,
        model=model,
        working_directory=directory,
        machine=socket.gethostname() or None,
        user=_user(),
        turns=tuple(turns),
        agent_version=_string(row.session.get("version")),
        initial_prompt=initial,
        context_items=tuple(context),
        compactions=tuple(compactions),
        last_activity_at=last,
        responses=tuple(responses),
        generated_title=title if title is not None and not _PLACEHOLDER.match(title) else None,
        compactions_recorded=True,
        subagents_recorded=True,
    )


def _turn(
    position: int,
    key: str,
    role: str,
    text: str,
    sidechain: bool,
    calls: tuple[ToolCall, ...],
    at: datetime | None,
    response_id: str | None = None,
) -> Turn:
    return Turn(
        position=position,
        key=key,
        role=role,
        text=text,
        is_sidechain=sidechain,
        tool_calls=calls,
        occurred_at=at,
        response_id=response_id,
    )


def _tool_call(
    session_id: str,
    message_id: str,
    index: int,
    part: Mapping[str, object],
    message: Mapping[str, object],
    servers: Mapping[str, str],
) -> ToolCall:
    raw = _string(part.get("tool")) or ""
    server, tool = _split(raw, servers)
    state = _mapping(part.get("state"))
    arguments = dict(_mapping(state.get("input")))
    metadata = _mapping(state.get("metadata"))
    status, denial = _status(state, metadata)
    completed = state.get("status") == "completed"
    body = _string(state.get("output")) if completed else _string(state.get("error"))
    timing = _mapping(state.get("time"))
    start, end = _count(timing.get("start")), _count(timing.get("end"))
    shell = server is None and tool == "bash"
    path = _mapping(message.get("path"))
    exit_code = _count(metadata.get("exit")) if shell and status in ("ok", "error") else None
    if completed and exit_code is not None and exit_code != 0:
        # OpenCode completes a command that exited non-zero. Its own exit
        # status says it failed, as Codex's does (`readers.codex`).
        status = "error"
    return ToolCall(
        span=span_id(session_id, message_id, index),
        tool_name=tool,
        mcp_server=server,
        status=status,
        arguments=arguments,
        result=body,
        result_size=len(body) if body is not None else None,
        error_text=body if status == "error" else None,
        truncated=metadata.get("truncated") is True,
        provider_call_id=_string(part.get("callID")),
        duration_ms=end - start if start is not None and end is not None else None,
        denial_kind=denial,
        working_directory=(_string(arguments.get("workdir")) or _string(path.get("cwd")))
        if shell
        else None,
        attributed_skill=_string(arguments.get("name"))
        if server is None and tool == "skill"
        else None,
        tool_class=tool_class(SOURCE, tool, server),
        canonical_arguments=canonical_arguments(SOURCE, tool, server, arguments),
        exit_code=exit_code,
    )


def _status(
    state: Mapping[str, object], metadata: Mapping[str, object]
) -> tuple[Status, str | None]:
    recorded = state.get("status")
    if recorded == "completed":
        return "ok", None
    if recorded in ("pending", "running"):
        return "pending", None
    if recorded != "error":
        return "unknown", None
    error = _string(state.get("error")) or ""
    if error.startswith(_REJECTED):
        return "rejected", "user-rejected"
    if error.startswith(_DENIED):
        return "rejected", "permission-rule"
    if error == _ABORTED or metadata.get("interrupted") is True:
        return "interrupted", None
    return "error", None


def _response(
    part_id: str,
    created: int,
    part: Mapping[str, object],
    message: Mapping[str, object],
    sidechain: bool,
) -> ModelResponse:
    """One `step-finish` part: one request to the provider. OpenCode's `input`
    is uncached input and its `output` leaves out `reasoning`, so output here is
    the two together and reasoning is the part of it spent thinking."""
    tokens = _mapping(part.get("tokens"))
    cache = _mapping(tokens.get("cache"))
    output, reasoning = _count(tokens.get("output")), _count(tokens.get("reasoning"))
    return ModelResponse(
        response_id=part_id,
        request_id=None,
        model=_string(message.get("modelID")),
        occurred_at=_ms(created),
        final=True,
        is_sidechain=sidechain,
        usage=TokenUsage(
            input_tokens=_count(tokens.get("input")),
            cache_read_input_tokens=_count(cache.get("read")),
            cache_creation_input_tokens=_count(cache.get("write")),
            output_tokens=output + (reasoning or 0) if output is not None else None,
            reasoning_tokens=reasoning,
        ),
    )


def _split(raw: str, servers: Mapping[str, str]) -> tuple[str | None, str]:
    """`(server, tool)` for an MCP tool, the server named as it is configured.

    A built-in's name is never split. Otherwise exactly one configured server
    whose written name and `_` start `raw` splits it; with none or several, the
    call is left whole, with no server."""
    if tool_class(SOURCE, raw, None) is not None:
        return None, raw
    matches = [
        (written, name)
        for written, name in servers.items()
        if raw.startswith(f"{written}_") and len(raw) > len(written) + 1
    ]
    if len(matches) != 1:
        return None, raw
    written, name = matches[0]
    return name, raw[len(written) + 1 :]


# --- the reader --------------------------------------------------------------------


class OpenCodeReader:
    """Reads OpenCode's session database."""

    agent_kind = "opencode"

    def __init__(self, root: Path | None = None, *, config_root: Path | None = None) -> None:
        self._root = (root if root is not None else default_root()).absolute()
        self._config = (
            config_root if config_root is not None else default_config_root()
        ).absolute()
        self._newest: dict[Path, int] = {}
        self._digests: dict[str, str] = {}

    def locations(self) -> tuple[Path, ...]:
        extra = self._named_database()
        if extra is not None and extra.parent != self._root:
            return (self._root, extra.parent)
        return (self._root,)

    def collect(self, window: Window) -> tuple[list[Session], list[ReaderFailure]]:
        failures: list[ReaderFailure] = []
        sessions: list[Session] = []
        since = int(window.since.timestamp() * 1000) if window.since is not None else None
        for path in self._databases(failures):
            # A session with a row written since `since` is the set to read;
            # its last activity then decides, as for every kind.
            for session in self._sessions(path, since, failures):
                if window.since is not None and (
                    session.last_activity_at is None or session.last_activity_at < window.since
                ):
                    continue
                sessions.append(session)
        return sessions, failures

    def collect_file(self, path: Path) -> tuple[list[Session], list[ReaderFailure]]:
        """The sessions a change to one database touched, if they differ from
        what was last returned for them."""
        path = path.absolute()
        database = path.with_name(path.name.removesuffix("-wal").removesuffix("-shm"))
        if database != self._named_database() and (
            database.parent != self._root or not _is_database(database.name)
        ):
            return [], []
        failures: list[ReaderFailure] = []
        newest = self._newest.get(database)
        since = newest - int(_MARGIN.total_seconds() * 1000) if newest is not None else None
        changed: list[Session] = []
        for session in self._sessions(database, since, failures):
            digest = hashlib.sha256(repr(session).encode("utf-8")).hexdigest()
            if self._digests.get(session.session_id) == digest:
                continue
            self._digests[session.session_id] = digest
            changed.append(session)
        return changed, failures

    def _sessions(
        self, path: Path, since: int | None, failures: list[ReaderFailure]
    ) -> list[Session]:
        try:
            rows, roots, newest = _read(path, since, failures)
        except FileNotFoundError:
            return []
        except sqlite3.OperationalError as error:
            reason = "database busy" if "locked" in str(error) else str(error)
            failures.append(self._failure(f"{path}: {reason}"))
            return []
        except (sqlite3.Error, OSError, ValueError) as error:
            failures.append(self._failure(f"{path}: {error}"))
            return []
        self._newest[path] = max(newest, self._newest.get(path, 0))
        servers: dict[tuple[str, ...], dict[str, str]] = {}
        sessions: list[Session] = []
        for row in rows:
            directory = _string(row.session.get("directory"))
            files = tuple(str(f) for f in self._config_files(directory))
            if files not in servers:
                servers[files] = self._servers(files, failures)
            sessions.append(_session(row, roots.get(str(row.session["id"]), ""), servers[files]))
        return sessions

    def _databases(self, failures: list[ReaderFailure]) -> list[Path]:
        """`opencode.db`, a non-default channel's `opencode-<channel>.db`, and
        the file `$OPENCODE_DB` names."""
        found: list[Path] = []
        try:
            names = os.listdir(self._root)
        except FileNotFoundError:
            names = []
        except OSError as error:
            failures.append(self._failure(f"{self._root}: {error.strerror or error}"))
            names = []
        found.extend(self._root / name for name in names if _is_database(name))
        extra = self._named_database()
        if extra is not None:
            try:
                extra.stat()
            except FileNotFoundError:
                return sorted(found)
            except OSError as error:
                failures.append(self._failure(f"{extra}: {error.strerror or error}"))
                return sorted(found)
            if extra not in found:
                found.append(extra)
        return sorted(found)

    def _named_database(self) -> Path | None:
        """The file `$OPENCODE_DB` names, absolute or relative to the data
        directory, unless it is `:memory:`."""
        named = os.environ.get("OPENCODE_DB")
        if not named or named == ":memory:":
            return None
        return Path(named) if Path(named).is_absolute() else self._root / named

    def _config_files(self, directory: str | None) -> Iterable[Path]:
        """Every file OpenCode reads its configuration from, for a session in
        `directory`: the global ones, the project's up to the root, and the
        ones the environment names."""
        yield from (self._config / name for name in _CONFIG_FILES)
        named = os.environ.get("OPENCODE_CONFIG")
        if named:
            yield Path(named)
        named_dir = os.environ.get("OPENCODE_CONFIG_DIR")
        if named_dir:
            yield from (Path(named_dir) / name for name in _PROJECT_FILES)
        yield from (Path.home() / ".opencode" / name for name in _PROJECT_FILES)
        if directory:
            current = Path(directory)
            for folder in (current, *current.parents):
                yield from (folder / name for name in _PROJECT_FILES)
                yield from (folder / ".opencode" / name for name in _PROJECT_FILES)

    def _servers(self, files: Iterable[str], failures: list[ReaderFailure]) -> dict[str, str]:
        """Each configured MCP server's written name, mapped to its configured
        name. A written name two configured names share maps to neither."""
        names: dict[str, set[str]] = {}
        for file in files:
            for name in _configured(Path(file), failures):
                names.setdefault(_SANITIZE.sub("_", name), set()).add(name)
        return {written: next(iter(found)) for written, found in names.items() if len(found) == 1}

    def _failure(self, message: str) -> ReaderFailure:
        return ReaderFailure(agent_kind=self.agent_kind, message=message)


def _is_database(name: str) -> bool:
    return name == DATABASE or (name.startswith("opencode-") and name.endswith(".db"))


def _configured(path: Path, failures: list[ReaderFailure]) -> list[str]:
    """The MCP server names one configuration file holds, under `mcp` or, in
    OpenCode's newer layout, `mcp.servers`. None for a missing file."""
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return []
        document = json.loads(_strip_jsonc(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, NotADirectoryError):
        return []
    except (OSError, ValueError, RecursionError) as error:
        failures.append(
            ReaderFailure(
                SOURCE, f"{path}: not read, MCP calls it configures stay unsplit ({error})"
            )
        )
        return []
    servers = document.get("mcp") if isinstance(document, dict) else None
    if not isinstance(servers, dict):
        return []
    nested = servers.get("servers")
    if isinstance(nested, dict) and all(isinstance(v, dict) for v in nested.values()):
        servers = nested
    return [
        name
        for name, value in servers.items()
        if isinstance(name, str) and isinstance(value, dict) and name != "servers"
    ]


def _strip_jsonc(text: str) -> str:
    """JSON from JSONC: comments and trailing commas removed, strings kept."""
    out: list[str] = []
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        if char == '"':
            end = index + 1
            while end < length and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            out.append(text[index : end + 1])
            index = end + 1
        elif text.startswith("//", index):
            newline = text.find("\n", index)
            index = length if newline < 0 else newline
        elif text.startswith("/*", index):
            close = text.find("*/", index + 2)
            index = length if close < 0 else close + 2
        else:
            out.append(char)
            index += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


# --- small helpers -------------------------------------------------------------------


def _loaded(value: object) -> object:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            return None
    return value


def _ms(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _later(current: datetime | None, other: datetime | None) -> datetime | None:
    if other is None:
        return current
    return other if current is None or other > current else current


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
