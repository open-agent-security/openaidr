"""Reads Devin CLI sessions from its local SQLite store and per-process run logs.

Devin CLI, Devin Local (Devin Desktop's default agent) and editors driving it
over ACP are one harness with one data root, `$XDG_DATA_HOME/devin/cli` or
`~/.local/share/devin/cli`. The session store there is `sessions.db`, a SQLite
database in WAL mode, opened here read-only and read inside one transaction, so
a pass sees committed rows and nothing half-written (ADR-0018). `adr-sensor`
has no Devin parser; this reader is OpenAIDR's own, on the same contract.

**A conversation is a tree.** Regenerating or editing a turn forks it, and
`sessions.main_chain_id` is the current leaf. Turns are the path from that leaf
to the root; every branch's requests still count as usage, because each was a
real request (ADR-0019). A sub-agent's chain is its own tree in the same
session's forest, named by `subagent_heads`, and becomes a session of its own.

What this reader states is what the store records. Where a field's source is
not confirmed against a signed-in session -- `docs/specs/devin-cli.md` lists
them -- the reader takes the conservative reading: a failure without a stated
reason is `unknown`, never `error`, because reading a person's refusal as a
tool's fault is the worst wrong answer this model can give.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import socket
import sqlite3
import stat
from collections.abc import Iterable, Mapping
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
from openaidr.readers.devin_cli_log import LOCKS, LOGS, Placement, RunLogs
from openaidr.toolclass import canonical_arguments, tool_class
from openaidr.toolnames import split_tool_name

SOURCE = "devin"
STORE = "sessions.db"
#: The newest migration this reader was built against (`V17__subagent_heads`).
KNOWN_MIGRATION = 17

_REQUIRED = {
    "sessions": {"id", "working_directory", "model", "created_at", "last_activity_at"},
    "message_nodes": {
        "row_id",
        "session_id",
        "node_id",
        "parent_node_id",
        "chat_message",
        "created_at",
    },
}
_RESULT_META = "chisel/tool_result_meta"
_GENERIC_MCP = "mcp_call_tool"

#: `ToolResult.failure_reason`, as the binary declares it, to a status and the
#: same `denial_kind` vocabulary the Claude Code reader uses.
_FAILURES: dict[str, tuple[Status, str | None]] = {
    "Rejected": ("rejected", "user-rejected"),
    "Blocked": ("rejected", "permission-rule"),
    "ToolError": ("error", None),
    "ValidationError": ("error", None),
    "UnsearchedTool": ("error", None),
}


def default_root() -> Path:
    data = os.environ.get("XDG_DATA_HOME")
    base = Path(data) if data else Path(os.path.expanduser("~")) / ".local" / "share"
    return base / "devin" / "cli"


@dataclass(frozen=True)
class _Row:
    id: str
    working_directory: str
    model: str
    created_at: datetime | None
    last_activity_at: datetime | None
    title: str | None
    main_chain_id: int | None
    hidden: bool


@dataclass(frozen=True)
class _Node:
    row_id: int
    node_id: int
    parent: int | None
    message: dict[str, object]
    created_at: datetime | None
    metadata: dict[str, object]

    @property
    def meta(self) -> dict[str, object]:
        return _mapping(self.message.get("metadata"))

    @property
    def role(self) -> str:
        role = self.message.get("role")
        return "assistant" if role == "agent" else role if isinstance(role, str) else ""

    @property
    def at(self) -> datetime | None:
        """The message's own call time where it states one; else the row's second."""
        return _instant(self.meta.get("created_at")) or self.created_at


@dataclass
class _Store:
    rows: list[_Row]
    nodes: dict[str, list[_Node]]
    heads: dict[str, list[tuple[str, int]]]
    prompts: dict[str, str]
    subagents_recorded: bool
    failures: list[str] = field(default_factory=list)


class DevinCliReader:
    """Reads Devin CLI sessions from `sessions.db` and places run-log facts on them."""

    agent_kind = "devin-cli"

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root if root is not None else default_root()).resolve()
        #: Per session, a digest of the session `collect_file` last returned.
        #: In memory only (ADR-0011). What is re-emitted is decided by
        #: comparing outputs, never by reasoning about which input an event
        #: touched (ADR-0018).
        self._emitted: dict[str, bytes] = {}

    def collect(self, window: Window) -> tuple[list[Session], list[ReaderFailure]]:
        store, failures = self._load()
        if store is None:
            return [], failures
        logs = RunLogs.load(self._root)
        rows = [row for row in store.rows if _within(row, store.nodes.get(row.id, ()), window)]
        sessions = self._sessions(store, rows, logs, failures)
        if window.since is not None:
            # A row can pass because its main conversation is still within
            # the window while one of its sub-agents, scoped to its own
            # tree (ADR-0019), is not.
            sessions = [
                s
                for s in sessions
                if s.last_activity_at is not None and s.last_activity_at >= window.since
            ]
        failures.extend(self._failure(message) for message in logs.failures)
        return sessions, failures

    def collect_file(self, path: Path) -> tuple[list[Session], list[ReaderFailure]]:
        """Return every session that differs from what this reader last returned.

        Any event under the data root -- `sessions.db` or its `-wal` (a WAL
        commit changes only that file), a run log, a session lock -- can
        change what a session is: its rows, its placement, or the placement
        of every other session that shared its process. So whichever path
        the watcher names, every session is rebuilt and compared with the
        one last returned under its identity, and only those that differ are
        returned (ADR-0018). A consumer replacing by identity then holds what
        a cold read holds, without this reader enumerating what changed.
        """
        path = path.resolve()
        store_path = self._root / STORE
        ours = path in (store_path, Path(f"{store_path}-wal")) or path.parent in (
            self._root / LOGS,
            self._root / LOCKS,
        )
        if not ours:
            return [], []
        store, failures = self._load()
        if store is None:
            return [], failures
        logs = RunLogs.load(self._root)
        changed: list[Session] = []
        for session in self._sessions(store, store.rows, logs, failures):
            digest = hashlib.sha256(repr(session).encode()).digest()
            if self._emitted.get(session.session_id) != digest:
                self._emitted[session.session_id] = digest
                changed.append(session)
        failures.extend(self._failure(message) for message in logs.failures)
        return changed, failures

    # --- the store -----------------------------------------------------------

    def _load(self) -> tuple[_Store | None, list[ReaderFailure]]:
        path = self._root / STORE
        try:
            root_mode = self._root.stat().st_mode
        except FileNotFoundError:
            return None, []
        except OSError as error:
            return None, [self._failure(f"{self._root}: {error}")]
        if not stat.S_ISDIR(root_mode):
            return None, [self._failure(f"{self._root}: not a directory")]
        try:
            path.stat()
        except FileNotFoundError:
            return None, []
        except OSError as error:
            return None, [self._failure(f"{path}: {error}")]
        try:
            connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2.0)
        except sqlite3.Error as error:
            return None, [self._failure(f"{path}: {error}")]
        try:
            connection.isolation_level = None
            connection.execute("BEGIN")
            store = self._read(connection)
            connection.execute("COMMIT")
        except sqlite3.OperationalError as error:
            text = str(error)
            busy = "locked" in text or "busy" in text
            return None, [self._failure(f"{path}: {'store busy' if busy else text}")]
        except sqlite3.Error as error:
            return None, [self._failure(f"{path}: {error}")]
        finally:
            connection.close()
        if isinstance(store, str):
            return None, [self._failure(f"{path}: {store}")]
        return store, [self._failure(message) for message in store.failures]

    def _read(self, db: sqlite3.Connection) -> _Store | str:
        tables = {
            name: {column[1] for column in db.execute(f"PRAGMA table_info({name})")}
            for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for table, columns in _REQUIRED.items():
            missing = columns - tables.get(table, set())
            if missing:
                return f"unsupported store schema: {table} lacks {', '.join(sorted(missing))}"
        failures: list[str] = []
        if "refinery_schema_history" in tables:
            (newest,) = db.execute("SELECT max(version) FROM refinery_schema_history").fetchone()
            if isinstance(newest, int) and newest > KNOWN_MIGRATION:
                failures.append(
                    f"store schema V{newest} is newer than V{KNOWN_MIGRATION}; read for the known columns"
                )
        session_columns = tables["sessions"]
        optional = [c for c in ("title", "main_chain_id", "hidden") if c in session_columns]
        rows = [
            _row(
                dict(
                    zip(
                        (
                            "id",
                            "working_directory",
                            "model",
                            "created_at",
                            "last_activity_at",
                            *optional,
                        ),
                        values,
                        strict=True,
                    )
                )
            )
            for values in db.execute(
                "SELECT id, working_directory, model, created_at, last_activity_at"
                + "".join(f", {c}" for c in optional)
                + " FROM sessions"
            )
        ]
        has_node_metadata = "metadata" in tables["message_nodes"]
        nodes: dict[str, list[_Node]] = {}
        for values in db.execute(
            "SELECT session_id, row_id, node_id, parent_node_id, chat_message, created_at"
            + (", metadata" if has_node_metadata else "")
            + " FROM message_nodes ORDER BY row_id"
        ):
            session_id, row_id, node_id, parent, raw, created = values[:6]
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                failures.append(f"session {session_id} node {node_id}: chat_message is not JSON")
                continue
            if not isinstance(message, dict):
                failures.append(
                    f"session {session_id} node {node_id}: chat_message is not an object"
                )
                continue
            nodes.setdefault(session_id, []).append(
                _Node(
                    row_id=row_id,
                    node_id=node_id,
                    parent=parent,
                    message=message,
                    created_at=_instant(created),
                    metadata=_json_object(values[6]) if has_node_metadata else {},
                )
            )
        heads: dict[str, list[tuple[str, int]]] = {}
        if "subagent_heads" in tables:
            for session_id, agent_id, head in db.execute(
                "SELECT session_id, agent_id, chain_node_id FROM subagent_heads ORDER BY session_id, agent_id"
            ):
                heads.setdefault(session_id, []).append((agent_id, head))
        prompts: dict[str, str] = {}
        if {"content", "session_id", "is_shell"} <= tables.get("prompt_history", set()):
            for session_id, content in db.execute(
                "SELECT session_id, content FROM prompt_history WHERE is_shell = 0 ORDER BY id"
            ):
                prompts.setdefault(session_id, content)
        return _Store(
            rows=rows,
            nodes=nodes,
            heads=heads,
            prompts=prompts,
            subagents_recorded="subagent_heads" in tables,
            failures=failures,
        )

    # --- sessions ------------------------------------------------------------

    def _sessions(
        self,
        store: _Store,
        rows: Iterable[_Row],
        logs: RunLogs,
        failures: list[ReaderFailure],
    ) -> list[Session]:
        windows = {
            row.id: (row.created_at, _effective_last_activity(row, store.nodes.get(row.id, ())))
            for row in store.rows
            if not row.hidden
        }
        sessions: list[Session] = []
        for row in rows:
            if row.hidden:
                # Internal helpers (the summarizer, the titler): no recorded
                # parent, person or owner to attribute them to.
                continue
            placement = logs.place(row.id, windows[row.id], windows)
            sessions.extend(self._build(store, row, placement, failures))
        return sessions

    def _build(
        self, store: _Store, row: _Row, placement: Placement, failures: list[ReaderFailure]
    ) -> list[Session]:
        nodes = store.nodes.get(row.id, [])
        by_id = {node.node_id: node for node in nodes}
        roots = {node.node_id: _root(node.node_id, by_id) for node in nodes}
        heads = store.heads.get(row.id, [])
        # A sub-agent owns every node in its tree; the main session owns the
        # rest. A head that hangs off the main tree instead of its own root
        # owns only its chain's nodes off the main chain, so a malformed head
        # can never carry the main session's turns and usage away with it.
        leaf = row.main_chain_id if row.main_chain_id in by_id else None
        main_chain = {node.node_id for node in _chain(leaf, by_id)}
        main_root = roots.get(leaf) if leaf is not None else None
        owner: dict[int, str] = {}
        for agent_id, head in heads:
            tree = roots.get(head)
            if tree is None:
                continue
            if tree == main_root:
                claimed = [n.node_id for n in _chain(head, by_id) if n.node_id not in main_chain]
            else:
                claimed = [node_id for node_id, root in roots.items() if root == tree]
            for node_id in claimed:
                owner.setdefault(node_id, agent_id)
        main_nodes = [node for node in nodes if node.node_id not in owner]
        if leaf is None and main_nodes:
            leaf = max(main_nodes, key=lambda node: node.row_id).node_id
        built = [
            self._session(
                store,
                row,
                session_id=f"{self.agent_kind}:{row.id}",
                chain=_chain(leaf, by_id),
                tree=main_nodes,
                sidechain=False,
                placement=placement,
                failures=failures,
            )
        ]
        for agent_id, head in heads:
            head_chain = _chain(head, by_id)
            if roots.get(head) == main_root:
                head_chain = [n for n in head_chain if n.node_id not in main_chain]
            built.append(
                self._session(
                    store,
                    row,
                    session_id=f"{self.agent_kind}:{row.id}:{agent_id}",
                    chain=head_chain,
                    tree=[node for node in nodes if owner.get(node.node_id) == agent_id],
                    sidechain=True,
                    placement=placement,
                    failures=failures,
                )
            )
        return built

    def _session(
        self,
        store: _Store,
        row: _Row,
        *,
        session_id: str,
        chain: list[_Node],
        tree: list[_Node],
        sidechain: bool,
        placement: Placement,
        failures: list[ReaderFailure],
    ) -> Session:
        results = _results(chain, tree)
        transports = {c.server: c.transport for c in placement.connections}
        responses = _responses(tree, sidechain)
        turns: list[Turn] = []
        context: list[ContextItem] = []
        compactions: list[Compaction] = []
        initial: str | None = None
        for position, node in enumerate(chain):
            summarized = node.metadata.get("summarized_from", node.meta.get("summarized_from"))
            if summarized is not None:
                compactions.append(
                    Compaction(
                        trigger="unknown",
                        pre_tokens=_count(
                            node.metadata.get(
                                "num_tokens_preceding", node.meta.get("num_tokens_preceding")
                            )
                        ),
                        dropped_tokens=None,
                        occurred_at=node.at,
                    )
                )
            role, text = node.role, _text(node.message.get("content"))
            if role == "tool":
                continue
            if role == "system" or (role == "user" and node.meta.get("is_user_input") is False):
                context.append(
                    ContextItem(
                        # The node id, not the position in `context`: a
                        # regeneration can move the chain to a branch with
                        # different context nodes, and a positional index
                        # would then misname an unrelated item's span.
                        span=span_id(session_id, "context", node.node_id),
                        source="system" if role == "system" else "injected",
                        name=None,
                        text=text,
                        occurred_at=node.at,
                    )
                )
                continue
            if role not in ("user", "assistant"):
                continue
            if role == "user" and initial is None:
                initial = text
            key = str(node.node_id)
            turns.append(
                Turn(
                    position=len(turns),
                    key=key,
                    role=role,
                    text=text,
                    is_sidechain=sidechain,
                    tool_calls=self._calls(
                        node,
                        session_id,
                        key,
                        results,
                        # Not just the chain's last node: an assistant node's
                        # calls are still at the tip if everything after it
                        # is a result for one of them -- a sibling result
                        # landing first does not mean the conversation moved
                        # on, so an unmatched sibling is still pending.
                        at_tip=all(n.role == "tool" for n in chain[position + 1 :]),
                        transports=transports,
                        failures=failures,
                    ),
                    occurred_at=node.at,
                    response_id=_response_key(node) if role == "assistant" else None,
                )
            )
        newest = max((n.at for n in tree if n.at is not None), default=None)
        # A sub-agent's own tree, never the row's column: the row tracks the
        # whole conversation's activity, which a parent can extend long after
        # one of its sub-agents stopped.
        last_activity = (
            newest
            if sidechain
            else max((t for t in (row.last_activity_at, newest) if t is not None), default=None)
        )
        return Session(
            session_id=session_id,
            agent_kind=map_source(SOURCE),
            source=SOURCE,
            started_at=row.created_at,
            model=_newest_model(tree) or (row.model or None),
            working_directory=row.working_directory or None,
            machine=socket.gethostname() or None,
            user=_user(),
            turns=tuple(turns),
            agent_version=placement.agent_version,
            entrypoint=placement.entrypoint,
            initial_prompt=initial
            if initial is not None or sidechain
            else store.prompts.get(row.id),
            context_items=tuple(context),
            compactions=tuple(compactions),
            mcp_connections=placement.connections,
            mcp_log_state=placement.state,
            last_activity_at=last_activity,
            responses=responses,
            generated_title=row.title or None if not sidechain else None,
            compactions_recorded=True,
            subagents_recorded=store.subagents_recorded,
        )

    def _calls(
        self,
        node: _Node,
        session_id: str,
        key: str,
        results: Mapping[str, _Node],
        *,
        at_tip: bool,
        transports: Mapping[str, str | None],
        failures: list[ReaderFailure],
    ) -> tuple[ToolCall, ...]:
        raw_calls = node.message.get("tool_calls")
        if not isinstance(raw_calls, list):
            return ()
        calls: list[ToolCall] = []
        for index, raw in enumerate(raw_calls):
            entry = _mapping(raw)
            function = _mapping(entry.get("function"))
            name = _string(entry.get("name")) or _string(function.get("name")) or ""
            call_id = _string(entry.get("id"))
            arguments = _arguments(entry.get("arguments", function.get("arguments")))
            if arguments is None:
                failures.append(
                    self._failure(
                        f"{session_id} node {node.node_id} call {call_id}: arguments are not an object"
                    )
                )
                arguments = {}
            server, tool = split_tool_name(self.agent_kind, name)
            if server is None and name == _GENERIC_MCP:
                server, tool = _generic_mcp(arguments, name)
            outcome = results.get(call_id) if call_id else None
            meta = _result_meta(outcome) if outcome is not None else {}
            body = _text(outcome.message.get("content")) if outcome is not None else None
            status, denial = _status(outcome, meta, at_tip)
            calls.append(
                ToolCall(
                    span=span_id(session_id, key, index),
                    tool_name=tool,
                    mcp_server=server,
                    status=status,
                    arguments=arguments,
                    result=body,
                    result_size=len(body) if body is not None else None,
                    error_text=body if status == "error" else None,
                    truncated="overflow_metadata" in meta and meta["overflow_metadata"] is not None,
                    provider_call_id=call_id,
                    denial_kind=denial,
                    working_directory=_string(meta.get("cwd")),
                    transport=transports.get(server) if server is not None else None,
                    tool_class=tool_class(self.agent_kind, tool, server),
                    canonical_arguments=canonical_arguments(
                        self.agent_kind, tool, server, arguments
                    ),
                    exit_code=_count(meta.get("exit_code"), allow_negative=True),
                )
            )
        return tuple(calls)

    def _failure(self, message: str) -> ReaderFailure:
        return ReaderFailure(agent_kind=self.agent_kind, message=message)


# --- helpers ------------------------------------------------------------------


def _row(values: dict[str, object]) -> _Row:
    main = values.get("main_chain_id")
    return _Row(
        id=str(values["id"]),
        working_directory=str(values.get("working_directory") or ""),
        model=str(values.get("model") or ""),
        created_at=_instant(values.get("created_at")),
        last_activity_at=_instant(values.get("last_activity_at")),
        title=_string(values.get("title")),
        main_chain_id=main if isinstance(main, int) else None,
        hidden=bool(values.get("hidden")),
    )


def _effective_last_activity(row: _Row, nodes: Iterable[_Node]) -> datetime | None:
    """The row's own column, or a node's later metadata timestamp.

    A node's own metadata timestamp can carry precision (sub-second) the
    store's whole-second `last_activity_at` column lacks, and so can exceed
    it. This is the same effective value `_session()` reports as the main
    session's `Session.last_activity_at` (ADR-0019); used wherever that
    column alone would be a stale stand-in for it -- window filtering and
    log placement alike.
    """
    newest = max((n.at for n in nodes if n.at is not None), default=None)
    return max(
        (t for t in (row.last_activity_at, row.created_at, newest) if t is not None), default=None
    )


def _within(row: _Row, nodes: Iterable[_Node], window: Window) -> bool:
    if window.since is None:
        return True
    latest = _effective_last_activity(row, nodes)
    return latest is not None and latest >= window.since


def _root(node_id: int, by_id: Mapping[int, _Node]) -> int:
    seen: set[int] = set()
    current = node_id
    while True:
        seen.add(current)
        parent = by_id[current].parent if current in by_id else None
        if parent is None or parent not in by_id or parent in seen:
            return current
        current = parent


def _chain(leaf: int | None, by_id: Mapping[int, _Node]) -> list[_Node]:
    """The path from the root to `leaf`, oldest first; a cycle stops the walk."""
    path: list[_Node] = []
    seen: set[int] = set()
    current = leaf
    while current is not None and current in by_id and current not in seen:
        seen.add(current)
        node = by_id[current]
        path.append(node)
        current = node.parent
    return list(reversed(path))


def _results(chain: list[_Node], tree: list[_Node]) -> dict[str, _Node]:
    """Each call id's result: the one on the chain first, else one in the tree."""
    found: dict[str, _Node] = {}
    for node in [*chain, *tree]:
        if node.role == "tool":
            call_id = _string(node.message.get("tool_call_id"))
            if call_id:
                found.setdefault(call_id, node)
    return found


def _result_meta(node: _Node) -> dict[str, object]:
    extensions = _mapping(node.meta.get("extensions"))
    meta = extensions.get(_RESULT_META)
    if isinstance(meta, str):
        return _json_object(meta)
    return _mapping(meta)


def _status(
    outcome: _Node | None, meta: Mapping[str, object], at_tip: bool
) -> tuple[Status, str | None]:
    if outcome is None:
        # Nothing came back. At the tip the call may still be running; behind
        # it the conversation moved on without a result the store kept.
        return ("pending" if at_tip else "unknown"), None
    success = meta.get("success")
    if success is True:
        return "ok", None
    reason = meta.get("failure_reason")
    if isinstance(reason, dict) and len(reason) == 1:
        reason = next(iter(reason))
    if success is False and isinstance(reason, str) and reason in _FAILURES:
        return _FAILURES[reason]
    return "unknown", None


def _newest_model(tree: list[_Node]) -> str | None:
    """The model of the most recently written response, by `row_id`.

    Not `responses[-1].model`: that list is ordered by a response's first
    appearance, and a regeneration can add a later record under an id that
    appeared earlier, leaving the true newest record stranded mid-list.
    """
    candidates = [
        node
        for node in tree
        if node.role == "assistant" and _string(node.meta.get("generation_model")) is not None
    ]
    if not candidates:
        return None
    return _string(max(candidates, key=lambda node: node.row_id).meta.get("generation_model"))


def _responses(tree: list[_Node], sidechain: bool) -> tuple[ModelResponse, ...]:
    """One response per provider request across the tree, from its last record."""
    first_seen: dict[str, int] = {}
    latest: dict[str, _Node] = {}
    for node in tree:
        if node.role != "assistant":
            continue
        meta = node.meta
        if meta.get("metrics") is None and meta.get("request_id") is None:
            continue
        key = _response_key(node)
        if key is None:
            continue
        first_seen.setdefault(key, node.row_id)
        latest[key] = node
    responses = []
    for key in sorted(first_seen, key=first_seen.__getitem__):
        node = latest[key]
        meta = node.meta
        usage = _mapping(meta.get("metrics"))
        responses.append(
            ModelResponse(
                response_id=key,
                request_id=_string(meta.get("request_id")),
                model=_string(meta.get("generation_model")),
                occurred_at=node.at,
                final=meta.get("finish_reason") is not None,
                is_sidechain=sidechain,
                usage=TokenUsage(
                    input_tokens=_count(usage.get("input_tokens")),
                    cache_read_input_tokens=_count(usage.get("cache_read_tokens")),
                    cache_creation_input_tokens=_count(usage.get("cache_creation_tokens")),
                    output_tokens=_count(usage.get("output_tokens")),
                ),
            )
        )
    return tuple(responses)


def _response_key(node: _Node) -> str | None:
    meta = node.meta
    request = _string(meta.get("request_id"))
    if request:
        return request
    if meta.get("metrics") is not None:
        return f"node-{node.node_id}"
    return None


def _generic_mcp(arguments: Mapping[str, object], name: str) -> tuple[str | None, str]:
    """`mcp_call_tool` names its server and tool in its arguments; recover both."""
    server, tool = _string(arguments.get("server_name")), _string(arguments.get("tool_name"))
    if server and tool:
        return server, tool
    return None, name


def _arguments(value: object) -> dict[str, object] | None:
    if value is None:
        return {}
    if isinstance(value, str):
        if not value.strip():
            return {}
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        return "\n".join(parts)
    return ""


def _instant(value: object) -> datetime | None:
    """A stored time as an instant in UTC: epoch seconds, epoch milliseconds, or RFC 3339."""
    try:
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int | float):
            seconds = value / 1000 if value >= 10_000_000_000 else value
            return datetime.fromtimestamp(seconds, UTC)
        if isinstance(value, str) and value:
            moment = datetime.fromisoformat(value)
            return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return None


def _count(value: object, *, allow_negative: bool = False) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if allow_negative or value >= 0 else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _user() -> str | None:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return None
