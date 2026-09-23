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
import stat
from collections import Counter, defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import cached_property
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
    #: `(server, tool)` -> calls to that key currently in flight: a `Calling`
    #: line seen with no completion or failure after it yet. Tracked only to
    #: detect overlap; the count itself is never read back out.
    in_flight: dict[tuple[str, str], int] = field(default_factory=dict)
    #: `(server, tool)` keys where two or more calls were ever in flight at
    #: once. The log carries no id per call, so the ordinal join in
    #: `claude_code.py` trusts that the Nth call to complete is the Nth call
    #: the transcript issued -- true only while calls to that key run one at a
    #: time. Two overlapping in flight can complete in either order, and
    #: nothing in the log says which is which, so once overlap is seen for a
    #: key its outcomes are withheld for the whole session rather than risk
    #: swapping two calls' status and duration.
    unordered: set[tuple[str, str]] = field(default_factory=set)

    def start(self, server: str, tool: str) -> None:
        key = (server, tool)
        count = self.in_flight.get(key, 0) + 1
        self.in_flight[key] = count
        if count > 1:
            self.unordered.add(key)

    def finish(self, server: str, tool: str) -> None:
        """Resolve one in-flight call, or record that the log cannot say which.

        An outcome arriving with nothing outstanding for its key means one of
        two things, and the log cannot tell them apart: a `Calling` line went
        missing (a torn record, a rotated or pruned head), or two calls
        overlapped and the line announcing the second was the one lost. Both
        make completion order unusable as invocation order, so the key joins
        `unordered` either way.

        A log that never records a `Calling` line for the key at all is a
        different case -- an older client that does not write them -- and is
        not held to a line it was never going to produce.
        """
        key = (server, tool)
        outstanding = self.in_flight.get(key)
        if outstanding is None:
            return
        if outstanding == 0:
            self.unordered.add(key)
        else:
            self.in_flight[key] = outstanding - 1

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
    """The logs on this machine, keyed by the project and session that wrote them."""

    #: `(project directory, session id)` -> that session's logs. The cache
    #: files a log under a mangled *project* directory, so a session id is only
    #: unique within one of them: a copied, restored or independently rooted
    #: project can carry the same raw session id under another, which is the
    #: same collision the collector already reports for transcripts. Keyed on
    #: both so those two never merge into one entry.
    by_session: dict[tuple[str, str], SessionMCPLogs]
    #: False when no cache directory exists at any known path -- most often an
    #: unsupported platform, and reported as such rather than as "no MCP here".
    root_found: bool
    #: True when `root_found` is False *because* every candidate root that
    #: exists could not be statted, rather than because none exists. Kept
    #: apart from `root_found` so a caller can report "could not be read" for
    #: a permission or transient filesystem failure instead of the platform
    #: explanation `no_log_root` gives an ordinary absence.
    root_unreadable: bool = False
    #: Files present but unreadable or unparseable, by path -- and a cache
    #: root candidate that exists but could not be statted (see
    #: `read_mcp_logs`), which is otherwise indistinguishable from one that
    #: was never configured.
    unreadable: tuple[str, ...] = ()
    #: True when a directory under a cache root could not be scanned at all,
    #: so the set of entries this read found is not known to be every entry on
    #: disk.
    #:
    #: Weaker evidence than `incomplete_project_servers`, and it withholds
    #: more. An unreadable *file* was still named by the directory that holds
    #: it, so both the project and the server it belongs to are known and the
    #: loss can be scoped to that pair (ADR-0006). An unscanned *directory*
    #: yields no names at all: any project, any server and any session id can
    #: be inside it. `for_session`'s single-match fast path trusts a session id
    #: found under exactly one project on the reasoning that one session id
    #: names one session -- which presumes every project that filed a log under
    #: that id was enumerated. Once a subtree went unscanned, it was not, so no
    #: session's identity survives the lookup and the connections go with it,
    #: the same way a cache-side collision already takes them (ADR-0009).
    discovery_incomplete: bool = False
    #: `(cache project directory, server)` pairs with at least one log file
    #: this read could not open or decode.
    #:
    #: A server's log can be split across several files -- one per run -- so a
    #: session's own connection history can span more than one. Skipping an
    #: unreadable file among them leaves `by_session` holding whatever the
    #: *other* files for that server recorded, not the whole story, and the
    #: ordinal join's count guard has no way to tell a genuinely complete count
    #: from one that only looks complete because the missing file's share of it
    #: went uncounted on both sides. Scoped to the project too (ADR-0006): a
    #: server name is not unique across projects, and a session in one project
    #: must not be withheld over a file it never had a share in, in another
    #: project that happens to run a same-named server. Naming the pair here is
    #: what lets a consumer of this index withhold per-call attribution for it
    #: rather than trust a count comparison run against a partial read.
    incomplete_project_servers: frozenset[tuple[str, str]] = frozenset()

    @cached_property
    def incomplete_servers(self) -> frozenset[str]:
        """`incomplete_project_servers` with the project dropped.

        The scoped pair is what a session with a *resolved* log is checked
        against (ADR-0006): the cache project it resolved under is known, so a
        same-named server's unread file in an unrelated project must not
        withhold anything here. A session whose log resolved to nothing has no
        cache project to scope against -- and cannot borrow the transcript's,
        which is a different mangling of the same path and disagrees for most
        directories measured (ADR-0009's table) -- so the only sound question
        left is whether a server it calls had unread evidence anywhere.
        """
        return frozenset(server for _project, server in self.incomplete_project_servers)

    def for_session(
        self, session_id: str, project: str
    ) -> tuple[SessionMCPLogs | None, bool, str | None]:
        """This session's logs, whether the identity was ambiguous, and which
        cache-side project directory they were filed under.

        Returns `(logs, ambiguous, resolved_project)`. `ambiguous` is what a
        caller must withhold on: two projects filed a log under this session id
        and neither can be shown to be this one's. `resolved_project` is the
        *cache's own* project directory name for the match -- not necessarily
        equal to `project` (see below) -- and is `None` whenever `logs` is,
        since nothing was resolved to attribute a scoped lookup against.

        **Why the project name only breaks ties.** The two directory manglings
        are not the same function. Measured on one machine, 99 of 139 cache
        directory names have no exact counterpart under `~/.claude/projects`:
        the cache truncates a long project path and appends a hash suffix
        (`...-agent-local-ditto-1e835041-318a-4985-a288-3baa1e-n0kpsc`), and
        the transcript root does not. Requiring the names to agree would
        therefore withhold enrichment from every project with a long path --
        losing far more than the collision it guards against. So a session id
        found under exactly one project is that session's log whatever the
        directory is called, and the name is consulted only when more than one
        project claims the id and something has to break the tie.
        """
        matches = self._by_id.get(session_id)
        if not matches:
            return None, False, None
        if len(matches) == 1:
            (only_project, only_logs) = next(iter(matches.items()))
            return only_logs, False, only_project
        own = matches.get(project)
        return (own, False, project) if own is not None else (None, True, None)

    @cached_property
    def _by_id(self) -> dict[str, dict[str, SessionMCPLogs]]:
        """`by_session` inverted to session id, so a lookup is not a scan.

        `collect()` asks once per session against an index holding one entry
        per session on the machine, which is quadratic if each ask walks the
        whole index. Built once per index and cached on the instance --
        `cached_property` writes through `__dict__`, which a frozen dataclass
        permits.
        """
        inverted: dict[str, dict[str, SessionMCPLogs]] = defaultdict(dict)
        for (project, session_id), logs in self.by_session.items():
            inverted[session_id][project] = logs
        return dict(inverted)


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


def _walk_log_dirs(root: Path) -> tuple[list[tuple[Path, list[str]]], list[OSError]]:
    """Every `mcp-logs-*` directory under `root`, with the `*.jsonl` names in
    it, and every subtree scan `glob()` would have silently dropped.

    Same gap as `_discover_transcripts` in `claude_code.py`, one directory
    tree over: `Path.glob()`/`Path.rglob()` suppress every `OSError` raised
    while scanning the filesystem as of Python 3.13 -- including a
    `PermissionError` on a directory this process cannot list -- so a project
    directory this process cannot enter vanishes from `**/mcp-logs-*` with no
    trace, and every session whose log lived under it reads as pruned
    (`no_log_for_session`) rather than as withheld. `os.walk`'s `onerror` is
    the one stdlib primitive still willing to name the directory it could not
    scan; each error's `filename` attribute is that directory.
    """
    log_dirs: list[tuple[Path, list[str]]] = []
    failures: list[OSError] = []
    for dirpath, _dirnames, filenames in os.walk(root, onerror=failures.append):
        path = Path(dirpath)
        if path.name.startswith(_LOG_DIR_PREFIX):
            names = sorted(name for name in filenames if name.endswith(".jsonl"))
            log_dirs.append((path, names))
    log_dirs.sort(key=lambda item: item[0])
    return log_dirs, failures


def read_mcp_logs(roots: tuple[Path, ...] | None = None) -> MCPLogIndex:
    """Read every MCP connection log this machine has, keyed by session."""
    return _read_mcp_logs(roots, lambda path: path.read_text(encoding="utf-8").splitlines())


class _IncrementalMCPFile:
    """Retain complete lines and read only bytes appended to one MCP log."""

    def __init__(self) -> None:
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._partial = b""
        self._lines: list[str] = []
        #: True once `path.open()` has succeeded at the current `_offset`.
        #: A reset (fresh file, or one whose size fell below its cursor)
        #: clears it, so a file that has never been opened successfully --
        #: including one whose size happens to equal a just-reset offset of
        #: zero -- cannot take the no-reopen fast path below on an `OSError`
        #: that never gets the chance to update `_offset` in the first place.
        self._opened = False
        #: Changes whenever `_lines` does -- a reset or an appended complete
        #: line -- and never otherwise, so an equal generation means the lines
        #: the index was built from are the lines this file still holds.
        self.generation = 0
        #: Changes on every reset only. Within one epoch `_lines` is only ever
        #: extended, so lines already folded stay a prefix of the lines held.
        self.epoch = 0

    def read(self, path: Path) -> list[str]:
        metadata = path.stat()
        identity = (metadata.st_dev, metadata.st_ino)
        if self._identity != identity or metadata.st_size < self._offset:
            self._reset(identity)
        elif self._opened and metadata.st_size == self._offset:
            # Nothing was appended since the last successful read, and a
            # cache holds thousands of these files, so skip the open as well
            # as the read.
            return self._lines
        with path.open("rb") as handle:
            handle.seek(self._offset)
            appended = handle.read()
            next_offset = handle.tell()
        self._opened = True

        combined = self._partial + appended
        boundary = combined.rfind(b"\n")
        if boundary < 0:
            self._partial = combined
            self._offset = next_offset
            return self._lines
        complete = combined[: boundary + 1]
        partial = combined[boundary + 1 :]
        try:
            text = complete.decode("utf-8")
        except UnicodeDecodeError:
            self._reset(identity)
            raise
        added = text.splitlines()
        if added:
            self._lines.extend(added)
            self.generation += 1
        self._partial = partial
        self._offset = next_offset
        return self._lines

    def _reset(self, identity: tuple[int, int]) -> None:
        self._identity = identity
        self._offset = 0
        self._partial = b""
        self._lines = []
        self._opened = False
        self.generation += 1
        self.epoch += 1


class _IncrementalMCPLogReader:
    """Read and fold only what was appended to each MCP log (ADR-0013).

    Every key a log record can touch in `SessionMCPLogs` belongs to one server,
    so the logs of one `(project, server)` pair fold independently of every
    other pair. Each pair keeps an unsealed fold. When lines were only appended
    to the pair's last file, just those lines are folded into it; any other
    change refolds that pair from its retained lines. The index handed out is
    assembled from sealed copies, so folding later lines never changes an index
    a consumer already holds, and a session whose pairs did not change keeps its
    previous sealed copy.
    """

    def __init__(self, roots: tuple[Path, ...] | None = None) -> None:
        self._roots = roots
        self._files: dict[Path, _IncrementalMCPFile] = {}
        self._pairs: dict[tuple[str, str], _PairFold] = {}
        self._sealed: dict[tuple[str, str], SessionMCPLogs] = {}
        self._last: tuple[object, MCPLogIndex] | None = None

    def read(self) -> MCPLogIndex:
        discovered = _discover_mcp_logs(self._roots)
        if isinstance(discovered, MCPLogIndex):
            self._files.clear()
            self._pairs.clear()
            self._sealed.clear()
            self._last = None
            return discovered
        results: dict[Path, list[str] | OSError | UnicodeDecodeError] = {}
        for path, _server, _project in discovered.files:
            state = self._files.setdefault(path, _IncrementalMCPFile())
            try:
                results[path] = state.read(path)
            except (OSError, UnicodeDecodeError) as error:
                results[path] = error
        for path in self._files.keys() - results.keys():
            del self._files[path]
        # A failed read is keyed as a failure, not by generation: a file that
        # cannot be decoded resets on every attempt, and each attempt produces
        # the same index.
        versions = {
            path: None if isinstance(result, Exception) else self._files[path].generation
            for path, result in results.items()
        }
        signature = (discovered, tuple(versions.items()))
        if self._last is not None and self._last[0] == signature:
            return self._last[1]

        groups: dict[tuple[str, str], list[Path]] = {}
        for path, server, project in discovered.files:
            groups.setdefault((project, server), []).append(path)
        pairs: dict[tuple[str, str], _PairFold] = {}
        affected: set[tuple[str, str]] = set()
        for key, paths in groups.items():
            pairs[key] = self._fold_pair(key, paths, results, versions, affected)
        for key in self._pairs.keys() - pairs.keys():
            affected.update(self._pairs[key].by_session)
        self._pairs = pairs

        parts: dict[tuple[str, str], list[SessionMCPLogs]] = defaultdict(list)
        for pair in pairs.values():
            for session, logs in pair.by_session.items():
                if session in affected:
                    parts[session].append(logs)
        for session in affected:
            if session in parts:
                self._sealed[session] = _sealed_copy(parts[session])
            else:
                self._sealed.pop(session, None)

        unreadable = list(discovered.unreadable)
        unreadable.extend(str(path) for path, _s, _p in discovered.files if versions[path] is None)
        index = MCPLogIndex(
            by_session=dict(self._sealed),
            root_found=True,
            discovery_incomplete=discovered.discovery_incomplete,
            unreadable=tuple(sorted(unreadable)),
            incomplete_project_servers=frozenset(
                key for key, pair in pairs.items() if pair.incomplete
            ),
        )
        self._last = (signature, index)
        return index

    def _fold_pair(
        self,
        key: tuple[str, str],
        paths: list[Path],
        results: dict[Path, list[str] | OSError | UnicodeDecodeError],
        versions: dict[Path, int | None],
        affected: set[tuple[str, str]],
    ) -> _PairFold:
        project, server = key
        signature = tuple((path, versions[path]) for path in paths)
        previous = self._pairs.get(key)
        if previous is not None and previous.signature == signature:
            return previous
        last = paths[-1]
        lines = results[last]
        if (
            previous is not None
            and previous.folder is not None
            and previous.last == (last, self._files[last].epoch)
            and previous.signature[:-1] == signature[:-1]
            and isinstance(lines, list)
            and len(lines) >= previous.folder.folded
        ):
            previous.folder.fold(
                lines[previous.folder.folded :], server, project, previous.by_session
            )
            previous.folder.folded = len(lines)
            affected.update(previous.folder.touched)
            previous.signature = signature
            return previous

        if previous is not None:
            affected.update(previous.by_session)
        pair = _PairFold(signature=signature)
        for position, path in enumerate(paths):
            result = results[path]
            pair.folder = None
            if isinstance(result, Exception):
                pair.prefix_incomplete = True
                continue
            folder = _LineFolder()
            folder.fold(result, server, project, pair.by_session)
            folder.folded = len(result)
            if position < len(paths) - 1 and folder.torn:
                pair.prefix_incomplete = True
            pair.folder = folder
        if pair.folder is not None:
            pair.last = (last, self._files[last].epoch)
        affected.update(pair.by_session)
        return pair


@dataclass
class _PairFold:
    """The unsealed fold of one `(project, server)` pair's log files."""

    #: `(path, generation or None)` for every file, in fold order.
    signature: tuple[tuple[Path, int | None], ...]
    by_session: dict[tuple[str, str], SessionMCPLogs] = field(
        default_factory=lambda: defaultdict(SessionMCPLogs)
    )
    #: An unreadable file, or a torn record in a file before the last one.
    prefix_incomplete: bool = False
    #: The last file's path and epoch, when it was readable and folded.
    last: tuple[Path, int] | None = None
    #: The last file's folder, which later appended lines continue.
    folder: _LineFolder | None = None

    @property
    def incomplete(self) -> bool:
        return self.prefix_incomplete or (self.folder is not None and self.folder.torn)


def _sealed_copy(parts: list[SessionMCPLogs]) -> SessionMCPLogs:
    """One session's logs from every pair that wrote them, sealed.

    Copies everything a later fold could mutate. The pairs hold disjoint keys,
    each being one server's, so merging is a union.
    """
    merged = SessionMCPLogs()
    for part in parts:
        for server, connection in part.connections.items():
            merged.connections[server] = replace(connection)
        for key, queue in part.outcomes.items():
            merged.outcomes[key] = deque(queue)
        merged.waiting.update(part.waiting)
        merged.in_flight.update(part.in_flight)
        merged.unordered.update(part.unordered)
    merged.seal()
    return merged


def _read_mcp_logs(
    roots: tuple[Path, ...] | None,
    read_lines: Callable[[Path], list[str]],
) -> MCPLogIndex:
    discovered = _discover_mcp_logs(roots)
    if isinstance(discovered, MCPLogIndex):
        return discovered
    return _fold_mcp_logs(discovered, read_lines)


@dataclass(frozen=True)
class _DiscoveredLogs:
    """Every MCP log file one pass found, in the order the index folds them."""

    #: `(path, server, cache project directory)`, roots in candidate order and
    #: each root's directories and file names sorted.
    files: tuple[tuple[Path, str, str], ...]
    #: Candidate roots and directories that could not be statted or scanned.
    unreadable: tuple[str, ...]
    discovery_incomplete: bool


def _discover_mcp_logs(roots: tuple[Path, ...] | None) -> MCPLogIndex | _DiscoveredLogs:
    """The log files to fold, or the finished index when no cache root exists."""
    candidates = cache_roots() if roots is None else roots
    present: list[Path] = []
    unreadable: list[str] = []
    # Set when a candidate could not be statted at all -- unlike a candidate
    # confirmed to be a non-directory, its type is unknown, so it cannot be
    # ruled out as a directory holding sessions another, readable candidate
    # also claims (ADR-0009's argument, applied one level up: an unscanned
    # *root* withholds exactly as much as an unscanned subdirectory does).
    root_probe_incomplete = False
    for root in candidates:
        try:
            is_dir = stat.S_ISDIR(root.stat().st_mode)
        except FileNotFoundError:
            # `stat()` follows symlinks, so this also fires for a dangling
            # symlink at `root` -- indistinguishable from true absence unless
            # `lstat()` is asked whether anything is there at all.
            try:
                root.lstat()
            except FileNotFoundError:
                # No cache at this candidate path is the ordinary state; most
                # platforms have exactly one candidate, and it usually does
                # not exist.
                continue
            except OSError:
                unreadable.append(str(root))
                root_probe_incomplete = True
                continue
            # `lstat()` succeeded where `stat()` did not: a symlink exists at
            # `root` but its target does not -- a broken
            # `CLAUDE_CLI_CACHE_DIR` or a stray dangling link at a default
            # path, not the ordinary absence the inner `FileNotFoundError`
            # above handles. A dangling link is confirmed to hold nothing at
            # this path, so unlike the two `OSError` catches around it, it
            # does not leave `root`'s type unknown.
            unreadable.append(str(root))
            continue
        except OSError:
            # The candidate exists but could not be statted -- a permission
            # or transient filesystem failure, unlike the ordinary absence
            # above. `Path.is_dir()` cannot tell them apart: before Python
            # 3.14 it propagates some `OSError`s and swallows others, and
            # from 3.14 it swallows every `OSError` and reports `False`,
            # indistinguishable from a root that was never configured -- so
            # every session would silently lose MCP enrichment with nothing
            # to show for it, the same gap `ClaudeCodeReader.collect` closed
            # for the transcript root. Recorded as the bare path, the same
            # shape a file-level failure takes below -- `collect()` wraps
            # every entry in `unreadable` with its own "could not be read".
            unreadable.append(str(root))
            root_probe_incomplete = True
            continue
        if is_dir:
            present.append(root)
        else:
            # The candidate exists but is a regular file (or a symlink to
            # one), not a directory -- a misconfigured `CLAUDE_CLI_CACHE_DIR`
            # or a stray file at a default path, not the ordinary absence the
            # `FileNotFoundError` branch above handles. Folding it into
            # `present` would try to `os.walk()` a non-directory; leaving it
            # out of both lists would make it indistinguishable from a root
            # that was never configured, the same falsehood-as-absence gap
            # the `OSError` branch above closes.
            unreadable.append(str(root))
    if not present:
        # Every candidate that exists having errored (`unreadable` non-empty)
        # is a different fact from none of them existing at all -- the one
        # `root_found=False` alone cannot state, per `MCPLogIndex.root_unreadable`.
        return MCPLogIndex(
            by_session={},
            root_found=False,
            root_unreadable=bool(unreadable),
            unreadable=tuple(sorted(unreadable)),
        )

    files: list[tuple[Path, str, str]] = []
    # A root whose type could not be determined (see `root_probe_incomplete`
    # above) might have been a directory holding a second claimant for a
    # session id or project this pass resolved from the *other*, readable
    # roots -- the same reason an unscanned subdirectory below sets this flag.
    discovery_incomplete = root_probe_incomplete
    for root in present:
        log_dirs, walk_failures = _walk_log_dirs(root)
        # Whatever level of the tree it failed at, a directory that could not be
        # scanned yields no file names, so nothing bounds what was inside it --
        # not the servers, and not the session ids. It is deliberately not
        # scoped to the pair the way an unreadable file is (ADR-0006): naming
        # the server for a failed `mcp-logs-<server>` directory would state the
        # completeness half of the loss and leave the identity half, which is
        # the stronger claim, unstated (ADR-0009).
        discovery_incomplete = discovery_incomplete or bool(walk_failures)
        unreadable.extend(str(error.filename) for error in walk_failures)
        for directory, names in log_dirs:
            server = directory.name[len(_LOG_DIR_PREFIX) :]
            project = directory.parent.name
            files.extend((directory / name, server, project) for name in names)
    return _DiscoveredLogs(
        files=tuple(files),
        unreadable=tuple(unreadable),
        discovery_incomplete=discovery_incomplete,
    )


def _fold_mcp_logs(
    discovered: _DiscoveredLogs,
    read_lines: Callable[[Path], list[str]],
) -> MCPLogIndex:
    """Build the index from the lines of every discovered log file."""
    by_session: dict[tuple[str, str], SessionMCPLogs] = defaultdict(SessionMCPLogs)
    incomplete_project_servers: set[tuple[str, str]] = set()
    unreadable = list(discovered.unreadable)
    for path, server, project in discovered.files:
        try:
            lines = read_lines(path)
        except (OSError, UnicodeDecodeError):
            unreadable.append(str(path))
            incomplete_project_servers.add((project, server))
            continue
        if _read_file(lines, server, project, by_session):
            incomplete_project_servers.add((project, server))
    # Every log has been read, so an unresolved wait is final rather than
    # merely not-yet-answered.
    for logs in by_session.values():
        logs.seal()
    return MCPLogIndex(
        by_session=dict(by_session),
        root_found=True,
        discovery_incomplete=discovered.discovery_incomplete,
        unreadable=tuple(sorted(unreadable)),
        incomplete_project_servers=frozenset(incomplete_project_servers),
    )


def _read_file(
    lines: list[str],
    server: str,
    project: str,
    by_session: dict[tuple[str, str], SessionMCPLogs],
) -> bool:
    """Fold one log file into the index; report whether anything was lost.

    Returns `True` when a line anywhere but the end failed to decode. A torn
    *final* write is ordinary -- the log is appended to live -- but an earlier
    one is a record that existed and could not be read, and what it said is
    unknowable. One of the things it can have said is `Calling MCP tool` for a
    call that overlapped another, which is the only evidence that this server's
    completion order is not its invocation order; the surviving completions
    still make the per-tool counts agree, so no other guard sees anything
    wrong. Naming the project and server together lets the ordinal join decline
    for that pair rather than trust a sequence assembled from a read known to
    have a hole in it -- and rather than withhold a same-named server in an
    unrelated project that never shared the incomplete file (ADR-0006).
    """
    folder = _LineFolder()
    folder.fold(lines, server, project, by_session)
    return folder.torn


class _LineFolder:
    """Fold one log file's lines in order, a batch at a time.

    Whether an undecodable line is the file's last record -- ordinary, see
    `_read_file` -- is only known once the next record arrives, so that
    judgement is held open across batches rather than made per batch.
    """

    def __init__(self) -> None:
        self.torn = False
        #: Lines of the file folded so far; kept by the caller.
        self.folded = 0
        #: `(project, session)` keys the latest `fold` call wrote to.
        self.touched: set[tuple[str, str]] = set()
        self._undecodable_last = False

    def fold(
        self,
        lines: list[str],
        server: str,
        project: str,
        by_session: dict[tuple[str, str], SessionMCPLogs],
    ) -> None:
        self.touched = set()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if self._undecodable_last:
                self.torn = True
                self._undecodable_last = False
            try:
                record = json.loads(line)
            except ValueError:
                self._undecodable_last = True
                continue
            if not isinstance(record, dict):
                continue
            session_id = record.get("sessionId")
            message = record.get("debug")
            if not isinstance(session_id, str) or not isinstance(message, str):
                continue
            self.touched.add((project, session_id))
            _apply(message, server, by_session[(project, session_id)])


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
        # The URL is an operator coordinate, not content: an MCP URL already
        # travels in a BOM on these same terms.
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
        # The completion line is what carries the outcome; this is tracked
        # only to detect two calls to the same key overlapping.
        logs.start(server, found.group(1))
        return

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
    logs.finish(server, tool)
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
