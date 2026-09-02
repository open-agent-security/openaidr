"""MCP connection logs: what Claude Code records about a server it talked to.

The transcript says an MCP tool was called and, for 129 of 130 calls measured on
a real corpus, nothing about how it ended. It never says which transport carried
the call, what the server calls itself, or whether the connection came up at all.
Claude Code writes all of that to a per-server log beside its own cache, and this
module reads it.

**Why it belongs here.** OpenAIDR reads the session state an agent already writes
to disk, and owns "everything the shared event schema cannot express — which
files exist, which session each one belongs to, what a tool name means, and what
an outcome was." This is the outcome half, for the one class of call the
transcript cannot answer it for.

**Best-effort, and it says so when it fails.** The path is an undocumented cache
whose directory names mangle the project directory, and it is pruned on the
agent's own schedule, not ours. Every way this can come up short is reported as
a state on the session rather than passed off as an absence of findings: a run
that could not read the logs and a run whose servers were all local must not look
the same (ADR-0003's argument, one layer down).

**Nothing here is judgement.** A transport is recorded, not interpreted; that
`claudeai-proxy` implies the call left the machine is a consumer's conclusion.
"""

from __future__ import annotations

import json
import os
import platform
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "MCPCallOutcome",
    "MCPLogIndex",
    "SessionMCPLogs",
    "cache_roots",
    "read_mcp_logs",
]

_LOG_DIR_PREFIX = "mcp-logs-"

_TRANSPORT = re.compile(r"Successfully connected \(transport: ([A-Za-z0-9_-]+)\) in (\d+)ms")
_HTTP_ENDPOINT = re.compile(r"Initializing HTTP transport to (\S+)")
_PROXY = re.compile(r"Initializing claude\.ai proxy transport for server (\S+)")
_CAPABILITIES = re.compile(r"Connection established with capabilities: (\{.*\})\s*$")
_CALLING = re.compile(r"^Calling MCP tool: (\S+)")
_COMPLETED = re.compile(r"^Tool '(.+?)' completed successfully(?: in (\d+)ms)?")
_FAILED = re.compile(r"^Tool '(.+?)' failed(?: in (\d+)ms)?")
#: The client's own liveness report, emitted every 30s while it waits. It is the
#: only *observed* evidence that a call is stuck rather than merely unfinished:
#: `pending` in a transcript is indistinguishable from a session still running,
#: while this is the client saying it has been waiting, and for how long.
#:
#: Measured on real logs, 5 of 7 such episodes were followed by a completion, so
#: the line alone means "slow", not "hung". What means hung is a `still running`
#: with no outcome after it — a property of the finished log, independent of when
#: anything scanned.
_STILL_RUNNING = re.compile(r"^Tool '(.+?)' still running \((\d+)s elapsed\)")
#: The status slot is not always an HTTP code. Measured on 993 real failure
#: lines: 410 carry a bare status (`400`), 154 carry a symbolic one
#: (`CONNECTION_CLOSED`), 14 carry a negative JSON-RPC code (`-32000`) and 415
#: carry none. Accepting only digits dropped the whole line for 168 of them --
#: not the status, the *line*, so `connected`, `duration_ms` and
#: `failure_category` were all left unset and a real failure read as silence.
_CONNECT_FAILED = re.compile(r"Connection failed after (\d+)ms(?: \(([^)]+)\))?: (.*)$", re.DOTALL)


@dataclass(frozen=True)
class MCPCallOutcome:
    """How one MCP call ended, as the client recorded it."""

    server: str
    tool: str
    #: `True` completed, `False` failed, `None` **never resolved** — the client
    #: reported the call still running and no outcome ever followed it.
    ok: bool | None
    duration_ms: int | None


@dataclass
class _Connection:
    server: str
    transport: str | None = None
    endpoint: str | None = None
    advertised_name: str | None = None
    advertised_version: str | None = None
    #: `None` until the log states an outcome. A server whose log carries only
    #: an endpoint or a capabilities line -- observed while the connection is
    #: still being established, or from prose the current patterns do not
    #: recognise -- has not been *seen to fail*, and defaulting to `False`
    #: would assert that anyway.
    connected: bool | None = None
    failure_category: str | None = None
    failure_detail: str | None = None
    duration_ms: int | None = None


@dataclass
class SessionMCPLogs:
    """Everything the logs record for one session."""

    connections: dict[str, _Connection] = field(default_factory=dict)
    #: Call outcomes in the order the client logged them, per `(server, tool)`.
    #: The logs carry no span and no call id, so position within one tool's
    #: sequence is the only thing that can identify one. Keyed by the same
    #: compound identity the rest of the codebase joins on: servers own their
    #: schemas independently, so two of them can expose `search`, and a name-only
    #: queue would hand one server's call the other server's outcome.
    outcomes: dict[tuple[str, str], deque[MCPCallOutcome]] = field(
        default_factory=lambda: defaultdict(deque)
    )
    #: `(server, tool)` -> longest elapsed the client reported while still
    #: waiting, for a call that has not yet been resolved by a completion or a
    #: failure. What survives here at end of file never came back.
    waiting: dict[tuple[str, str], int] = field(default_factory=dict)

    def seal(self) -> None:
        """Turn every unresolved wait into an outcome that says so.

        Called once the whole log is read: a `still running` with nothing after
        it is the client's last word on that call, so it becomes an outcome with
        `ok=None` rather than being dropped.
        """
        for (server, tool), elapsed in self.waiting.items():
            self.outcomes[(server, tool)].append(
                MCPCallOutcome(server=server, tool=tool, ok=None, duration_ms=elapsed)
            )
        self.waiting.clear()

    def tool_counts(self) -> Counter[tuple[str, str]]:
        return Counter({key: len(queue) for key, queue in self.outcomes.items()})


@dataclass(frozen=True)
class MCPLogIndex:
    """The logs on this machine, keyed by the session that produced them."""

    by_session: dict[str, SessionMCPLogs]
    #: False when no cache directory exists at any known path -- most often an
    #: unsupported platform, and reported as such rather than as "no MCP here".
    root_found: bool
    #: Files present but unreadable or unparseable, by path.
    unreadable: tuple[str, ...] = ()

    def for_session(self, session_id: str) -> SessionMCPLogs | None:
        return self.by_session.get(session_id)


def cache_roots() -> tuple[Path, ...]:
    """Where Claude Code's CLI cache lives, per platform.

    Undocumented on every one of them, so all the plausible locations are
    probed and absence is normal rather than an error. `CLAUDE_CLI_CACHE_DIR`
    overrides, which is also what makes this testable without a real cache.
    """
    override = os.environ.get("CLAUDE_CLI_CACHE_DIR")
    if override:
        return (Path(override),)
    home = Path.home()
    system = platform.system()
    if system == "Darwin":
        return (home / "Library/Caches/claude-cli-nodejs",)
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else home / "AppData/Local"
        return (base / "claude-cli-nodejs",)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else home / ".cache"
    return (base / "claude-cli-nodejs",)


def read_mcp_logs(roots: tuple[Path, ...] | None = None) -> MCPLogIndex:
    """Read every MCP connection log this machine has, keyed by session."""
    candidates = cache_roots() if roots is None else roots
    present = [root for root in candidates if root.is_dir()]
    if not present:
        return MCPLogIndex(by_session={}, root_found=False)

    by_session: dict[str, SessionMCPLogs] = defaultdict(SessionMCPLogs)
    unreadable: list[str] = []
    for root in present:
        for directory in root.glob(f"**/{_LOG_DIR_PREFIX}*"):
            if not directory.is_dir():
                continue
            server = directory.name[len(_LOG_DIR_PREFIX) :]
            for path in sorted(directory.glob("*.jsonl")):
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeDecodeError):
                    unreadable.append(str(path))
                    continue
                _read_file(lines, server, by_session)
    # Every log has been read, so an unresolved wait is final rather than
    # merely not-yet-answered.
    for logs in by_session.values():
        logs.seal()
    return MCPLogIndex(
        by_session=dict(by_session), root_found=True, unreadable=tuple(sorted(unreadable))
    )


def _read_file(lines: list[str], server: str, by_session: dict[str, SessionMCPLogs]) -> None:
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            # One malformed line is not a malformed file: the log is appended
            # to live, so a torn final write is ordinary.
            continue
        if not isinstance(record, dict):
            continue
        session_id = record.get("sessionId")
        message = record.get("debug")
        if not isinstance(session_id, str) or not isinstance(message, str):
            continue
        _apply(message, server, by_session[session_id])


def _apply(message: str, server: str, logs: SessionMCPLogs) -> None:
    connection = logs.connections.setdefault(server, _Connection(server=server))

    found = _TRANSPORT.search(message)
    if found:
        connection.transport = found.group(1)
        connection.connected = True
        connection.duration_ms = int(found.group(2))
        # A retried connection can fail before it succeeds, and the client
        # logs both lines to the same server's log. A prior failure is not
        # this connection's final state once a later line says it came up --
        # carrying it forward would report both success and failure at once.
        connection.failure_category = None
        connection.failure_detail = None
        return

    found = _HTTP_ENDPOINT.search(message)
    if found:
        # The URL is an operator coordinate, not content: OpenACA already
        # carries an MCP server's URL in the BOM on the same terms.
        connection.endpoint = found.group(1)
        return

    found = _PROXY.search(message)
    if found:
        connection.endpoint = found.group(1)
        return

    found = _CAPABILITIES.search(message)
    if found:
        _read_capabilities(found.group(1), connection)
        return

    found = _CONNECT_FAILED.search(message)
    if found:
        connection.connected = False
        connection.duration_ms = int(found.group(1))
        detail = found.group(3).strip()
        connection.failure_detail = detail
        connection.failure_category = _categorise(found.group(2), detail)
        return

    found = _CALLING.match(message)
    if found:
        return  # The completion line is what carries the outcome.

    found = _COMPLETED.match(message)
    if found:
        _record(logs, server, found, ok=True)
        return

    found = _FAILED.match(message)
    if found:
        _record(logs, server, found, ok=False)
        return

    found = _STILL_RUNNING.match(message)
    if found:
        key, elapsed = (server, found.group(1)), int(found.group(2)) * 1000
        logs.waiting[key] = max(logs.waiting.get(key, 0), elapsed)


def _record(logs: SessionMCPLogs, server: str, found: re.Match[str], *, ok: bool) -> None:
    tool = found.group(1)
    logs.waiting.pop((server, tool), None)
    duration = found.group(2)
    logs.outcomes[(server, tool)].append(
        MCPCallOutcome(
            server=server,
            tool=tool,
            ok=ok,
            duration_ms=int(duration) if duration else None,
        )
    )


def _read_capabilities(blob: str, connection: _Connection) -> None:
    try:
        parsed = json.loads(blob)
    except ValueError:
        return
    version = parsed.get("serverVersion") if isinstance(parsed, dict) else None
    if not isinstance(version, dict):
        return
    name, number = version.get("name"), version.get("version")
    if isinstance(name, str) and name:
        connection.advertised_name = name
    if isinstance(number, str) and number:
        connection.advertised_version = number


def _categorise(status: str | None, detail: str) -> str:
    """A failure reason a finding can carry, without the server's own words.

    The raw message is free text an MCP server chose, and can hold a URL, a
    header fragment or whatever else the author printed. The category is the
    part a consumer can act on and the part that is safe to travel; the detail
    stays local (`LOCAL_ONLY`).
    """
    lowered = detail.lower()
    if status in {"401", "403"} or "authorization" in lowered or "unauthorized" in lowered:
        return "auth"
    if "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if status and status.lstrip("-").isdigit():
        # A negative code is JSON-RPC's, not HTTP's -- the transport carried the
        # message and the peer rejected it, which is a protocol failure.
        return "http_status" if not status.startswith("-") else "protocol"
    if "econnrefused" in lowered or "enotfound" in lowered or "socket" in lowered:
        return "network"
    if "protocol" in lowered or "jsonrpc" in lowered:
        return "protocol"
    # A symbolic status the client chose rather than the server: the channel
    # went away before the handshake finished. `CONNECTION_CLOSED` is the
    # dominant one, and it is what a stdio server exiting at startup looks like.
    if status:
        return "protocol"
    return "unknown"
