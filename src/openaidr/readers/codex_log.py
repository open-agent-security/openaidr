"""Places Codex's MCP connections on a thread from its log database.

A rollout records MCP calls, never connections: Codex keeps its MCP startup
events out of the rollout. It logs them, at its default level, to
`logs_<n>.sqlite` under its home, in a `logs` table it indexes by thread and,
for rows that name no thread, by process. Only the terminal UI and the
app-server open that database; `codex exec` writes no rows at all
(`docs/specs/codex.md`).

**A startup line names a server, not a thread.** Codex starts a thread's MCP
servers inside the request that creates the thread, and logs that request's
span on every line it writes while doing so. Another line in the same request
does name the thread. So a line is placed by the request it was logged in:
never by time, which a server that fails slowly would cross. A line logged in
no request is placed only where its process served one thread.

Read-only, and only the rows of the targets below. No other log body is read.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from openaidr.model import MCPConnection, MCPLogState

#: The log targets whose lines say how a server's startup ended.
_STARTED = "rmcp::service"
_FAILED = "codex_mcp::rmcp_client"
_TARGETS = (_STARTED, _FAILED)

_DATABASE = re.compile(r"^logs_(\d+)\.sqlite$")
#: The span a line was logged in while its thread was being created: the
#: app-server request, by connection and request id.
_REQUEST = re.compile(
    r"^app_server\.request\{(?=[^}]*\brpc\.request_id=(\S+))(?=[^}]*\bapp_server\.connection_id=(\S+))"
)
_SERVER_SPAN = re.compile(r"\bnew\{server_name=([^}\s]+)\}")
_INITIALIZED = re.compile(r"Service initialized as client")
_ADVERTISED = re.compile(
    r'server_info: Some\(Implementation \{ name: "(?P<name>[^"]*)"[^}]*?\bversion: "(?P<version>[^"]*)"'
)
_STARTUP_FAILED = re.compile(
    r'MCP server startup failed server_name="(?P<server>[^"]+)" error=(?P<error>.*)$', re.DOTALL
)
_HTTP = re.compile(
    r"StreamableHttpClientAdapter|streamable HTTP|http/request failed", re.IGNORECASE
)

#: How a startup failure maps to a category, first match wins, on Codex's own
#: words. The specific above the general, as for the other kinds.
_FAILURE_CATEGORIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "policy",
        re.compile(r"blocked by policy|disabled by (?:your )?organi[sz]ation", re.IGNORECASE),
    ),
    ("spawn", re.compile(r"No such file or directory|os error 2\b|failed to spawn", re.IGNORECASE)),
    (
        "auth",
        re.compile(r"\b40[13]\b|unauthori[sz]ed|forbidden|oauth|auth required", re.IGNORECASE),
    ),
    ("timeout", re.compile(r"timed out|timeout", re.IGNORECASE)),
    (
        "network",
        re.compile(
            r"error sending request|connection refused|tcp connect|dns error|failed to lookup"
            r"|network is unreachable",
            re.IGNORECASE,
        ),
    ),
    ("http_status", re.compile(r"HTTP status|\b[45]\d\d\b", re.IGNORECASE)),
    ("protocol", re.compile(r"handshaking with MCP server failed|protocol", re.IGNORECASE)),
)


@dataclass(frozen=True)
class Placement:
    """What the log database says about one thread's MCP connections."""

    connections: tuple[MCPConnection, ...] = ()
    state: MCPLogState = "not_attempted"


@dataclass(frozen=True)
class _Line:
    at: datetime
    server: str
    started: bool
    advertised: tuple[str, str] | None
    error: str | None
    request: tuple[str, str] | None


class CodexLogs:
    """One read-only connection to Codex's log database, for one pass."""

    def __init__(self, path: Path | None, state: MCPLogState, failure: str | None = None) -> None:
        self._path = path
        self._state: MCPLogState = state
        self.failure = failure
        self._db: sqlite3.Connection | None = None

    @classmethod
    def open(cls, root: Path) -> CodexLogs:
        """The newest `logs_<n>.sqlite` under `root`; a state saying why there is none."""
        try:
            names = os.listdir(root)
        except FileNotFoundError:
            return cls(None, "no_log_root")
        except OSError as error:
            return cls(None, "log_root_unreadable", f"{root}: {error}")
        versions = [(int(m.group(1)), name) for name in names if (m := _DATABASE.match(name))]
        if not versions:
            return cls(None, "no_log_root")
        path = root / max(versions)[1]
        logs = cls(path, "applied")
        try:
            logs._db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2.0)
            logs._db.execute("SELECT 1 FROM logs LIMIT 1").fetchall()
        except sqlite3.Error as error:
            logs.close()
            return cls(None, "log_root_unreadable", f"{path}: {error}")
        return logs

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def place(self, thread_id: str) -> tuple[Placement, str | None]:
        """The thread's connections, and a failure message when a read broke part-way."""
        if self._db is None:
            return Placement(state=self._state), None
        try:
            return self._place(self._db, thread_id), None
        except sqlite3.Error as error:
            return Placement(state="log_discovery_incomplete"), f"{self._path}: {error}"

    def _place(self, db: sqlite3.Connection, thread_id: str) -> Placement:
        processes = [
            process
            for (process,) in db.execute(
                "SELECT DISTINCT process_uuid FROM logs WHERE thread_id = ?", (thread_id,)
            )
            if isinstance(process, str)
        ]
        if not processes:
            return Placement(state="no_log_for_session")
        mine: list[_Line] = []
        ambiguous = False
        for process in processes:
            lines = self._startup_lines(db, process)
            requests = {line.request for line in lines if line.request is not None}
            owned = {request for request in requests if self._names(db, thread_id, request)}
            mine.extend(
                line for line in lines if line.request is not None and line.request in owned
            )
            loose = [line for line in lines if line.request is None]
            if loose:
                if self._threads_served(db, process) == 1:
                    mine.extend(loose)
                else:
                    ambiguous = True
        if ambiguous:
            return Placement(state="placement_ambiguous")
        return Placement(connections=_connections(mine), state="applied")

    def _startup_lines(self, db: sqlite3.Connection, process: str) -> list[_Line]:
        rows = db.execute(
            "SELECT ts, ts_nanos, target, feedback_log_body FROM logs"
            " WHERE process_uuid = ? AND thread_id IS NULL AND target IN (?, ?)"
            " ORDER BY ts, ts_nanos, id",
            (process, *_TARGETS),
        )
        lines: list[_Line] = []
        for seconds, nanos, target, body in rows:
            if not isinstance(body, str) or not isinstance(seconds, int):
                continue
            line = _line(seconds, nanos if isinstance(nanos, int) else 0, target, body)
            if line is not None:
                lines.append(line)
        return lines

    def _names(self, db: sqlite3.Connection, thread_id: str, request: tuple[str, str]) -> bool:
        """Whether a line the thread logged sits in the same request span."""
        request_id, connection = request
        pattern = f"app_server.request{{%rpc.request_id={_like(request_id)} %"
        for (body,) in db.execute(
            "SELECT feedback_log_body FROM logs WHERE thread_id = ? AND feedback_log_body LIKE ? ESCAPE '\\'",
            (thread_id, pattern),
        ):
            if isinstance(body, str) and _request(body) == (request_id, connection):
                return True
        return False

    def _threads_served(self, db: sqlite3.Connection, process: str) -> int:
        (count,) = db.execute(
            "SELECT COUNT(DISTINCT thread_id) FROM logs WHERE process_uuid = ? AND thread_id IS NOT NULL",
            (process,),
        ).fetchone()
        return count if isinstance(count, int) else 0


def _line(seconds: int, nanos: int, target: str, body: str) -> _Line | None:
    try:
        at = datetime.fromtimestamp(seconds, UTC).replace(microsecond=nanos // 1000)
    except (OSError, OverflowError, ValueError):
        # An out-of-range ts or ts_nanos; this row's instant is unrepresentable,
        # so the row is skipped like any other it cannot make sense of.
        return None
    request = _request(body)
    if target == _STARTED and _INITIALIZED.search(body):
        span = _SERVER_SPAN.findall(body)
        if not span:
            return None
        advertised = _ADVERTISED.search(body)
        return _Line(
            at=at,
            server=span[-1],
            started=True,
            advertised=(advertised.group("name"), advertised.group("version"))
            if advertised
            else None,
            error=None,
            request=request,
        )
    if target == _FAILED:
        failed = _STARTUP_FAILED.search(body)
        if failed is None:
            return None
        return _Line(
            at=at,
            server=failed.group("server"),
            started=False,
            advertised=None,
            error=failed.group("error").strip(),
            request=request,
        )
    return None


def _request(body: str) -> tuple[str, str] | None:
    found = _REQUEST.match(body)
    return (found.group(1), found.group(2)) if found else None


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _connections(lines: list[_Line]) -> tuple[MCPConnection, ...]:
    """One connection per server, in the order servers first appear. Lines that
    disagree about a server leave `connected` unknown."""
    by_server: dict[str, list[_Line]] = {}
    for line in lines:
        by_server.setdefault(line.server, []).append(line)
    connections: list[MCPConnection] = []
    for server, attempts in by_server.items():
        outcomes = {line.started for line in attempts}
        connected = outcomes.pop() if len(outcomes) == 1 else None
        failure = next((line.error for line in attempts if line.error is not None), None)
        advertised = next((line.advertised for line in attempts if line.advertised), None)
        connections.append(
            MCPConnection(
                server=server,
                transport="streamable-http"
                if failure is not None and _HTTP.search(failure)
                else None,
                endpoint=None,
                advertised_name=advertised[0] if advertised else None,
                advertised_version=advertised[1] if advertised else None,
                connected=connected,
                failure_category=_category(failure) if connected is False and failure else None,
                failure_detail=failure if connected is False else None,
                duration_ms=None,
            )
        )
    return tuple(connections)


def _category(reason: str) -> str:
    for category, pattern in _FAILURE_CATEGORIES:
        if pattern.search(reason):
            return category
    return "unknown"
