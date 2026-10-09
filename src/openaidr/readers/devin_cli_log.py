"""Devin CLI's per-process run log: MCP connections, the build, and how it was driven.

The session store records MCP *calls*, never connections. Every Devin process
writes `<data>/logs/devin_<YYYYMMDD-HHMMSS>_<pid>.log`, and at the default level
it logs each server it connects to: the transport, the URL for a remote one, and
whether the connection came up and, if not, why. Logs of finished processes are
gzipped, not deleted.

**A log line names a server and a process, never a session.** What joins the
two is `<data>/session_locks/<session>.lock`, which holds the PID of the process
that last held the session -- the same PID the log's file name carries. That is
an exact join where it exists; where more than one session's lock names the
same process, the log's connection lines cannot be divided between them, and
the session says so (`placement_ambiguous`) rather than taking a share it cannot
show is its own (ADR-0020).
"""

from __future__ import annotations

import gzip
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openaidr.model import MCPConnection, MCPLogState

LOGS = "logs"
LOCKS = "session_locks"

_LOG_NAME = re.compile(r"^devin_\d{8}-\d{6}_(?P<pid>\d+)\.log(?:\.gz)?$")
_LINE = re.compile(
    r"^(?P<at>\d{4}-\d{2}-\d{2}T\S+)\s+(?:TRACE|DEBUG|INFO|WARN|ERROR)\s+(?P<rest>.*)$"
)
_VERSION = re.compile(r"\bversion=(?P<version>\S+)")
_CONNECTING = re.compile(r"Connecting to MCP server '(?P<server>[^']+)'")
_STDIO = re.compile(r"Starting stdio MCP server '(?P<server>[^']+)'")
_HTTP = re.compile(r"Connecting to streamable HTTP MCP server '(?P<server>[^']+)' at: (?P<url>\S+)")
_SSE = re.compile(r"Connecting to legacy SSE MCP server '(?P<server>[^']+)'(?: at: (?P<url>\S+))?")
_CONNECTED = re.compile(r"MCP server '(?P<server>[^']+)' connected successfully")
_FAILED = re.compile(r"MCP server '(?P<server>[^']+)' connection failed: (?P<reason>.*)$")
_DESCRIPTION = re.compile(r"Failed to connect to MCP server '(?P<server>[^']+)' for description")

#: How a failure reason maps to a category, first match wins: the specific
#: above the general, so a refused OAuth handshake over HTTP is `auth`, not
#: `http_status`. Matched on the client's own words and nothing else.
_FAILURE_CATEGORIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "policy",
        re.compile(
            r"blocked by policy|disabled by your organization|organization's allowlist",
            re.IGNORECASE,
        ),
    ),
    (
        "spawn",
        re.compile(r"cannot find binary|No such file or directory|failed to spawn", re.IGNORECASE),
    ),
    (
        "auth",
        re.compile(r"\b40[13]\b|auth required|unauthori[sz]ed|forbidden|oauth", re.IGNORECASE),
    ),
    ("timeout", re.compile(r"timed out|timeout", re.IGNORECASE)),
    ("http_status", re.compile(r"HTTP status|\b[45]\d\d\b", re.IGNORECASE)),
    (
        "network",
        re.compile(
            r"tcp connect|connection refused|dns error|failed to lookup|network is unreachable|"
            r"error sending request",
            re.IGNORECASE,
        ),
    ),
    ("protocol", re.compile(r"protocol|handshake|initialize", re.IGNORECASE)),
)

#: Session times are whole seconds; a log is the session's only if it spans it.
_SLACK = timedelta(seconds=2)


@dataclass
class _Attempt:
    server: str
    started: datetime
    transport: str | None = None
    endpoint: str | None = None
    connected: bool | None = None
    reason: str | None = None
    ended: datetime | None = None
    #: The background pass that fetches tool descriptions connects to every
    #: configured server; its failure is announced, so it is set aside.
    description_pass: bool = False


@dataclass(frozen=True)
class ProcessLog:
    """What one process's log says, independent of any session."""

    path: Path
    first: datetime | None
    last: datetime | None
    agent_version: str | None
    entrypoint: str | None
    connections: tuple[MCPConnection, ...]


@dataclass(frozen=True)
class Placement:
    state: MCPLogState
    agent_version: str | None = None
    entrypoint: str | None = None
    connections: tuple[MCPConnection, ...] = ()
    #: When the placed log's process was alive, first line to last; `None`
    #: when no log was placed.
    alive: tuple[datetime, datetime] | None = None

    def for_session(self, window: tuple[datetime | None, datetime | None]) -> Placement:
        """This placement, for one session drawn from the row it was made for.

        The lock names the row's last holder, so its log is a session's only
        if that process was alive at the session's own last activity -- the
        same test that chose the log for the row. A sub-agent that finished in
        an earlier process has no log the lock can name.
        """
        if self.alive is None or _alive_at(self.alive, window):
            return self
        return Placement(state="no_log_for_session")


@dataclass
class RunLogs:
    """One pass's view of the logs and locks under a data root."""

    root: Path
    failures: list[str] = field(default_factory=list)
    _state: MCPLogState | None = None
    _by_pid: dict[int, list[Path]] = field(default_factory=dict)
    _locks: dict[str, int] = field(default_factory=dict)
    _unreadable_locks: set[str] = field(default_factory=set)
    _read: dict[Path, ProcessLog | None] = field(default_factory=dict)

    @classmethod
    def load(cls, root: Path) -> RunLogs:
        logs = cls(root=root)
        logs._state = logs._list_logs()
        logs._list_locks()
        return logs

    def place(
        self,
        session: str,
        window: tuple[datetime | None, datetime | None],
        others: dict[str, tuple[datetime | None, datetime | None]],
    ) -> Placement:
        """Place one session's process log, or say why it could not be placed.

        `others` is every other session in the store that could share a
        process with this one, by raw id, with its own time window.
        """
        if self._state is not None:
            return Placement(state=self._state)
        pid = self._locks.get(session)
        if pid is None:
            if session in self._unreadable_locks:
                return Placement(state="log_discovery_incomplete")
            return Placement(state="no_log_for_session")
        spanning = [
            log
            for path in self._by_pid.get(pid, ())
            if (log := self._log(path)) is not None and _spans(log, window)
        ]
        if any(self._read.get(path) is None for path in self._by_pid.get(pid, ())):
            return Placement(state="log_discovery_incomplete")
        if not spanning:
            return Placement(state="no_log_for_session")
        if len(spanning) > 1:
            # The lock names only a pid, never a specific incarnation of it;
            # more than one process log spanning the boundary -- a pid reused
            # right at the session's last activity, within _SLACK of both --
            # means which one the session actually ran in cannot be
            # established.
            return Placement(state="log_discovery_incomplete")
        log = spanning[0]
        sharing = [
            other
            for other, other_window in others.items()
            if other != session and self._locks.get(other) == pid and _spans(log, other_window)
        ]
        alive = (log.first, log.last) if log.first and log.last else None
        if sharing:
            return Placement(
                state="placement_ambiguous",
                agent_version=log.agent_version,
                entrypoint=log.entrypoint,
                alive=alive,
            )
        if any(
            other != session and other in self._unreadable_locks and _spans(log, other_window)
            for other, other_window in others.items()
        ):
            # An overlapping peer's lock could not be read, so it cannot be
            # ruled out as sharing this same process.
            return Placement(state="log_discovery_incomplete")
        return Placement(
            state="applied",
            agent_version=log.agent_version,
            entrypoint=log.entrypoint,
            connections=log.connections,
            alive=alive,
        )

    def _list_logs(self) -> MCPLogState | None:
        directory = self.root / LOGS
        try:
            entries = os.scandir(directory)
        except FileNotFoundError:
            return "no_log_root"
        except OSError as error:
            self.failures.append(f"{directory}: {error}")
            return "log_root_unreadable"
        try:
            with entries:
                for entry in entries:
                    match = _LOG_NAME.match(entry.name)
                    if match:
                        self._by_pid.setdefault(int(match.group("pid")), []).append(
                            Path(entry.path)
                        )
        except OSError as error:
            self.failures.append(f"{directory}: {error}")
            return "log_discovery_incomplete"
        return None

    def _list_locks(self) -> None:
        directory = self.root / LOCKS
        try:
            entries = os.scandir(directory)
        except FileNotFoundError:
            # No lock has ever been taken on this machine: every session is
            # then `no_log_for_session`, which is what is known.
            return
        except OSError as error:
            self.failures.append(f"{directory}: {error}")
            self._state = self._state or "log_discovery_incomplete"
            return
        try:
            with entries:
                for entry in entries:
                    if not entry.name.endswith(".lock"):
                        continue
                    session = entry.name.removesuffix(".lock")
                    try:
                        text = Path(entry.path).read_text(encoding="utf-8").strip()
                        self._locks[session] = int(text)
                    except (OSError, UnicodeDecodeError, ValueError) as error:
                        # This session cannot be placed; every other one can.
                        self.failures.append(f"{entry.path}: unreadable session lock ({error})")
                        self._unreadable_locks.add(session)
        except OSError as error:
            self.failures.append(f"{directory}: {error}")
            self._state = self._state or "log_discovery_incomplete"

    def _log(self, path: Path) -> ProcessLog | None:
        if path not in self._read:
            try:
                self._read[path] = read_log(path)
            except (OSError, EOFError, UnicodeDecodeError) as error:
                self.failures.append(f"{path}: {error}")
                self._read[path] = None
        return self._read[path]


def read_log(path: Path) -> ProcessLog:
    """Parse one process log. Lines this does not recognise are skipped."""
    opener = gzip.open if path.name.endswith(".gz") else open
    first: datetime | None = None
    last: datetime | None = None
    version: str | None = None
    entrypoint: str | None = None
    attempts: list[_Attempt] = []
    with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            match = _LINE.match(raw.rstrip("\n"))
            if match is None:
                continue
            at = _instant(match.group("at"))
            if at is None:
                continue
            first = first or at
            last = at
            rest = match.group("rest")
            if version is None and "init_cli" in rest:
                found = _VERSION.search(rest)
                version = found.group("version") if found else None
            if entrypoint is None:
                if "run_acp_server" in rest:
                    entrypoint = "acp"
                elif "run_repl_mode" in rest:
                    entrypoint = "cli"
            _mcp_line(rest, at, attempts)
    return ProcessLog(
        path=path,
        first=first,
        last=last,
        agent_version=version,
        entrypoint=entrypoint,
        connections=_connections(attempts),
    )


def _mcp_line(rest: str, at: datetime, attempts: list[_Attempt]) -> None:
    if found := _DESCRIPTION.search(rest):
        failed = _latest(attempts, found.group("server"), lambda a: a.connected is False)
        if failed is not None:
            failed.description_pass = True
        return
    if found := _CONNECTING.search(rest):
        attempts.append(_Attempt(server=found.group("server"), started=at))
        return
    if found := _STDIO.search(rest):
        _open(attempts, found.group("server"), at).transport = "stdio"
        return
    if found := _HTTP.search(rest):
        attempt = _open(attempts, found.group("server"), at)
        attempt.transport, attempt.endpoint = "streamable-http", found.group("url")
        return
    if found := _SSE.search(rest):
        attempt = _open(attempts, found.group("server"), at)
        attempt.transport, attempt.endpoint = "sse", found.group("url")
        return
    if found := _CONNECTED.search(rest):
        attempt = _open(attempts, found.group("server"), at)
        attempt.connected, attempt.ended = True, at
        return
    if found := _FAILED.search(rest):
        attempt = _open(attempts, found.group("server"), at)
        attempt.connected, attempt.ended = False, at
        attempt.reason = found.group("reason").strip()


def _latest(
    attempts: list[_Attempt], server: str, predicate: Callable[[_Attempt], bool]
) -> _Attempt | None:
    for attempt in reversed(attempts):
        if attempt.server == server and predicate(attempt):
            return attempt
    return None


def _open(attempts: list[_Attempt], server: str, at: datetime) -> _Attempt:
    """The attempt a line continues: the newest one for this server still open."""
    attempt = _latest(attempts, server, lambda a: a.connected is None)
    if attempt is None:
        attempt = _Attempt(server=server, started=at)
        attempts.append(attempt)
    return attempt


def _connections(attempts: list[_Attempt]) -> tuple[MCPConnection, ...]:
    by_server: dict[str, list[_Attempt]] = {}
    for attempt in attempts:
        if not attempt.description_pass:
            by_server.setdefault(attempt.server, []).append(attempt)
    connections: list[MCPConnection] = []
    for server, tries in by_server.items():
        outcomes = {attempt.connected for attempt in tries if attempt.connected is not None}
        # Attempts that disagree say the server was flapping, or that a
        # background pass's success is mixed in with the session's failure:
        # either way the log does not establish one answer.
        connected = outcomes.pop() if len(outcomes) == 1 else None
        decided = [attempt for attempt in tries if attempt.connected is not None]
        last = decided[-1] if decided else tries[-1]
        # From `last` alone, never searched for independently: a newer,
        # still-pending reconnect under a different transport must not lend
        # its transport or endpoint to an older attempt's outcome.
        transport = last.transport
        endpoint = last.endpoint
        reason = last.reason if connected is False else None
        connections.append(
            MCPConnection(
                server=server,
                transport=transport,
                # A stdio command line can carry credentials; only a URL travels.
                endpoint=endpoint if transport in ("streamable-http", "sse") else None,
                connected=connected,
                failure_category=_category(reason) if reason is not None else None,
                failure_detail=reason,
                duration_ms=_milliseconds(last.started, last.ended),
            )
        )
    return tuple(connections)


def _category(reason: str) -> str:
    for category, pattern in _FAILURE_CATEGORIES:
        if pattern.search(reason):
            return category
    return "unknown"


def _milliseconds(started: datetime, ended: datetime | None) -> int | None:
    if ended is None:
        return None
    return round((ended - started).total_seconds() * 1000)


def _spans(log: ProcessLog, window: tuple[datetime | None, datetime | None]) -> bool:
    """Whether the log's process is the one the session's lock last named.

    The lock names only a session's *last* holder, so the log must still be
    alive at the session's last known activity, not merely somewhere inside
    its lifetime: a pid can be reused by an unrelated, short-lived process
    that falls entirely within a long-lived session's window.
    """
    if log.first is None or log.last is None:
        return False
    return _alive_at((log.first, log.last), window)


def _alive_at(
    alive: tuple[datetime, datetime], window: tuple[datetime | None, datetime | None]
) -> bool:
    """Whether a process alive from `alive[0]` to `alive[1]` was running at a
    session's last known activity (its start when it has no other)."""
    started, last_active = window
    boundary = last_active or started
    if boundary is None:
        return False
    first, last = alive
    return first <= boundary + _SLACK and last >= boundary - _SLACK


def _instant(value: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
