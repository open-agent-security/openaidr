"""Claude Code sessions, parsed by `adr-sensor` and normalised here.

The division of labour is the point. `adr-sensor` owns the format: it knows how
Claude Code's JSONL is shaped and how to turn one file into an event. OpenAIDR
owns everything the shared event schema cannot express — which files exist, which
session each one belongs to, what a tool name means, and what an outcome was.

**Discovery is ours because identity is ours.** Upstream's whole-tree parse walks
`**/*.jsonl` and keys each session on the `sessionId` field, but every record in
a subagent transcript carries the *parent's* `sessionId`, and the event schema has
no field for the file it came from. Parsed that way, a parent and its subagents
collapse into one identity. Calling upstream per file keeps the path — the only
thing that distinguishes them — in our hands (ADR-0001).
"""

from __future__ import annotations

import contextlib
import io
import json
import re
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from adr_sensor.parsers.claude_parser import ClaudeParser
from adr_sensor.schemas.agent_event_schema import AgentEvent, ChatMessage, ToolUsage

from openaidr.kinds import map_source
from openaidr.model import (
    Compaction,
    ContextItem,
    MCPConnection,
    MCPLogState,
    ProviderRefusal,
    Session,
    Status,
    ToolCall,
    Turn,
    span_id,
)
from openaidr.readers.base import ReaderFailure, Window
from openaidr.readers.claude_code_mcp import MCPCallOutcome, MCPLogIndex, read_mcp_logs
from openaidr.toolnames import split_tool_name

#: Where Claude Code keeps session transcripts, one directory per project root.
DEFAULT_ROOT = Path.home() / ".claude" / "projects"

#: The on-disk name this kind gives itself, as `adr-sensor` reports it.
SOURCE = "claude"

#: A subagent's transcript lives under this directory beside its parent's file.
_SUBAGENT_DIR = "subagents"

#: What `toolDenialKind` says, mapped onto this package's vocabulary.
#:
#: All three are refusals, so all three are `rejected`. They are kept apart in
#: `ToolCall.denial_kind` because *who* refused is a different fact from *that*
#: something was refused: a person declining and an automated classifier blocking
#: are not the same event to anything reasoning downstream.
_DENIAL_KINDS = {
    "user-rejected": "a person declined the call",
    "automode-blocked": "an automated classifier blocked the call",
    "permission-rule": "a configured permission rule blocked the call",
}


class ClaudeCodeReader:
    """Reads Claude Code sessions, one file at a time, through `adr-sensor`."""

    agent_kind = "claude-code"

    def __init__(self, root: Path | None = None, mcp_logs: MCPLogIndex | None = None) -> None:
        self._root = root if root is not None else DEFAULT_ROOT
        self._parser = ClaudeParser()
        #: transcript path -> everything that file records and upstream drops.
        #: One read per file per `collect()` pass, however many sessions or
        #: fields ask for it; `collect()` clears this before each pass so a
        #: reader kept alive across calls never serves a stale read of a file
        #: that has since grown.
        self._transcripts: dict[Path, _Transcript] = {}
        #: Read once per `collect()` pass rather than per session: the logs are
        #: filed under a mangled project directory, not under a session, so
        #: finding one session's would mean walking the same tree every time.
        #: An injected index is authoritative for this reader's whole lifetime
        #: (what makes a test independent of a real cache); the default is
        #: instead reloaded on every pass, since the log is append-only the
        #: same way a transcript is, and a reader kept alive across repeated
        #: `collect()` calls must see a connection or outcome the log gained
        #: since the last pass rather than the first pass's snapshot forever.
        self._injected_mcp_logs = mcp_logs
        self._mcp_logs: MCPLogIndex | None = mcp_logs

    def collect(self, window: Window) -> tuple[list[Session], list[ReaderFailure]]:
        if not self._root.is_dir():
            return [], []
        # Scoped to this pass, not this reader's lifetime: a session file is
        # append-only, so a reader kept alive across repeated `collect()` calls
        # (the steady-state design in docs/specs/session-collection.md) must see
        # a growing file's new records each time, not the recovery data an
        # earlier, shorter read of the same path already cached.
        self._transcripts = {}
        self._mcp_logs = (
            self._injected_mcp_logs if self._injected_mcp_logs is not None else read_mcp_logs()
        )
        mcp_logs = self._mcp_logs
        sessions: list[Session] = []
        failures: list[ReaderFailure] = [
            # The log itself could still be read; only these specific files
            # inside it could not, and each is one file, not a failed kind
            # (ADR-0003's argument, one layer down).
            ReaderFailure(agent_kind=self.agent_kind, message=f"{path}: could not be read")
            for path in mcp_logs.unreadable
        ]
        for path in sorted(self._root.glob("**/*.jsonl")):
            try:
                in_window = _within_window(path, window)
            except OSError as error:
                # The file listed by the glob a moment ago is gone, replaced or
                # otherwise inaccessible by the time it is stat'd. One missing
                # transcript is not a failed kind: report it and move on to the
                # rest, the same isolation `_read_file` already gives a
                # transcript it cannot parse.
                failures.append(
                    ReaderFailure(agent_kind=self.agent_kind, message=f"{path}: {error}")
                )
                continue
            if not in_window:
                continue
            file_sessions, file_failure = self._read_file(path)
            sessions.extend(file_sessions)
            if file_failure is not None:
                failures.append(file_failure)
        return sessions, failures

    def _read_file(self, path: Path) -> tuple[list[Session], ReaderFailure | None]:
        """Parse one file upstream, then normalise what comes back.

        Upstream narrates its progress on stdout, which would corrupt this
        package's own machine-readable output, so it is captured rather than
        left to print directly — but it is read, not merely discarded: upstream
        prints exactly when it caught a file it could not open or decode and
        returned no data for it, which is otherwise indistinguishable from a
        file with nothing in it.
        """
        sink = io.StringIO()
        try:
            with contextlib.redirect_stdout(sink):
                events = self._parser.parse_jsonl_file(path)
        except Exception as error:  # noqa: BLE001 - one unreadable file is not a failed kind
            return [], ReaderFailure(agent_kind=self.agent_kind, message=f"{path}: {error}")
        captured = sink.getvalue().strip()
        if captured:
            return [], ReaderFailure(agent_kind=self.agent_kind, message=f"{path}: {captured}")
        is_subagent = path.parent.name == _SUBAGENT_DIR
        return [self._session(event, path, is_subagent) for event in events], None

    def _project_root(self, event: AgentEvent, path: Path) -> str | None:
        """Where the session ran, from the best evidence available.

        Three sources, most trustworthy first.

        Upstream fixes `project_path` from whichever record *first* carried the
        session's id and never refills it, so a session whose transcript opens
        with a record that has no `cwd` — a `queue-operation`, typically — loses
        the field permanently even though later records carry it. Measured on one
        machine: 44 of 333 sessions, one of which repeated its `cwd` on 93 later
        records. Upstream sharpens `timestamp` and `model` from later records in
        the same loop, so this reads as an omission rather than a decision; it is
        recorded as an upstream ask in `docs/specs/session-collection.md`.

        Rereading the transcript for it is ours to do and cheap — only sessions
        upstream left empty are reread, and the answer is the transcript's own
        recorded `cwd`, not an inference.

        Only when the file itself says nothing do we fall back to decoding the
        directory Claude Code names after the project root
        (`-Users-me-Projects-thing`). That decoding is ambiguous — a dash
        separates path segments *and* appears inside directory names — so
        candidates are rebuilt greedily against the filesystem and returned only
        if they exist. An ambiguous split therefore cannot invent a path, and a
        project since renamed or deleted stays unknown rather than becoming a
        confident wrong answer.
        """
        if event.project_path:
            return event.project_path
        recorded = self._recorded_cwd(path, event.session_id)
        if recorded:
            return recorded
        directory = path.parent.parent if path.parent.name == _SUBAGENT_DIR else path.parent
        return _decode_project_directory(directory.name)

    def _read(self, path: Path) -> _Transcript:
        """What this transcript records, read once per file."""
        cached = self._transcripts.get(path)
        if cached is None:
            cached = _recorded(path)
            self._transcripts[path] = cached
        return cached

    def _recorded_cwd(self, path: Path, session_id: str) -> str | None:
        """The `cwd` this transcript records for this session, if any.

        Keyed by session id rather than taken from the file as a whole: one file
        can hold more than one session, and lending one session's directory to
        another would be exactly the confident wrong answer this exists to avoid.
        """
        recorded = self._read(path).cwds
        raw = _strip_source_prefix(session_id)
        return recorded.get(raw) or recorded.get(session_id)

    def _session(self, event: AgentEvent, path: Path, is_subagent: bool) -> Session:
        raw_id = _strip_source_prefix(event.session_id)
        # A subagent's records carry its parent's session id, so the file's own
        # path is what makes it a distinct session (ADR-0001).
        identity = f"{raw_id}:{path.stem}" if is_subagent else raw_id
        session_id = f"{self.agent_kind}:{identity}"
        transcript = self._read(path)
        if is_subagent:
            # The log is keyed by `raw_id` too, which for a subagent is the
            # *parent's* session id (ADR-0001) -- the same collision the
            # session identity above works around. Looking it up here would
            # hand every subagent the parent's connections and, where
            # per-tool counts happen to agree, the parent's or a sibling
            # subagent's outcomes. Nothing survives the dependency boundary
            # that disambiguates one file's share of that log from
            # another's, so it is withheld rather than attributed on a
            # guess (ADR-0002, ADR-0003's argument).
            enrichment = _MCPEnrichment(state="not_attempted")
        else:
            enrichment = self._mcp_enrichment(raw_id, event)
        return Session(
            session_id=session_id,
            agent_kind=map_source(event.source),
            source=event.source,
            started_at=event.timestamp,
            model=event.model,
            working_directory=self._project_root(event, path),
            machine=event.hostname,
            user=event.username,
            agent_version=transcript.versions.get(raw_id),
            entrypoint=transcript.entrypoints.get(raw_id),
            git_branch=transcript.branches.get(raw_id),
            initial_prompt=transcript.initial_prompts.get(raw_id),
            context_items=tuple(
                ContextItem(
                    # The same shape a call's span has, so a finding cites one
                    # the same way rather than a consumer learning a second
                    # addressing scheme. `context` is the turn-key position.
                    span=span_id(session_id, "context", index),
                    source=source,
                    name=name,
                    text=text,
                )
                for index, (source, name, text) in enumerate(transcript.context.get(raw_id, ()))
            ),
            compactions=tuple(
                Compaction(trigger=trigger, pre_tokens=pre, dropped_tokens=dropped)
                for trigger, pre, dropped in transcript.compactions.get(raw_id, ())
            ),
            provider_refusals=tuple(
                ProviderRefusal(category=category, original_model=original, fallback_model=fallback)
                for category, original, fallback in transcript.refusals.get(raw_id, ())
            ),
            mcp_connections=enrichment.connections,
            mcp_log_state=enrichment.state,
            turns=_turns(
                event.chat_history, session_id, raw_id, is_subagent, transcript, enrichment
            ),
        )

    def _mcp_enrichment(self, raw_id: str, event: AgentEvent) -> _MCPEnrichment:
        """What the connection log adds to this session, and whether it may.

        **The guard.** The log carries no span and no call id -- only an ordered
        run of `Tool 'x' completed`/`failed` lines -- so the only way to say
        *which* call an outcome belongs to is position within that tool's
        sequence. That is sound while both sides recorded every call, and
        silently wrong the moment one did not: a single dropped line shifts
        every later outcome onto the wrong call, with nothing to show for it.

        So per-`(server, tool)` counts must agree before any outcome is
        attributed -- the same compound key the join itself uses, since a name
        alone would let two servers' sequences cover for each other. Where
        they do not, the connection-scoped facts are still kept -- transport and
        advertised identity are properties of the connection, unaffected by how
        many calls went over it -- and the per-call half is withheld. A gap that
        says so beats an attribution that cannot be checked.
        """
        index = self._mcp_logs
        if index is None:
            return _MCPEnrichment(state="not_attempted")
        if not index.root_found:
            return _MCPEnrichment(state="no_log_root")
        logs = index.for_session(raw_id)
        if logs is None:
            # Only a session that actually called an MCP tool is missing
            # anything; the rest simply never opened a connection.
            state: MCPLogState = "no_log_for_session" if _calls_mcp(event) else "applied"
            return _MCPEnrichment(state=state)
        connections = tuple(
            MCPConnection(
                server=c.server,
                transport=c.transport,
                endpoint=c.endpoint,
                advertised_name=c.advertised_name,
                advertised_version=c.advertised_version,
                connected=c.connected,
                failure_category=c.failure_category,
                failure_detail=c.failure_detail,
                duration_ms=c.duration_ms,
            )
            for c in sorted(logs.connections.values(), key=lambda c: c.server)
        )
        # Compared per `(server, tool)`, and only over the ones the transcript
        # actually calls. Outcomes are queued and popped under that same key, so
        # a tool present only in the log can never shift one that is in both --
        # the client makes MCP calls of its own (`closeAllDiffTabs`,
        # `getDiagnostics` against the IDE server) that are not agent tool calls
        # and never appear in a transcript. Comparing whole count maps would
        # read those as corruption and withhold outcomes over nothing.
        transcript_counts = _mcp_tool_counts(event)
        log_counts = logs.tool_counts()
        # A count agreement is only as trustworthy as the read it was taken
        # from. A server whose log spans more than one file -- one per run --
        # can have some of those files unreadable while others parsed cleanly;
        # `log_counts` then reflects only the readable remainder, and nothing
        # about a count built from a partial read can tell "this really is
        # every call" from "this merely looks complete because the missing
        # file's share went uncounted." Any server this session actually calls
        # that had an unreadable file anywhere is therefore treated the same
        # as a count that failed to agree, rather than trusted on a guard run
        # against data known to be incomplete.
        touches_incomplete_server = any(
            server in index.incomplete_servers for server, _tool in transcript_counts
        )
        if touches_incomplete_server or any(
            log_counts.get(key, 0) != n for key, n in transcript_counts.items()
        ):
            return _MCPEnrichment(state="count_mismatch", connections=connections)
        transports = {
            server: connection.transport for server, connection in logs.connections.items()
        }
        # A count agreement says the two sides logged the same number of
        # calls; it says nothing about whether the log's completion order
        # matches the transcript's invocation order. Two calls to the same key
        # that overlapped can complete in either order, with nothing in the
        # log to say which is which, so a key `logs` ever saw overlap on is
        # left out of the ordinal join entirely rather than risk swapping two
        # calls' status and duration.
        outcomes = {
            key: deque(queue) for key, queue in logs.outcomes.items() if key not in logs.unordered
        }
        return _MCPEnrichment(
            state="applied",
            connections=connections,
            outcomes=outcomes,
            transports=transports,
        )


@dataclass
class _MCPEnrichment:
    """What the connection log contributes to one session.

    Stateful by design: `take` pops from a per-`(server, tool)` queue, so
    consecutive calls to the same tool on the same server consume consecutive
    outcomes. That is the ordinal join, and it is only ever reached once
    `_mcp_enrichment`'s guard has established that both sides counted the same
    calls.
    """

    state: MCPLogState
    connections: tuple[MCPConnection, ...] = ()
    outcomes: dict[tuple[str, str], deque[MCPCallOutcome]] = field(default_factory=dict)
    transports: dict[str, str | None] = field(default_factory=dict)

    def take(self, server: str, tool: str) -> MCPCallOutcome | None:
        queue = self.outcomes.get((server, tool))
        return queue.popleft() if queue else None

    def transport_for(self, server: str | None) -> str | None:
        return self.transports.get(server) if server else None


def _calls_mcp(event: AgentEvent) -> bool:
    return any(
        _server_and_tool(tool)[0] is not None
        for message in event.chat_history
        for tool in message.tools
    )


def _mcp_tool_counts(event: AgentEvent) -> Counter[tuple[str, str]]:
    """How many times each MCP tool was called, as the transcript has it.

    Compared against the log's own count to decide whether the ordinal join is
    trustworthy. Counted per `(server, tool)` rather than in total: a total would
    hide one server's log being pruned behind another's being complete, and two
    servers exposing the same tool name would cover for each other on a name
    alone.
    """
    counts: Counter[tuple[str, str]] = Counter()
    for message in event.chat_history:
        for tool in message.tools:
            server, name = _server_and_tool(tool)
            if server is not None:
                counts[(server, name)] += 1
    return counts


def _turns(
    messages: list[ChatMessage],
    session_id: str,
    raw_session_id: str,
    is_subagent: bool,
    transcript: _Transcript,
    mcp: _MCPEnrichment,
) -> tuple[Turn, ...]:
    """Build turns, keeping each turn's key unique within its session.

    Upstream's `sequence_id` is the record's own identifier and is *usually*
    unique, but real transcripts repeat it — a resumed session can re-emit a
    record. A repeat is disambiguated by occurrence, and only the later one:
    the first keeps the plain key, so a span already emitted for it never
    changes when a duplicate turns up later in the file (ADR-0001).
    """
    turns: list[Turn] = []
    seen: dict[str, int] = {}
    compromised = _compromised_results(messages)
    reused_call_ids = _reused_call_ids(transcript)
    for message in messages:
        base = message.sequence_id or f"turn-{len(turns)}"
        occurrence = seen.get(base, 0)
        seen[base] = occurrence + 1
        key = base if occurrence == 0 else f"{base}#{occurrence}"
        turns.append(
            Turn(
                position=len(turns),
                key=key,
                role=message.role,
                text=message.content,
                is_sidechain=is_subagent,
                tool_calls=_tool_calls(
                    message.tools,
                    session_id,
                    key,
                    raw_session_id,
                    base,
                    occurrence,
                    compromised,
                    reused_call_ids,
                    transcript,
                    mcp,
                ),
                permission_mode=transcript.permission_modes.get((raw_session_id, base, occurrence)),
            )
        )
    return tuple(turns)


def _reused_call_ids(transcript: _Transcript) -> set[str]:
    """Raw provider call ids this transcript's own recovery pass attaches to
    more than one call.

    `_compromised_results` only catches upstream's own structural mismatch --
    two calls that share tool name, type, server and arguments. It says
    nothing about a literal id reused across two calls whose *shape* differs
    (one `Read`, one `Bash`), because those land in different structural
    groups there and neither is flagged. But `call_ids`, `results`, `errored`,
    `denials`, `started` and `ended` are all keyed on the bare id this reader
    recovers directly from the raw record -- shape never enters into it -- so
    a reused id hands every call that carries it whichever call's data this
    pass's single pass over the file wrote there last, regardless of whether
    the calls look alike (ADR-0001, ADR-0002's argument extended to this
    reader's own recovery, not just upstream's).
    """
    counts = Counter(transcript.call_ids.values())
    return {call_id for call_id, count in counts.items() if count > 1}


def _compromised_results(messages: list[ChatMessage]) -> set[int]:
    """Find calls whose result cannot be trusted as necessarily their own.

    Upstream resolves a provider result by locating any structurally-identical
    *pending* `ToolUsage` in the whole transcript, not only the one that issued
    the call, and updates every match it finds rather than stopping at the
    first (`ClaudeParser._create_entry_from_extracted_session`). Two calls with
    the same tool name, server and arguments therefore end up sharing one
    result even when they had distinct provider ids and distinct results in
    the file — one copy wins and the other is silently discarded.

    Two or more otherwise-identical calls left holding one byte-identical
    result is what that mismatch looks like from the projected model — but it
    is an ambiguous signal, not a diagnostic one: the same shape is produced
    by two calls that genuinely ran independently and happened to return the
    same thing (rereading an unchanged file, for one). Nothing surviving the
    dependency boundary — no provider id, no result-arrival order — can tell
    those two apart, so both are withheld alike (ADR-0002).
    """
    groups: dict[tuple[str, str, str | None, str], list[ToolUsage]] = {}
    for message in messages:
        for tool in message.tools:
            key = (tool.tool_name, tool.tool_type, tool.server_name, _freeze(tool.arguments))
            groups.setdefault(key, []).append(tool)
    compromised: set[int] = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        by_result: dict[str, list[ToolUsage]] = {}
        for tool in group:
            if tool.result is not None:
                by_result.setdefault(tool.result, []).append(tool)
        for shared in by_result.values():
            if len(shared) >= 2:
                compromised.update(id(tool) for tool in shared)
    return compromised


def _freeze(arguments: dict | None) -> str:
    return json.dumps(arguments or {}, sort_keys=True, default=str)


def _tool_calls(
    tools: list[ToolUsage],
    session_id: str,
    turn_key: str,
    raw_session_id: str,
    record_uuid: str,
    occurrence: int,
    compromised: set[int],
    reused_call_ids: set[str],
    transcript: _Transcript,
    mcp: _MCPEnrichment,
) -> tuple[ToolCall, ...]:
    """Normalise one message's calls, joining each to what the record says about it.

    The join is `(raw session id, record uuid, occurrence, position)` — the
    same `(uuid, position)` pair span identity is already built from, plus the
    occurrence a repeated uuid is disambiguated by, and the raw session id a
    uuid shared across two sessions in one file is disambiguated by
    (ADR-0001), verified against 1,063 real messages where upstream's tool
    order matched the record's in every one. It is what makes latency, a real
    outcome and the provider's own call id reachable at all: the shared event
    schema carries none of them, and without an identifier per call there is
    nothing to attach them to. Occurrence keeps a resumed session's re-emitted
    record from lending its data to the original's already-disambiguated
    turn; the session id keeps a second session in the same file from lending
    its data to a uuid it merely happens to share with the first.
    """
    calls: list[ToolCall] = []
    for index, tool in enumerate(tools):
        server, name = _server_and_tool(tool)
        call_id = transcript.call_ids.get((raw_session_id, record_uuid, occurrence, index))
        withheld = id(tool) in compromised or (call_id is not None and call_id in reused_call_ids)
        if withheld:
            status: Status = "pending"
            result, size, error_text, truncated = None, None, None, False
            denial: str | None = None
        else:
            denial = transcript.denials.get(call_id) if call_id else None
            status = _status(tool, denial, transcript.errored.get(call_id) if call_id else None)
            if status == "pending":
                # `_status` returned `pending` from `tool.result is None` --
                # upstream's own snapshot of the file, taken in a separate read
                # from this transcript's own recovery pass below. A session
                # still being written can grow between the two reads, so the
                # recovery pass can see a record upstream's snapshot never had.
                # Trusting it here would attach a result to a call this same
                # status was just derived from *not* having one, an internally
                # contradictory call.
                result, truncated, size = None, False, None
            else:
                # The record's own body, whole, where the call could be
                # identified. Upstream's copy is a fallback, and it arrives
                # already abridged — so `truncated` is derived from whichever
                # body is actually carried.
                recorded = transcript.results.get(call_id) if call_id else None
                if recorded is not None:
                    result, truncated, size = recorded, False, len(recorded)
                else:
                    truncated, size = _truncation(tool.result)
                    result = tool.result
            error_text = tool.error
        # The connection log answers what the transcript cannot for an MCP
        # call: 129 of 130 measured carry `unknown` here despite every one
        # having returned. It only ever *fills a gap* -- a status the transcript
        # actually stated is evidence from the record itself and is never
        # overwritten by a second source.
        #
        # A withheld duplicate is a gap, not a statement. `pending` there does
        # not mean the transcript said nothing came back; it means *this reader*
        # declined to attribute a result it could not place (ADR-0002). The log
        # can place it -- by ordinal, already guarded by a per-`(server, tool)`
        # count -- so letting the outcome through is the same rule, not an
        # exception to it.
        #
        # It is also the case that matters most. A retry loop is duplicates by
        # definition, so without this the one shape where the outcome is most
        # informative is the one shape that never receives it: three identical
        # rejected calls reached consumers as three `pending` with the failure
        # stripped off, and the loop read as silence.
        outcome = mcp.take(server, name) if server is not None else None
        if outcome is not None and (status == "unknown" or withheld):
            # `ok is None` means the client reported the call still running and
            # nothing ever followed. That is not an outcome to assert, so the
            # call stays `pending` -- but the elapsed time below is the client's
            # own measure of how long it waited, which is what separates a hang
            # from a call that simply has not finished yet.
            if outcome.ok is not None:
                status = "ok" if outcome.ok else "error"
            else:
                status = "pending"
        # `started`/`ended` are keyed on the bare provider call id, with no
        # occurrence to disambiguate it -- exactly what made `call_id` itself
        # ambiguous for a withheld call. Reading them here would hand a
        # withheld call a duration measured off whichever of the colliding
        # calls wrote to that key last, the same silent misattribution its
        # result was already withheld to avoid.
        #
        # A call whose status is still `pending` from `tool.result is None`
        # gets the same treatment as withheld, for the same reason `result`
        # does above: `ended` can hold a value the recovery pass saw after
        # upstream's own snapshot did not, and a `pending` call reporting a
        # duration is the same contradiction as one reporting a result.
        duration = None if withheld or status == "pending" else _duration(call_id, transcript)
        if duration is None and outcome is not None:
            duration = outcome.duration_ms
        skill, plugin = transcript.attribution.get(
            (raw_session_id, record_uuid, occurrence), (None, None)
        )
        calls.append(
            ToolCall(
                span=span_id(session_id, turn_key, index),
                tool_name=name,
                mcp_server=server,
                status=status,
                arguments=dict(tool.arguments or {}),
                result=result,
                result_size=size,
                error_text=error_text,
                truncated=truncated,
                denial_kind=denial,
                provider_call_id=call_id,
                duration_ms=duration,
                working_directory=transcript.cwd_at.get((raw_session_id, record_uuid, occurrence)),
                attributed_skill=skill,
                attributed_plugin=plugin,
                transport=mcp.transport_for(outcome.server if outcome else None),
            )
        )
    return tuple(calls)


def _duration(call_id: str | None, transcript: _Transcript) -> int | None:
    """How long the call took, where both ends of it were recorded.

    Both ends, or nothing: a call whose result never came back has no duration,
    and a start alone would invite a consumer to treat *still running* as *fast*.
    Rounded to whole milliseconds, which is finer than the record's own precision
    warrants but avoids a float in a value consumers will sum.

    **This is wall clock, not tool latency, and for some tools that is the whole
    difference.** `AskUserQuestion` and `ExitPlanMode` block on a person: the
    longest measured on one machine is 24 hours, which is someone going to bed
    mid-session. The median across every call is 67ms. A consumer reading this as
    *how slow the tool is* will be wrong for exactly the tools that wait on a
    human, and it is named here rather than corrected, because the elapsed time
    is a true fact and which tools block on people is the consumer's to know.
    """
    if call_id is None:
        return None
    began, finished = transcript.started.get(call_id), transcript.ended.get(call_id)
    if began is None or finished is None:
        return None
    elapsed = (finished - began).total_seconds() * 1000
    # Records are written in the order events happened; a negative span would
    # mean the file contradicts itself, and inventing a zero would hide that.
    return round(elapsed) if elapsed >= 0 else None


def _server_and_tool(tool: ToolUsage) -> tuple[str | None, str]:
    """Prefer what upstream populated; recover the server from the name otherwise.

    Some parsers set `server_name`; the Claude one does not, leaving the server
    inside the tool name. Recovering it is this package's contract rather than an
    optional enrichment, because it is the key the whole downstream join uses.
    """
    server, name = split_tool_name("claude-code", tool.tool_name)
    if tool.server_name:
        return tool.server_name, name
    return server, name


def _status(tool: ToolUsage, denial: str | None, errored: bool | None) -> Status:
    """Normalise one outcome, asserting only what the surviving evidence supports.

    Every value here is **read**, never inferred. `pending` is structural: no
    result came back. `rejected` comes from the record's `toolDenialKind`.
    `ok` and `error` come from `is_error` on the result block — the agent's own
    statement about whether the call worked.

    `unknown` is what remains, and it is not a failure state. It means the call
    ran and returned and the record does not say how it went: `is_error` is
    absent on roughly a fifth of results. Claiming `ok` there would assert
    something unsupported, since a consumer reads `ok` as *worked*.

    Upstream's own `status` is deliberately not consulted. It reports `success`
    for a Claude Code call merely because a result exists — it never reads
    `is_error` — so passing it through would launder an absence of evidence into
    a claim of success. Its explicit `error`, and any `tool.error` text, are
    still honoured as a last resort where the record itself says nothing.
    """
    if tool.result is None:
        return "pending"
    if denial is not None:
        return "rejected"
    if errored is not None:
        return "error" if errored else "ok"
    if (tool.status or "").lower() == "error" or tool.error:
        return "error"
    return "unknown"


#: Upstream abridges a long result from the *middle*, leaving
#: `... [truncated N chars] ...` between two retained edges. The marker is
#: therefore never near either end, and it carries the count of what was
#: removed — which is what makes the original size recoverable.
_TRUNCATION_MARKER = re.compile(r"\.\.\. \[truncated (\d+) chars\] \.\.\.")


def _truncation(result: str | None) -> tuple[bool, int | None]:
    """Report whether a result was abridged upstream, and how large it really was.

    `result_size` is the size of what the agent actually produced, not the size
    of the abridged copy we hold: reporting the copy would understate every long
    result, and the marker upstream leaves behind makes the true figure exact
    rather than estimated.
    """
    if result is None:
        return False, None
    match = _TRUNCATION_MARKER.search(result)
    if match is None:
        return False, len(result)
    removed = int(match.group(1))
    return True, len(result) - len(match.group(0)) + removed


def _strip_source_prefix(session_id: str) -> str:
    """Upstream namespaces its own ids (`claude_<uuid>`); ours is the agent kind."""
    prefix = f"{SOURCE}_"
    return session_id.removeprefix(prefix)


@dataclass(frozen=True)
class _Transcript:
    """Everything one transcript records that the shared event schema drops.

    **One pass, not one per field.** These accreted as separate recoveries — a
    directory, then a refusal, then a permission mode — each walking the same
    file for its own key. Four reads of a transcript to build one session is a
    shape nobody chose; this is that shape corrected.

    What is recovered here is the transcript's own recorded values, never an
    inference from them. Where a value is absent it stays absent: a field this
    reader cannot establish is reported unknown rather than defaulted, because a
    plausible value is indistinguishable from a real one once it is in the model.
    """

    #: session id -> the working directory it recorded.
    cwds: dict[str, str]
    #: session id -> the agent build that wrote it.
    versions: dict[str, str]
    #: session id -> how the agent was driven (`cli`, `claude-vscode`, `sdk-cli`).
    entrypoints: dict[str, str]
    #: (session id, record uuid, occurrence) -> the permission mode in effect at
    #: that record. Occurrence-keyed for the same reason `call_ids` is: a
    #: resumed session can re-emit a record under the same uuid, and a plain
    #: uuid key would let the later occurrence's value silently answer for the
    #: earlier one. Session-keyed too: one file can hold more than one session
    #: (a resume mints a new session id but keeps writing to the same file),
    #: and a uuid is only guaranteed unique within the session that wrote it --
    #: a bare `(uuid, occurrence)` key lets one session's record silently
    #: answer for another's occurrence of the same uuid.
    permission_modes: dict[tuple[str, str, int], str]
    #: (session id, record uuid, occurrence, position within the message) ->
    #: the provider's call id. This is the association everything per-call
    #: rests on, and the (uuid, position) pair is the same one span identity is
    #: already built from. Verified against 1,063 real messages: upstream's
    #: tool order matches the record's in every one.
    #:
    #: Occurrence-keyed because a resumed session can re-emit a record under
    #: the same uuid (ADR-0001): without it, a single pass over the file would
    #: let the later occurrence's call id overwrite the earlier one's at the
    #: same `(uuid, position)` key, and the disambiguated turn built for the
    #: first occurrence would silently read the second occurrence's call id,
    #: result, duration, denial, working directory and attribution.
    #:
    #: Session-keyed for the same reason `permission_modes` is: occurrence is
    #: counted per uuid across the *whole file*, and a uuid is only promised
    #: unique within the session that wrote it. Without the session id in the
    #: key, a second session in the same file reusing a uuid the first session
    #: already used would read the first session's data at what looks, from
    #: the second session's own count, like occurrence zero.
    call_ids: dict[tuple[str, str, int, int], str]
    #: provider call id -> when the call was issued.
    started: dict[str, datetime]
    #: provider call id -> when its result came back.
    ended: dict[str, datetime]
    #: provider call id -> whether the result was an error. Absent where the
    #: record carries no `is_error`, which is not the same as False.
    errored: dict[str, bool]
    #: provider call id -> `toolDenialKind`, where the call was refused.
    denials: dict[str, str]
    #: provider call id -> the result body **as the agent recorded it**.
    #:
    #: Upstream abridges every result to 1,000 characters from the middle, which
    #: on one corpus held 22% of 56.5M characters and cut 38% of results — with
    #: the cap sitting *below* the median result size. The result body is where a
    #: consumer's evidence usually is, and middle-truncation removes exactly the
    #: middle while leaving both ends looking whole.
    #:
    #: Read here unabridged and deliberately unbounded. Measured cost for the
    #: entire corpus: 117MB peak RSS against an 18MB baseline. A cap can be added
    #: when the reported figure says it is needed; one chosen before then would
    #: be the same arbitrary threshold at a different number.
    results: dict[str, str]

    # Added after the fields above, with defaults, so a consumer or a test
    # that builds this record positionally is not broken by the reader
    # learning to recover one more thing.
    #: session id -> the branch checked out where it ran.
    branches: dict[str, str] = field(default_factory=dict)
    #: session id -> the request that started it, recorded apart from the turns
    #: and therefore surviving compaction, which the first user turn does not.
    initial_prompts: dict[str, str] = field(default_factory=dict)
    #: (session id, record uuid, occurrence) -> the directory *that record* ran
    #: in. Distinct from `cwds`, which is the session's first: a session can
    #: `cd`, and a relative path in a tool argument means nothing without the
    #: directory it was relative to. Session- and occurrence-keyed for the same
    #: reason `call_ids` is.
    cwd_at: dict[tuple[str, str, int], str] = field(default_factory=dict)
    #: (session id, record uuid, occurrence) -> (skill, plugin) the agent
    #: attributed the record to. Its own attribution, not an inference from
    #: names. Session- and occurrence-keyed for the same reason `call_ids` is.
    attribution: dict[tuple[str, str, int], tuple[str | None, str | None]] = field(
        default_factory=dict
    )
    #: session id -> material that reached the model outside the turn structure.
    context: dict[str, list[tuple[str, str | None, str]]] = field(default_factory=dict)
    #: session id -> compaction boundaries, as (trigger, pre, dropped).
    compactions: dict[str, list[tuple[str, int | None, int | None]]] = field(default_factory=dict)
    #: session id -> provider refusals, as (category, original, fallback).
    refusals: dict[str, list[tuple[str | None, str | None, str | None]]] = field(
        default_factory=dict
    )


def _recorded(path: Path) -> _Transcript:
    """Read one transcript once, for everything upstream drops.

    A best-effort read of a file upstream has already parsed: a line that will
    not decode is skipped rather than raised on, because the session it belongs
    to has been built successfully and losing it over a recovery pass would turn
    a missing field into a missing session.

    The permission mode is carried forward across records in file order within
    the session that declared it — it is declared on a turn and holds until the
    next declaration in that same session, and one file can hold more than one —
    while every other value is taken from the record that states it.
    """
    cwds: dict[str, str] = {}
    versions: dict[str, str] = {}
    entrypoints: dict[str, str] = {}
    branches: dict[str, str] = {}
    initial_prompts: dict[str, str] = {}
    cwd_at: dict[tuple[str, str, int], str] = {}
    attribution: dict[tuple[str, str, int], tuple[str | None, str | None]] = {}
    context: dict[str, list[tuple[str, str | None, str]]] = {}
    compactions: dict[str, list[tuple[str, int | None, int | None]]] = {}
    refusals: dict[str, list[tuple[str | None, str | None, str | None]]] = {}
    permission_modes: dict[tuple[str, str, int], str] = {}
    call_ids: dict[tuple[str, str, int, int], str] = {}
    started: dict[str, datetime] = {}
    ended: dict[str, datetime] = {}
    errored: dict[str, bool] = {}
    denials: dict[str, str] = {}
    results: dict[str, str] = {}
    modes: dict[str, str] = {}
    #: (session id, uuid) -> how many records with that uuid have been seen so
    #: far *in that session*. A resumed session can re-emit a record under the
    #: same uuid, and this is what keeps the re-emitted record's own data from
    #: overwriting the original's at the same key (ADR-0001).
    #:
    #: Session-keyed, not just uuid-keyed: one file can hold more than one
    #: session (a resume mints a new session id but keeps writing to the same
    #: file), and a record's uuid is only promised unique within the session
    #: that wrote it. Counting occurrences per uuid across the whole file would
    #: let a second session's record land at whatever occurrence number the
    #: *file-wide* count of that uuid happened to reach -- not the occurrence
    #: number its own session would compute independently when normalising
    #: that session's turns -- and read a stranger session's call id, result,
    #: permission mode, working directory and attribution instead of its own.
    uuid_occurrences: dict[tuple[str, str], int] = {}

    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue

                recorded_id = record.get("sessionId")
                session_id = recorded_id if isinstance(recorded_id, str) and recorded_id else None
                if session_id is not None:
                    for key, store in (
                        ("cwd", cwds),
                        ("version", versions),
                        ("entrypoint", entrypoints),
                        ("gitBranch", branches),
                    ):
                        value = record.get(key)
                        if isinstance(value, str) and value:
                            store.setdefault(session_id, value)
                    _recover_session_scoped(
                        record, session_id, initial_prompts, context, compactions, refusals
                    )

                declared = record.get("permissionMode")
                if session_id is not None and isinstance(declared, str) and declared:
                    modes[session_id] = declared
                mode = modes.get(session_id) if session_id is not None else None
                # A uuid is only promised unique within the session that wrote
                # it, and one file can hold more than one session -- so the
                # session id joins the uuid in every key below, not just in
                # `uuid_occurrences` itself.
                sid = session_id or ""
                uuid = record.get("uuid")
                occurrence = 0
                if isinstance(uuid, str) and uuid:
                    occurrence = uuid_occurrences.get((sid, uuid), 0)
                    uuid_occurrences[(sid, uuid)] = occurrence + 1
                if mode is not None and isinstance(uuid, str) and uuid:
                    permission_modes[(sid, uuid, occurrence)] = mode
                if isinstance(uuid, str) and uuid:
                    where = record.get("cwd")
                    if isinstance(where, str) and where:
                        cwd_at[(sid, uuid, occurrence)] = where
                    skill, plugin = (
                        record.get("attributionSkill"),
                        record.get("attributionPlugin"),
                    )
                    if isinstance(skill, str) or isinstance(plugin, str):
                        attribution[(sid, uuid, occurrence)] = (
                            skill if isinstance(skill, str) else None,
                            plugin if isinstance(plugin, str) else None,
                        )

                timestamp = _timestamp(record.get("timestamp"))
                denial = record.get("toolDenialKind")
                message = record.get("message")
                blocks = message.get("content") if isinstance(message, dict) else None
                position = 0
                for block in blocks if isinstance(blocks, list) else []:
                    if not isinstance(block, dict):
                        continue
                    kind = block.get("type")
                    identifier = block.get("id") if kind == "tool_use" else block.get("tool_use_id")
                    if not isinstance(identifier, str) or not identifier:
                        continue
                    if kind == "tool_use":
                        if isinstance(uuid, str) and uuid:
                            call_ids[(sid, uuid, occurrence, position)] = identifier
                        position += 1
                        if timestamp is not None:
                            started[identifier] = timestamp
                    elif kind == "tool_result":
                        if timestamp is not None:
                            ended[identifier] = timestamp
                        if isinstance(block.get("is_error"), bool):
                            errored[identifier] = block["is_error"]
                        if isinstance(denial, str) and denial in _DENIAL_KINDS:
                            denials[identifier] = denial
                        body = block.get("content")
                        text = body if isinstance(body, str) else json.dumps(body, default=str)
                        if text:
                            results[identifier] = text
    except OSError:
        pass

    return _Transcript(
        cwds=cwds,
        versions=versions,
        entrypoints=entrypoints,
        branches=branches,
        initial_prompts=initial_prompts,
        cwd_at=cwd_at,
        attribution=attribution,
        context=context,
        compactions=compactions,
        refusals=refusals,
        permission_modes=permission_modes,
        call_ids=call_ids,
        started=started,
        ended=ended,
        errored=errored,
        denials=denials,
        results=results,
    )


#: Attachment types that carry *material* into the conversation, mapped to the
#: provenance a consumer reasons about, together with the keys that hold their
#: body. Each type stores it differently, which is why this is a table rather
#: than one key name.
#:
#: Deliberately absent: `total_tokens_reminder` (12,522 of 15,653 real
#: attachments, and bookkeeping rather than content), and the mode records
#: `plan_mode` / `command_permissions` / `auto_mode`, which announce a state
#: change and carry no text. A record with nothing in it is not a provenance
#: class; carrying it would put empty rows on the surface a consumer scans.
_CONTEXT_SOURCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "hook_success": ("hook", ("stdout", "content")),
    "hook_additional_context": ("hook", ("additionalContext", "content", "stdout")),
    "skill_listing": ("skill_listing", ("content",)),
    "agent_listing_delta": ("agent_listing", ("addedLines",)),
    # An MCP server's own instructions, reaching the model directly. The
    # highest-value member of this table: a server's instruction block is
    # attacker-controllable in a supply-chain compromise and arrives as neither
    # a user turn nor a tool result.
    "mcp_instructions_delta": ("mcp_instructions", ("addedBlocks",)),
    "edited_text_file": ("file", ("snippet",)),
    "file": ("file", ("content",)),
    "nested_memory": ("memory", ("content",)),
    "compact_file_reference": ("file", ("content", "snippet")),
}

#: What names the item, by type.
_CONTEXT_NAMES = ("hookName", "filename", "path", "displayPath", "name")


def _body(value: object, depth: int = 0) -> str | None:
    """The text inside an attachment body, whatever shape it is stored in.

    Three shapes occur in real transcripts: a plain string; a list of strings
    (`addedLines`, `addedBlocks`); and a nested object whose own `content` holds
    the text (`file`, `nested_memory`). Bounded against a hostile nesting depth.
    """
    if depth > 4:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, list):
        parts = [text for item in value if (text := _body(item, depth + 1))]
        return "\n".join(parts) or None
    if isinstance(value, dict):
        for key in ("content", "text", "file"):
            found = _body(value.get(key), depth + 1)
            if found:
                return found
    return None


def _recover_session_scoped(
    record: dict[str, object],
    session_id: str,
    initial_prompts: dict[str, str],
    context: dict[str, list[tuple[str, str | None, str]]],
    compactions: dict[str, list[tuple[str, int | None, int | None]]],
    refusals: dict[str, list[tuple[str | None, str | None, str | None]]],
) -> None:
    """Everything recorded about a session rather than about one of its turns.

    Four record shapes upstream's event schema has no place for, each of which
    bounds or explains what the turns say: the initiating prompt (which survives
    compaction, unlike the first user turn), material injected outside the turn
    structure, the compaction boundaries themselves, and the provider declining.
    """
    kind = record.get("type")

    if kind == "last-prompt":
        prompt = record.get("lastPrompt")
        if isinstance(prompt, str) and prompt:
            initial_prompts.setdefault(session_id, prompt)
        return

    if kind == "attachment":
        attachment = record.get("attachment")
        if not isinstance(attachment, dict):
            return
        entry = _CONTEXT_SOURCES.get(str(attachment.get("type")))
        if entry is None:
            return
        source, body_keys = entry
        body = next(
            (text for key in body_keys if (text := _body(attachment.get(key)))),
            None,
        )
        if body is None:
            return
        name = next(
            (
                value
                for key in _CONTEXT_NAMES
                if isinstance(value := attachment.get(key), str) and value
            ),
            None,
        )
        context.setdefault(session_id, []).append((source, name, body))
        return

    if kind != "system":
        return
    subtype = record.get("subtype")
    if subtype == "compact_boundary":
        metadata = record.get("compactMetadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        compactions.setdefault(session_id, []).append(
            (
                str(metadata.get("trigger") or "unknown"),
                _count(metadata.get("preTokens")),
                _count(metadata.get("cumulativeDroppedTokens")),
            )
        )
    elif subtype in ("model_refusal_fallback", "model_consent_fallback"):
        refusals.setdefault(session_id, []).append(
            (
                _text(record.get("apiRefusalCategory")),
                _text(record.get("originalModel")),
                _text(record.get("fallbackModel")),
            )
        )


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _decode_project_directory(name: str) -> str | None:
    """`-Users-me-Projects-thing` -> `/Users/me/Projects/thing`, if it exists."""
    parts = [part for part in name.lstrip("-").split("-") if part]
    if not parts:
        return None
    current = Path("/")
    index = 0
    while index < len(parts):
        # Longest first: a directory whose own name contains a dash must win over
        # the shorter prefix that merely looks like one.
        for end in range(len(parts), index, -1):
            candidate = current / "-".join(parts[index:end])
            if candidate.is_dir():
                current, index = candidate, end
                break
        else:
            return None
    return str(current)


def _within_window(path: Path, window: Window) -> bool:
    if window.since is None:
        return True
    modified = datetime.fromtimestamp(path.stat().st_mtime, tz=window.since.tzinfo)
    return modified >= window.since
