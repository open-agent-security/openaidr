"""The MCP connection log: what it adds, and every way it declines to."""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openaidr.model import LOCAL_ONLY
from openaidr.readers.base import Window
from openaidr.readers.claude_code import ClaudeCodeReader
from openaidr.readers.claude_code_mcp import cache_roots, read_mcp_logs
from tests.fixtures import mcp_logs as log
from tests.fixtures.claude_jsonl import (
    assistant_tool_use,
    tool_result,
    user_text,
    write_session,
    write_subagent_session,
)

SESSION = "11111111-2222-3333-4444-555555555555"


def _transcript(
    root: Path, tools: list[str], project: str = "project", session_id: str = SESSION
) -> None:
    """A session calling `tools` over MCP, plus one built-in so it always parses.

    Upstream yields nothing for a transcript with no tool calls at all, so a
    connection-only test still needs one call to have a session to attach to.
    """
    records = [
        user_text(session_id, "u0", "2026-01-01T00:00:00Z", "go"),
        assistant_tool_use(session_id, "anchor", "2026-01-01T00:00:01Z", "toolu_anchor", "Read"),
        tool_result(session_id, "ranchor", "2026-01-01T00:00:02Z", "toolu_anchor", "x"),
    ]
    for index, name in enumerate(tools):
        call_id = f"toolu_{index}"
        records.append(
            assistant_tool_use(
                session_id,
                f"a{index}",
                "2026-01-01T00:00:01Z",
                call_id,
                f"mcp__books__{name}",
                # Distinct arguments per call: structurally identical calls
                # sharing a body are treated as withheld retries and arrive
                # `pending`, which would mask what this file is testing.
                arguments={"q": f"query-{index}"},
            )
        )
        records.append(
            tool_result(session_id, f"r{index}", "2026-01-01T00:00:02Z", call_id, "done")
        )
    write_session(root, project, records)


def _collect(root: Path, cache: Path | None, window: Window | None = None):
    index = read_mcp_logs((cache,) if cache is not None else (root / "absent",))
    sessions, _ = ClaudeCodeReader(root=root, mcp_logs=index).collect(window or Window(since=None))
    return sessions


def _mcp_calls(sessions):
    return [c for s in sessions for t in s.turns for c in t.tool_calls if c.mcp_server]


def test_the_log_answers_what_the_transcript_cannot(tmp_path: Path) -> None:
    """Transport, outcome and duration, none of which the transcript records.

    Measured on a real corpus, 129 of 130 MCP calls carried `unknown` despite
    every one of them having returned: a successful MCP call could not be told
    from a failed one.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search", "search"])
    log.write_server_log(
        cache,
        "books",
        [
            log.connected(SESSION, transport="stdio"),
            log.capabilities(SESSION, "book_toolkit", "1.4.0"),
            log.calling(SESSION, "search"),
            log.completed(SESSION, "search", ms=37),
            log.calling(SESSION, "search"),
            log.failed(SESSION, "search", ms=8),
        ],
    )
    (session,) = _collect(root, cache)
    assert session.mcp_log_state == "applied"
    first, second = _mcp_calls([session])
    assert (first.status, first.transport) == ("ok", "stdio")
    assert (second.status, second.transport) == ("error", "stdio")


def test_the_log_fills_a_duration_but_never_replaces_one(tmp_path: Path) -> None:
    """Two different measurements, and the transcript's is not overwritten.

    The transcript times the round trip from issue to result; the log times the
    tool's own execution. Where the transcript has an answer it keeps it, on the
    same rule as `status`: a second source is for the gaps, not for correcting
    evidence the record already carried.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"])
    log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION), log.completed(SESSION, "search", ms=37)],
    )
    (call,) = _mcp_calls(_collect(root, cache))
    assert call.duration_ms == 1000, "the transcript's own round-trip timing stands"


def test_the_ordinal_join_is_positional_within_a_tool(tmp_path: Path) -> None:
    """The log carries no span and no call id, so position is the only identity.

    Two calls to the same tool with different outcomes is the case that proves
    the join is ordered rather than merely present.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search", "search", "search"])
    log.write_server_log(
        cache,
        "books",
        [
            log.connected(SESSION),
            log.failed(SESSION, "search"),
            log.completed(SESSION, "search"),
            log.failed(SESSION, "search"),
        ],
    )
    calls = _mcp_calls(_collect(root, cache))
    assert [c.status for c in calls] == ["error", "ok", "error"]


def test_overlapping_calls_to_the_same_tool_withhold_their_ordinal_outcomes(
    tmp_path: Path,
) -> None:
    """The log carries no id per call, so the ordinal join trusts that the Nth
    call to complete is the Nth call the transcript issued -- true only while
    calls to that key run one at a time. Two calls to `search` overlap here
    (the second `Calling` line arrives before the first call's outcome), and
    they complete in the opposite order from how the transcript issued them:
    trusting completion order would swap their status and duration."""
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search", "search"])
    log.write_server_log(
        cache,
        "books",
        [
            log.connected(SESSION, transport="stdio"),
            log.calling(SESSION, "search"),
            log.calling(SESSION, "search"),
            log.completed(SESSION, "search", ms=5),
            log.failed(SESSION, "search", ms=20),
        ],
    )
    (session,) = _collect(root, cache)
    assert session.mcp_log_state == "applied"
    assert session.mcp_overlap_withheld == 2, "reported, not just silently dropped"
    calls = _mcp_calls([session])
    assert [c.status for c in calls] == ["unknown", "unknown"]
    assert [c.transport for c in calls] == ["stdio", "stdio"], (
        "transport is a connection fact, not an ordinal one -- withholding the "
        "ambiguous status and duration should not also withhold this"
    )
    (connection,) = session.mcp_connections
    assert connection.transport == "stdio", "the connection fact is unaffected"


def test_a_count_disagreement_withholds_outcomes_and_keeps_connections(tmp_path: Path) -> None:
    """The guard, and why it is not simply "use what is there".

    The log has one outcome for two calls. Matching in order would attach it to
    the first, when it may have been the second's -- and every later call in the
    session would shift with it, with nothing to show for it. Transport and
    advertised identity survive: they are properties of the connection, not of
    any one call, so a miscounted sequence does not touch them.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search", "search"])
    log.write_server_log(
        cache,
        "books",
        [
            log.connected(SESSION, transport="stdio"),
            log.capabilities(SESSION, "book_toolkit", "1.4.0"),
            log.completed(SESSION, "search"),
        ],
    )
    (session,) = _collect(root, cache)
    assert session.mcp_log_state == "count_mismatch"
    assert [c.status for c in _mcp_calls([session])] == ["unknown", "unknown"]
    assert [c.transport for c in _mcp_calls([session])] == [None, None]
    (connection,) = session.mcp_connections
    assert connection.transport == "stdio"
    assert (connection.advertised_name, connection.advertised_version) == ("book_toolkit", "1.4.0")


def test_a_tool_only_the_client_called_does_not_trip_the_guard(tmp_path: Path) -> None:
    """Claude Code makes MCP calls of its own that are not agent tool calls.

    `closeAllDiffTabs` and `getDiagnostics` against the IDE server appear in the
    log and never in a transcript. Outcomes are queued and popped by tool name,
    so a tool the transcript never calls cannot shift one it does -- comparing
    whole count maps would read routine client traffic as corruption.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"])
    log.write_server_log(
        cache,
        "books",
        [
            log.connected(SESSION),
            log.completed(SESSION, "closeAllDiffTabs"),
            log.completed(SESSION, "search"),
            log.completed(SESSION, "getDiagnostics"),
        ],
    )
    (session,) = _collect(root, cache)
    assert session.mcp_log_state == "applied"
    assert [c.status for c in _mcp_calls([session])] == ["ok"]


def test_two_servers_sharing_a_tool_name_keep_their_own_outcomes(tmp_path: Path) -> None:
    """MCP servers own their schemas, so one tool name can belong to two of them.

    The join key is `(server, tool)` throughout, because the alternative is not
    a missing outcome but the wrong one: `books/search` reported with the status,
    duration and transport that belong to `tickets/search`. Counting per tool
    alone would also let the two servers' totals cover for each other, so a
    pruned log on one side could pass the guard on the strength of the other.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    records = [
        user_text(SESSION, "u0", "2026-01-01T00:00:00Z", "go"),
        assistant_tool_use(SESSION, "a0", "2026-01-01T00:00:01Z", "toolu_0", "mcp__books__search"),
        tool_result(SESSION, "r0", "2026-01-01T00:00:02Z", "toolu_0", "done"),
        assistant_tool_use(
            SESSION, "a1", "2026-01-01T00:00:03Z", "toolu_1", "mcp__tickets__search"
        ),
        tool_result(SESSION, "r1", "2026-01-01T00:00:04Z", "toolu_1", "done"),
    ]
    write_session(root, "project", records)
    # Written tickets-first, so a name-only queue would hand the `books` call
    # the `tickets` failure purely on directory traversal order.
    log.write_server_log(
        cache,
        "tickets",
        [
            log.connected(SESSION, transport="sse"),
            log.failed(SESSION, "search", ms=8),
        ],
    )
    log.write_server_log(
        cache,
        "books",
        [
            log.connected(SESSION, transport="stdio"),
            log.completed(SESSION, "search", ms=37),
        ],
    )
    (session,) = _collect(root, cache)
    assert session.mcp_log_state == "applied"
    books, tickets = _mcp_calls([session])
    assert (books.mcp_server, books.status, books.transport) == ("books", "ok", "stdio")
    assert (tickets.mcp_server, tickets.status, tickets.transport) == ("tickets", "error", "sse")


def test_one_server_of_two_missing_its_log_withholds_both(tmp_path: Path) -> None:
    """Aggregate counts agreeing across servers is not the same as each agreeing.

    `books` logged two calls and `tickets` none; a per-tool total of two would
    match the transcript's two and admit a join that places both of `books`'s
    outcomes, one of them onto a `tickets` call.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    records = [
        user_text(SESSION, "u0", "2026-01-01T00:00:00Z", "go"),
        assistant_tool_use(SESSION, "a0", "2026-01-01T00:00:01Z", "toolu_0", "mcp__books__search"),
        tool_result(SESSION, "r0", "2026-01-01T00:00:02Z", "toolu_0", "done"),
        assistant_tool_use(
            SESSION, "a1", "2026-01-01T00:00:03Z", "toolu_1", "mcp__tickets__search"
        ),
        tool_result(SESSION, "r1", "2026-01-01T00:00:04Z", "toolu_1", "done"),
    ]
    write_session(root, "project", records)
    log.write_server_log(
        cache,
        "books",
        [
            log.connected(SESSION),
            log.completed(SESSION, "search"),
            log.completed(SESSION, "search"),
        ],
    )
    (session,) = _collect(root, cache)
    assert session.mcp_log_state == "count_mismatch"
    assert [c.status for c in _mcp_calls([session])] == ["unknown", "unknown"]


def test_a_status_the_transcript_states_is_never_overwritten(tmp_path: Path) -> None:
    """The log only ever resolves an `unknown`.

    A status read from the record is evidence from the session itself; a second
    source disagreeing with it is a reason to keep the first, not to replace it.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    records = [
        user_text(SESSION, "u0", "2026-01-01T00:00:00Z", "go"),
        assistant_tool_use(SESSION, "a0", "2026-01-01T00:00:01Z", "toolu_0", "mcp__books__search"),
        tool_result(SESSION, "r0", "2026-01-01T00:00:02Z", "toolu_0", "boom", is_error=True),
    ]
    write_session(root, "project", records)
    log.write_server_log(cache, "books", [log.connected(SESSION), log.completed(SESSION, "search")])
    (call,) = _mcp_calls(_collect(root, cache))
    assert call.status == "error"


def test_an_unresolved_log_does_not_downgrade_a_transcript_proven_return(tmp_path: Path) -> None:
    """A `still running` outcome cannot un-prove a return the transcript itself recorded.

    The log is snapshotted once, before any transcript is read, so it can catch a
    call still in flight that has a result on disk by the time this transcript is
    parsed. `unknown` there is not silence -- it is the record proving the call
    returned, with no `is_error` to say how. Downgrading that to `pending` would
    be the log overwriting evidence rather than filling the gap it left.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"])
    log.write_server_log(
        cache,
        "books",
        [
            log.connected(SESSION),
            log.calling(SESSION, "search"),
            log.still_running(SESSION, "search"),
        ],
    )
    (call,) = _mcp_calls(_collect(root, cache))
    assert call.status == "unknown"


def test_a_still_running_outcome_gives_no_duration_to_a_pending_call(tmp_path: Path) -> None:
    """`still running` is an elapsed wait, not a round trip -- it must not become one.

    The transcript itself has no result for this call, so its status is
    genuinely `pending`, not merely `unknown`. The log's `still running` line
    is the client's last word before the snapshot was taken, and carries only
    how long it had waited so far -- not how long the call actually took. That
    must not stand in as `duration_ms` for a call the transcript never proved
    returned.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    records = [
        user_text(SESSION, "u0", "2026-01-01T00:00:00Z", "go"),
        assistant_tool_use(SESSION, "a0", "2026-01-01T00:00:01Z", "toolu_0", "mcp__books__search"),
    ]
    write_session(root, "project", records)
    log.write_server_log(
        cache,
        "books",
        [
            log.connected(SESSION),
            log.calling(SESSION, "search"),
            log.still_running(SESSION, "search"),
        ],
    )
    (call,) = _mcp_calls(_collect(root, cache))
    assert call.status == "pending"
    assert call.duration_ms is None


def test_a_completed_log_outcome_resolves_a_call_the_transcript_has_not_written_yet(
    tmp_path: Path,
) -> None:
    """`pending` is a gap, not a statement -- the log can still fill it.

    The transcript has no tool-result record for this call at all, so its
    status is genuinely `pending`, the same starting point as
    `test_a_still_running_outcome_gives_no_duration_to_a_pending_call`. But here
    the log's own line for this call is `completed`, not `still running`: the
    ordinal join -- already guarded by the per-`(server, tool)` count agreement
    -- identifies the call independently of whether the transcript has caught up
    yet, so the outcome and its real round-trip duration must not be withheld
    just because this reader's other reads have not seen a result.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    records = [
        user_text(SESSION, "u0", "2026-01-01T00:00:00Z", "go"),
        assistant_tool_use(SESSION, "a0", "2026-01-01T00:00:01Z", "toolu_0", "mcp__books__search"),
    ]
    write_session(root, "project", records)
    log.write_server_log(
        cache,
        "books",
        [
            log.connected(SESSION),
            log.calling(SESSION, "search"),
            log.completed(SESSION, "search", ms=37),
        ],
    )
    (call,) = _mcp_calls(_collect(root, cache))
    assert call.status == "ok"
    assert call.duration_ms == 37


def test_no_cache_directory_is_reported_not_assumed_empty(tmp_path: Path) -> None:
    """A machine we cannot read and a machine running no MCP must differ.

    Reported as a state rather than an absence, because a wrong cache path
    would otherwise make every transport `None` and read exactly like a fleet
    with no remote servers at all.
    """
    root = tmp_path / "projects"
    _transcript(root, ["search"])
    index = read_mcp_logs((root / "absent",))
    assert index.root_unreadable is False
    sessions, _ = ClaudeCodeReader(root=root, mcp_logs=index).collect(Window(since=None))
    (session,) = sessions
    assert session.mcp_log_state == "no_log_root"
    assert session.mcp_connections == ()
    assert [c.status for c in _mcp_calls([session])] == ["unknown"]


def test_an_inaccessible_mcp_cache_root_is_reported_as_a_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """A cache root that exists but cannot be statted -- a permission or
    transient filesystem failure -- is not the same as one that was never
    configured. `Path.is_dir()` cannot tell them apart: before Python 3.14 it
    propagates some `OSError`s and swallows others, and from 3.14 it swallows
    every `OSError` and reports `False`, indistinguishable from a root that
    was never configured -- which would otherwise silently withhold MCP
    enrichment from every session with nothing to show for it.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    cache.mkdir()
    _transcript(root, ["search"])
    real_stat = Path.stat

    def flaky_stat(self: Path, *args: object, **kwargs: object):
        if self == cache:
            raise OSError("permission denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    index = read_mcp_logs((cache,))
    assert index.root_found is False
    assert index.root_unreadable is True
    assert index.unreadable == (str(cache),)

    sessions, failures = ClaudeCodeReader(root=root, mcp_logs=index).collect(Window(since=None))
    assert (sessions[0].mcp_log_state, [c.status for c in _mcp_calls(sessions)]) == (
        "log_root_unreadable",
        ["unknown"],
    )
    assert any(str(cache) in f.message and "could not be read" in f.message for f in failures)


def test_a_non_directory_mcp_cache_root_is_reported_as_a_failure(tmp_path: Path) -> None:
    """A cache root that exists as a regular file (or a symlink to one) is not
    the same as one that was never configured -- the same falsehood-as-absence
    gap the inaccessible-root case above closes, one branch over: `is_dir` is
    simply `False` here, with no `OSError` to catch it.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    cache.write_text("not a directory")
    _transcript(root, ["search"])

    index = read_mcp_logs((cache,))
    assert index.root_found is False
    assert index.root_unreadable is True
    assert index.unreadable == (str(cache),)

    sessions, failures = ClaudeCodeReader(root=root, mcp_logs=index).collect(Window(since=None))
    assert (sessions[0].mcp_log_state, [c.status for c in _mcp_calls(sessions)]) == (
        "log_root_unreadable",
        ["unknown"],
    )
    assert any(str(cache) in f.message and "could not be read" in f.message for f in failures)


def test_a_log_directory_the_walk_cannot_scan_is_reported_and_withholds_enrichment(
    tmp_path: Path, monkeypatch
) -> None:
    """`Path.glob()` suppresses every `OSError` raised while scanning the
    filesystem as of Python 3.13, so a server's log directory this process
    cannot list would otherwise vanish from `**/mcp-logs-*` with no trace, and
    the session that called it would read as `no_log_for_session` -- the same
    as a pruned cache -- rather than as data withheld. Reading walks the tree
    itself (`os.walk`) rather than globbing it, so the directory it could not
    scan is at least named as a failure.

    Withheld whole, not merely per-call: a directory that could not be scanned
    yields no file names, so nothing establishes that the entries this read
    *did* find are every entry filed under this session id -- the precondition
    `for_session`'s single-match fast path rests on (ADR-0009). That is an
    identity doubt rather than a count disagreement, and identity doubt takes
    the connections with it, exactly as a cache-side collision already does.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"], project="-work-project")
    blocked = log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
    ).parent
    real_scandir = os.scandir

    def flaky_scandir(path="."):
        if path == str(blocked):
            raise PermissionError(13, "Permission denied", str(blocked))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", flaky_scandir)
    index = read_mcp_logs((cache,))
    assert any(str(blocked) in entry for entry in index.unreadable)
    assert index.discovery_incomplete

    sessions, failures = ClaudeCodeReader(root=root, mcp_logs=index).collect(Window(since=None))
    assert sessions[0].mcp_log_state == "log_discovery_incomplete"
    assert sessions[0].mcp_connections == ()
    assert any(str(blocked) in f.message for f in failures)


def test_a_session_with_no_log_is_distinguished_from_one_with_no_mcp(tmp_path: Path) -> None:
    """The cache is pruned on the agent's schedule, not ours."""
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"])
    log.write_server_log(cache, "books", [log.connected("some-other-session")])
    (session,) = _collect(root, cache)
    assert session.mcp_log_state == "no_log_for_session"


def test_a_session_that_never_touched_mcp_is_not_a_gap(tmp_path: Path) -> None:
    root, cache = tmp_path / "projects", tmp_path / "cache"
    write_session(
        root,
        f"{SESSION}.jsonl",
        [
            user_text(SESSION, "u0", "2026-01-01T00:00:00Z", "go"),
            assistant_tool_use(SESSION, "a0", "2026-01-01T00:00:01Z", "toolu_0", "Read"),
            tool_result(SESSION, "r0", "2026-01-01T00:00:02Z", "toolu_0", "x"),
        ],
    )
    log.write_server_log(cache, "books", [log.connected("other")])
    (session,) = _collect(root, cache)
    assert session.mcp_log_state == "applied"


def test_a_subagent_session_does_not_inherit_the_parents_mcp_log(tmp_path: Path) -> None:
    """A subagent's records carry the *parent's* session id (ADR-0001), and so
    does the MCP log's own `sessionId` tag -- the same collision the session
    identity works around. Applying the log to a subagent by that shared id
    would hand it the parent's connections and, wherever per-tool counts
    happen to agree, an outcome that was never necessarily its own."""
    root, cache = tmp_path / "projects", tmp_path / "cache"
    write_session(
        root,
        "-p",
        [
            user_text(SESSION, "u0", "2026-01-01T00:00:00Z", "go"),
            assistant_tool_use(SESSION, "a0", "2026-01-01T00:00:01Z", "toolu_0", "Read"),
            tool_result(SESSION, "r0", "2026-01-01T00:00:02Z", "toolu_0", "x"),
        ],
    )
    write_subagent_session(
        root,
        "-p",
        SESSION,
        "abc123",
        [
            user_text(SESSION, "su0", "2026-01-01T00:00:03Z", "delegated"),
            assistant_tool_use(
                SESSION, "sa0", "2026-01-01T00:00:04Z", "toolu_1", "mcp__books__search"
            ),
            tool_result(SESSION, "sr0", "2026-01-01T00:00:05Z", "toolu_1", "done"),
        ],
    )
    log.write_server_log(
        cache,
        "books",
        [
            log.connected(SESSION, transport="stdio"),
            log.calling(SESSION, "search"),
            log.completed(SESSION, "search", ms=37),
        ],
    )
    index = read_mcp_logs((cache,))
    sessions, _ = ClaudeCodeReader(root=root, mcp_logs=index).collect(Window(since=None))
    subagent = next(s for s in sessions if s.session_id == f"claude-code:{SESSION}:agent-abc123")
    assert subagent.mcp_log_state == "not_attempted"
    assert subagent.mcp_connections == ()
    (call,) = [c for t in subagent.turns for c in t.tool_calls if c.mcp_server]
    assert call.status == "unknown"
    assert call.transport is None


def test_a_failed_connection_carries_a_category_and_keeps_its_words_local(tmp_path: Path) -> None:
    """The category acts, the detail stays.

    A failure message is free text an MCP server author chose and can hold a
    URL or a header fragment, so it is `LOCAL_ONLY` while the category travels.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, [])
    log.write_server_log(
        cache,
        "books",
        [
            log.http_transport(SESSION, "https://api.example.test/mcp/"),
            log.connect_failed(SESSION, 232, "400", "bad request: Authorization header is bad"),
        ],
    )
    (session,) = _collect(root, cache)
    (connection,) = session.mcp_connections
    assert connection.connected is False
    assert connection.failure_category == "auth"
    assert connection.endpoint == "https://api.example.test/mcp/"
    assert "Authorization" in (connection.failure_detail or "")
    assert "failure_detail" in LOCAL_ONLY


def test_an_unresolved_connection_state_stays_unknown(tmp_path: Path) -> None:
    """A capabilities or endpoint line alone is not an observed outcome.

    Defaulting to `False` would assert the connection failed on evidence that
    is merely absent -- the handshake may still be in progress, or the log may
    state something in prose these patterns do not recognise.
    """
    cache = tmp_path / "cache"
    log.write_server_log(
        cache, "books", [log.http_transport(SESSION, "https://api.example.test/mcp/")]
    )
    index = read_mcp_logs((cache,))
    logs, _, _ = index.for_session(SESSION, "-work-project")
    assert logs is not None
    connection = logs.connections["books"]
    assert connection.connected is None
    assert connection.endpoint == "https://api.example.test/mcp/"


def test_a_later_success_clears_an_earlier_failed_attempts_category(tmp_path: Path) -> None:
    """A retried connection can fail before it comes up.

    The final state is the last line, not the union of every line that came
    before it -- otherwise the connection reports success and failure at once,
    and a summary counts it among the failures on the strength of a category
    that no longer describes anything.
    """
    cache = tmp_path / "cache"
    log.write_server_log(
        cache,
        "books",
        [log.connect_failed(SESSION, 10, "503", "upstream is unwell"), log.connected(SESSION)],
    )
    index = read_mcp_logs((cache,))
    logs, _, _ = index.for_session(SESSION, "-work-project")
    assert logs is not None
    connection = logs.connections["books"]
    assert connection.connected is True
    assert connection.failure_category is None
    assert connection.failure_detail is None


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        ("401", "nope", "auth"),
        (None, "request timed out", "timeout"),
        ("503", "upstream is unwell", "http_status"),
        (None, "connect ECONNREFUSED 127.0.0.1:8080", "network"),
        (None, "jsonrpc parse error", "protocol"),
        (None, "something else entirely", "unknown"),
    ],
)
def test_failure_categories(tmp_path: Path, status: str | None, detail: str, expected: str) -> None:
    cache = tmp_path / "cache"
    log.write_server_log(cache, "books", [log.connect_failed(SESSION, 10, status, detail)])
    index = read_mcp_logs((cache,))
    logs, _, _ = index.for_session(SESSION, "-work-project")
    assert logs is not None
    connection = logs.connections["books"]
    assert connection.failure_category == expected


def test_a_torn_final_line_does_not_lose_the_file(tmp_path: Path) -> None:
    """The log is appended to live, so a half-written last record is ordinary."""
    cache = tmp_path / "cache"
    path = log.write_server_log(cache, "books", [log.connected(SESSION)])
    path.write_text(path.read_text() + '{"debug": "Tool ', encoding="utf-8")
    index = read_mcp_logs((cache,))
    logs, _, _ = index.for_session(SESSION, "-work-project")
    assert logs is not None
    assert logs.connections["books"].transport == "stdio"


def test_an_undecodable_log_is_reported_not_silently_dropped(tmp_path: Path) -> None:
    """`MCPLogIndex.unreadable` used to name the file and nothing read it.

    A session that called an MCP tool over `books` would report
    `no_log_for_session` -- the same state a genuinely pruned cache produces --
    with no sign that the real cause was a file present but undecodable.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"])
    path = log.write_server_log(cache, "books", [log.connected(SESSION)])
    path.write_bytes(b"\xff\xfe not valid utf-8")
    index = read_mcp_logs((cache,))
    assert index.unreadable == (str(path),)
    _sessions, failures = ClaudeCodeReader(root=root, mcp_logs=index).collect(Window(since=None))
    assert any(str(path) in f.message for f in failures)


def test_an_unreadable_file_withholds_outcomes_for_its_whole_server(tmp_path: Path) -> None:
    """A server's log can be split across more than one file, one per run.

    Here the readable file alone already has a count that matches what the
    transcript expects -- exactly the coincidence that makes a partial read
    dangerous to trust: the unreadable second file's own share of the log was
    never counted on either side, so a guard built only from what could be
    opened has no way to tell "this really is the whole log" from "this only
    looks complete." Connection facts, which do not depend on the ordinal
    count, are unaffected.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"])
    log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
    )
    directory = cache / "-work-project" / "mcp-logs-books"
    (directory / "2026-01-02T00-00-00-000Z.jsonl").write_bytes(b"\xff\xfe not valid utf-8")

    index = read_mcp_logs((cache,))
    assert index.incomplete_project_servers == frozenset({("-work-project", "books")})

    (session,) = _collect(root, cache)
    assert session.mcp_log_state == "count_mismatch"
    assert [c.status for c in _mcp_calls([session])] == ["unknown"]
    (connection,) = session.mcp_connections
    assert connection.transport == "stdio"


def test_the_cache_root_can_be_overridden(tmp_path: Path, monkeypatch) -> None:
    """Which is what lets this be tested without a real cache on the machine."""
    monkeypatch.setenv("CLAUDE_CLI_CACHE_DIR", str(tmp_path))
    assert cache_roots() == (tmp_path,)


def test_a_reused_reader_does_not_serve_a_stale_mcp_log_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    """The MCP log is append-only the same way a transcript is, so a reader kept
    alive across repeated `collect()` calls (the steady-state design) must see a
    connection or outcome the log gained since the last pass. Only an *injected*
    index -- what makes a test independent of a real cache -- is meant to survive
    unchanged for the reader's whole lifetime; the default must be reloaded every
    pass rather than cached from the first `collect()` call forever."""
    root, cache = tmp_path / "projects", tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setenv("CLAUDE_CLI_CACHE_DIR", str(cache))
    _transcript(root, ["search"])

    reader = ClaudeCodeReader(root=root)
    first_sessions, _ = reader.collect(Window(since=None))
    (first,) = first_sessions
    assert first.mcp_log_state == "no_log_for_session"
    (first_call,) = _mcp_calls([first])
    assert first_call.status == "unknown"
    assert first_call.transport is None

    log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
    )
    second_sessions, _ = reader.collect(Window(since=None))
    (second,) = second_sessions
    assert second.mcp_log_state == "applied"
    (second_call,) = _mcp_calls([second])
    assert second_call.status == "ok"
    assert second_call.transport == "stdio"


def test_the_json_document_withholds_the_failure_detail(tmp_path: Path) -> None:
    from openaidr.collector import Collection
    from openaidr.render import render_json

    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, [])
    log.write_server_log(
        cache,
        "books",
        [log.connect_failed(SESSION, 10, "401", "token sk-secret-value rejected")],
    )
    sessions = _collect(root, cache)
    document = json.loads(render_json(Collection(sessions=list(sessions))))
    blob = json.dumps(document)
    assert "sk-secret-value" not in blob
    assert '"failure_category": "auth"' in blob


def test_one_session_id_under_two_project_directories_withholds_rather_than_merges(
    tmp_path: Path,
) -> None:
    """The cache files logs under a mangled *project* directory, not under a
    session, so a session id is only unique within one of them. A copied,
    restored or independently rooted project can put the same raw session id
    under two -- the collision the collector already reports for transcripts --
    and a session-only index merges both projects' logs into one entry. The
    retained session would then carry another project's connections and, where
    per-tool counts happen to coincide, its outcomes."""
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"], project="-unmangled-elsewhere")
    log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
        project="-work-project",
    )
    log.write_server_log(
        cache,
        "tickets",
        [log.connected(SESSION, transport="sse")],
        project="-other-project",
    )
    (session,) = _collect(root, cache)
    assert session.mcp_log_state == "session_id_collision"
    assert session.mcp_connections == (), "neither project's connections can be attributed"
    assert [c.status for c in _mcp_calls([session])] == ["unknown"]
    assert [c.transport for c in _mcp_calls([session])] == [None]


def test_a_collided_session_id_uses_the_log_filed_under_its_own_project(
    tmp_path: Path,
) -> None:
    """Withholding is the fallback, not the rule. Where the transcript's own
    project directory names one of the colliding cache directories, that is
    which log belongs to it, and the enrichment is neither ambiguous nor
    withheld."""
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"], project="-work-project")
    log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
        project="-work-project",
    )
    log.write_server_log(
        cache,
        "tickets",
        [log.connected(SESSION, transport="sse")],
        project="-other-project",
    )
    (session,) = _collect(root, cache)
    assert session.mcp_log_state == "applied"
    assert [c.server for c in session.mcp_connections] == ["books"]
    assert [c.status for c in _mcp_calls([session])] == ["ok"]


def test_two_transcript_projects_sharing_a_session_id_both_withhold(tmp_path: Path) -> None:
    """The collision the collector reports (ADR-0001) can happen on the
    *transcript* side too: a copied or restored project puts the same raw
    session id under two transcript project directories. `for_session`'s
    single-match fast path (ADR-0004) would otherwise hand its one cache-side
    log entry to whichever transcript asks first -- here, `-project-one`'s,
    since its per-tool count happens to agree -- even though nothing says the
    log is that transcript's rather than `-project-two`'s. Neither transcript's
    own project can be shown to be the log's, so both withhold."""
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"], project="-project-one")
    _transcript(root, ["search"], project="-project-two")
    log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
        project="-project-one",
    )
    sessions = _collect(root, cache)
    assert len(sessions) == 2
    assert {s.mcp_log_state for s in sessions} == {"session_id_collision"}
    assert all(s.mcp_connections == () for s in sessions)
    assert [c.status for c in _mcp_calls(sessions)] == ["unknown", "unknown"]


def test_two_transcript_files_in_the_same_project_sharing_a_session_id_both_withhold(
    tmp_path: Path,
) -> None:
    """The transcript-side collision `_ambiguous_transcript_ids` guards against
    is not only a two-*project* scenario. A restored or manually copied
    transcript can land beside the original under a different filename in the
    *same* project directory, still carrying the original's raw session id.
    Keying the check on project name rather than the file itself would
    collapse both files into one project entry and miss exactly this case."""
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"], project="-project-one")
    original = root / "-project-one" / f"{SESSION}.jsonl"
    shutil.copy(original, original.with_name(f"{SESSION}-restored.jsonl"))
    log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
        project="-project-one",
    )
    sessions = _collect(root, cache)
    assert len(sessions) == 2
    assert {s.mcp_log_state for s in sessions} == {"session_id_collision"}
    assert all(s.mcp_connections == () for s in sessions)
    assert [c.status for c in _mcp_calls(sessions)] == ["unknown", "unknown"]


def test_a_transcript_outside_the_window_still_claims_its_raw_session_id(
    tmp_path: Path,
) -> None:
    """The window bounds which sessions are returned, not which files claim an id.

    `--since` defaults to 14d, so the common case is that some transcript
    sharing a raw session id has an mtime outside the window while its twin
    has one inside it. If the excluded file is not counted as a claimant, the
    survivor looks like the id's only holder, and ADR-0004's single-match fast
    path hands it a log that may be the excluded file's.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"], project="-project-one")
    _transcript(root, ["search"], project="-project-two")
    stale = root / "-project-two" / f"{SESSION}.jsonl"
    os.utime(stale, (0, 0))
    log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
        project="-project-one",
    )
    window = Window(since=datetime(2026, 1, 1, tzinfo=UTC))
    sessions = _collect(root, cache, window)
    assert len(sessions) == 1, "the excluded file is still excluded from the result"
    assert sessions[0].mcp_log_state == "session_id_collision"
    assert sessions[0].mcp_connections == ()
    assert [c.status for c in _mcp_calls(sessions)] == ["unknown"]


def test_an_in_window_transcript_that_fails_to_parse_still_claims_its_raw_session_id(
    tmp_path: Path,
) -> None:
    """A file this pass tried and failed to parse is claimant by filename, the
    same as one the window excluded (ADR-0008).

    Unlike an out-of-window file, this one *was* in scope for the pass -- it
    was simply unreadable, e.g. a copy corrupted in transit. Dropping it from
    the claimant set entirely, rather than treating it like the out-of-window
    case, would let the readable twin look like the id's sole holder and hand
    it a log that may be the corrupted file's.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"], project="-project-one")
    broken = root / "-project-two" / f"{SESSION}.jsonl"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"\xff\xfe not valid utf-8 at all")
    log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
        project="-project-one",
    )
    sessions, failures = ClaudeCodeReader(root=root, mcp_logs=read_mcp_logs((cache,))).collect(
        Window(since=None)
    )
    assert len(failures) == 1 and str(broken) in failures[0].message
    assert len(sessions) == 1, "the unreadable file never becomes a session of its own"
    assert sessions[0].mcp_log_state == "session_id_collision"
    assert sessions[0].mcp_connections == ()
    assert [c.status for c in _mcp_calls(sessions)] == ["unknown"]


def test_a_transcript_gone_during_the_window_check_is_not_a_claimant(
    tmp_path: Path, monkeypatch
) -> None:
    """A `FileNotFoundError` from the window check, unlike any other `OSError`,
    proves the file no longer exists -- there is nothing left to claim its raw
    session id, so it must not manufacture a collision with a genuinely sole
    claimant."""
    from openaidr.readers import claude_code

    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"], project="-project-one")
    gone = root / "-project-two" / f"{SESSION}.jsonl"
    gone.parent.mkdir(parents=True, exist_ok=True)
    gone.write_text("{}\n")
    log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
        project="-project-one",
    )

    real_within_window = claude_code._within_window

    def flaky_within_window(path: Path, window: Window) -> bool:
        if path == gone:
            raise FileNotFoundError("vanished")
        return real_within_window(path, window)

    monkeypatch.setattr(claude_code, "_within_window", flaky_within_window)
    sessions = _collect(root, cache)
    assert len(sessions) == 1, "the vanished file never becomes a session of its own"
    assert sessions[0].mcp_log_state == "applied"


def test_an_in_window_transcript_whose_window_check_fails_still_claims_its_raw_session_id(
    tmp_path: Path, monkeypatch
) -> None:
    """A file whose window check raises is claimant by filename, the same as
    one that failed to parse or fell outside the window (ADR-0008) -- but only
    when the error does not establish that the file is gone. A permission or
    transient I/O failure proves the file still exists, unlike the file
    vanishing between the glob and the stat, so it must not disappear from the
    claimant set and let the readable twin look like the id's sole holder.
    """
    from openaidr.readers import claude_code

    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"], project="-project-one")
    inaccessible = root / "-project-two" / f"{SESSION}.jsonl"
    inaccessible.parent.mkdir(parents=True, exist_ok=True)
    inaccessible.write_text("{}\n")
    log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
        project="-project-one",
    )

    real_within_window = claude_code._within_window

    def flaky_within_window(path: Path, window: Window) -> bool:
        if path == inaccessible:
            raise OSError("permission denied")
        return real_within_window(path, window)

    monkeypatch.setattr(claude_code, "_within_window", flaky_within_window)
    sessions, failures = ClaudeCodeReader(root=root, mcp_logs=read_mcp_logs((cache,))).collect(
        Window(since=None)
    )
    assert len(failures) == 1 and str(inaccessible) in failures[0].message
    assert len(sessions) == 1, "the inaccessible file never becomes a session of its own"
    assert sessions[0].mcp_log_state == "session_id_collision"
    assert sessions[0].mcp_connections == ()
    assert [c.status for c in _mcp_calls(sessions)] == ["unknown"]


def test_an_in_window_transcript_whose_type_probe_fails_still_claims_its_raw_session_id(
    tmp_path: Path, monkeypatch
) -> None:
    """A file whose directory-vs-file probe raises is claimant by filename,
    the same as one whose window check raises (ADR-0008) -- but only when the
    error does not establish that the file is gone. The type probe runs
    before the window check, so this is the earliest point in the loop a
    permission or transient I/O failure can strike, and it must not disappear
    from the claimant set there either.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"], project="-project-one")
    inaccessible = root / "-project-two" / f"{SESSION}.jsonl"
    inaccessible.parent.mkdir(parents=True, exist_ok=True)
    inaccessible.write_text("{}\n")
    log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
        project="-project-one",
    )

    real_stat = Path.stat

    def flaky_stat(self: Path, *args: object, **kwargs: object):
        if self == inaccessible:
            raise OSError("permission denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    sessions, failures = ClaudeCodeReader(root=root, mcp_logs=read_mcp_logs((cache,))).collect(
        Window(since=None)
    )
    assert len(failures) == 1 and str(inaccessible) in failures[0].message
    assert len(sessions) == 1, "the inaccessible file never becomes a session of its own"
    assert sessions[0].mcp_log_state == "session_id_collision"
    assert sessions[0].mcp_connections == ()
    assert [c.status for c in _mcp_calls(sessions)] == ["unknown"]


def test_a_trivial_in_window_transcript_still_claims_its_raw_session_id(
    tmp_path: Path,
) -> None:
    """A file upstream parses without error but judges too trivial to report is
    claimant by filename, the same as one that failed to parse or fell outside
    the window (ADR-0008).

    Upstream drops a session whose single message is five characters or fewer
    and calls no tool (see `test_a_session_the_dependency_judges_trivial_is_
    not_reported` in `test_claude_code.py`). No event survives to name this
    file's raw session id via the parsed path, so it must be read from the
    filename instead -- otherwise the readable twin looks like the id's sole
    holder and receives a log that may be the trivial file's.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"], project="-project-one")
    write_session(root, "-project-two", [user_text(SESSION, "u0", "2026-01-01T00:00:00Z", "hi")])
    log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
        project="-project-one",
    )
    sessions = _collect(root, cache)
    assert len(sessions) == 1, "the trivial file never becomes a session of its own"
    assert sessions[0].mcp_log_state == "session_id_collision"
    assert sessions[0].mcp_connections == ()
    assert [c.status for c in _mcp_calls(sessions)] == ["unknown"]


def test_a_subagent_outside_the_window_does_not_manufacture_a_collision(
    tmp_path: Path,
) -> None:
    """A subagent file carries its *parent's* session id (ADR-0001), so it is
    not a claimant however the window falls. The out-of-window claimant set is
    read from the path rather than the file, so it has to make the same
    exclusion the parsed side makes -- from the directory, not from records it
    never read."""
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"], project="-project-one")
    subagent = write_subagent_session(
        root,
        "-project-one",
        SESSION,
        "a1",
        [
            user_text(SESSION, "su0", "2026-01-01T00:00:00Z", "go"),
            assistant_tool_use(SESSION, "sa0", "2026-01-01T00:00:01Z", "toolu_s0", "Read"),
            tool_result(SESSION, "sr0", "2026-01-01T00:00:02Z", "toolu_s0", "x"),
        ],
    )
    os.utime(subagent, (0, 0))
    log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
        project="-project-one",
    )
    window = Window(since=datetime(2026, 1, 1, tzinfo=UTC))
    sessions = _collect(root, cache, window)
    assert len(sessions) == 1
    assert sessions[0].mcp_log_state == "applied"
    assert [c.server for c in sessions[0].mcp_connections] == ["books"]


def test_a_renamed_copy_outside_the_window_is_a_known_uncovered_shape(
    tmp_path: Path,
) -> None:
    """Pins the one gap ADR-0008 leaves open, so closing it is a deliberate act.

    An excluded file is never opened, so its claimed id is its filename. A copy
    *renamed* in place carries the original's id in records nothing reads and a
    name that no longer says so, and while it sits outside the window there is
    nothing left to catch it with short of reading every file on disk -- the
    cost ADR-0008 declines. In-window the same copy *is* caught, because both
    files are parsed and the check keys on the file rather than the name
    (`test_two_transcript_files_in_the_same_project_sharing_a_session_id...`).

    If this test starts failing, the gap was closed: update ADR-0008 rather
    than restoring the old expectation.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"], project="-project-one")
    original = root / "-project-one" / f"{SESSION}.jsonl"
    renamed = original.with_name(f"{SESSION}-restored.jsonl")
    shutil.copy(original, renamed)
    os.utime(renamed, (0, 0))
    log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
        project="-project-one",
    )
    sessions = _collect(root, cache, Window(since=datetime(2026, 1, 1, tzinfo=UTC)))
    assert len(sessions) == 1
    assert sessions[0].mcp_log_state == "applied", "the documented limitation, not a passing case"


def test_a_malformed_line_before_the_end_marks_its_server_incomplete(tmp_path: Path) -> None:
    """A torn *final* line is an ordinary live append; an earlier one is loss.

    What the skipped line said is unknowable, and one of the things it can have
    said is `Calling MCP tool` for a call that overlapped another -- the only
    evidence that this server's completion order cannot be trusted as its
    invocation order. The later completions still make the counts agree, so
    neither the count guard nor the overlap guard sees anything wrong.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"])
    path = log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], '{"debug": "Calling MCP t', *lines[1:]]) + "\n")

    index = read_mcp_logs((cache,))
    assert index.incomplete_project_servers == frozenset({("-work-project", "books")})

    (session,) = _collect(root, cache)
    assert session.mcp_log_state == "count_mismatch"
    assert [c.status for c in _mcp_calls([session])] == ["unknown"]
    (connection,) = session.mcp_connections
    assert connection.transport == "stdio", "the connection fact does not depend on the ordinal"


def test_an_incomplete_log_in_one_project_does_not_withhold_a_same_named_server_elsewhere(
    tmp_path: Path,
) -> None:
    """A server name is not unique across projects (ADR-0006).

    `-broken-project` and `-clean-project` each run a server called `books`.
    Only the first's log is malformed, but a marker keyed on the server name
    alone would withhold the second project's session too, over a file it
    never had any share in.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    broken_session = "aaaaaaaa-0000-0000-0000-000000000000"
    clean_session = "bbbbbbbb-0000-0000-0000-000000000000"
    _transcript(root, ["search"], project="-broken-project", session_id=broken_session)
    _transcript(root, ["search"], project="-clean-project", session_id=clean_session)
    path = log.write_server_log(
        cache,
        "books",
        [log.connected(broken_session, transport="stdio"), log.completed(broken_session, "search")],
        project="-broken-project",
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], '{"debug": "Calling MCP t', *lines[1:]]) + "\n")
    log.write_server_log(
        cache,
        "books",
        [log.connected(clean_session, transport="stdio"), log.completed(clean_session, "search")],
        project="-clean-project",
    )

    index = read_mcp_logs((cache,))
    assert index.incomplete_project_servers == frozenset({("-broken-project", "books")})

    sessions = _collect(root, cache)
    broken = next(s for s in sessions if broken_session in s.session_id)
    clean = next(s for s in sessions if clean_session in s.session_id)
    assert broken.mcp_log_state == "count_mismatch"
    assert clean.mcp_log_state == "applied"
    assert [c.status for c in _mcp_calls([clean])] == ["ok"]


def test_an_outcome_with_no_call_outstanding_withholds_that_tools_ordinals(
    tmp_path: Path,
) -> None:
    """A log that records `Calling` lines and is then missing one has lost the
    evidence the ordinal join rests on.

    Two calls to `search` here, but only one `Calling` line: the second
    completion arrives with nothing in flight for that key, which means either
    a `Calling` line went missing or two calls overlapped. Both make completion
    order unusable as invocation order, and the counts still agree, so nothing
    else catches it. A log that records no `Calling` lines at all -- an older
    client -- is not held to a line it never writes.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search", "search"])
    log.write_server_log(
        cache,
        "books",
        [
            log.connected(SESSION, transport="stdio"),
            log.calling(SESSION, "search"),
            log.completed(SESSION, "search", ms=5),
            log.failed(SESSION, "search", ms=20),
        ],
    )
    (session,) = _collect(root, cache)
    assert session.mcp_log_state == "applied"
    assert session.mcp_overlap_withheld == 2
    calls = _mcp_calls([session])
    assert [c.status for c in calls] == ["unknown", "unknown"]
    assert [c.transport for c in calls] == ["stdio", "stdio"]


def test_a_transcript_subtree_the_walk_cannot_scan_withholds_every_enrichment(
    tmp_path: Path, monkeypatch
) -> None:
    """A directory discovery could not scan hides files whose *names* were never
    seen, which is what breaks the claimant set (ADR-0009).

    `for_session`'s single-match fast path hands over a log found under exactly
    one project on the reasoning that one session id names one session, and
    `_ambiguous_transcript_ids` is what establishes that. It reads a file's raw
    session id from its own name when it cannot read the file (ADR-0008) --
    which presumes a name was seen at all. A project directory `os.walk` could
    not enter yields no names, so a transcript inside it claiming the same id
    as a readable one is invisible, and the readable twin is left looking like
    the sole claimant.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"], project="-a")
    (root / "-b").mkdir(parents=True, exist_ok=True)
    (root / "-b" / f"{SESSION}.jsonl").write_text("{}\n", encoding="utf-8")
    log.write_server_log(
        cache,
        "books",
        [log.connected(SESSION, transport="stdio"), log.completed(SESSION, "search")],
        project="-a",
    )
    blocked = root / "-b"
    real_scandir = os.scandir

    def flaky_scandir(path="."):
        if path == str(blocked):
            raise PermissionError(13, "Permission denied", str(blocked))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", flaky_scandir)
    index = read_mcp_logs((cache,))
    sessions, failures = ClaudeCodeReader(root=root, mcp_logs=index).collect(Window(since=None))

    (session,) = sessions
    assert session.mcp_log_state == "transcript_discovery_incomplete"
    assert session.mcp_connections == ()
    assert [c.status for c in _mcp_calls(sessions)] == ["unknown"]
    assert any(str(blocked) in f.message for f in failures)


def test_an_unresolved_log_for_an_incomplete_server_is_not_called_a_pruned_cache(
    tmp_path: Path,
) -> None:
    """Absence is only absence once the read that would have found it succeeded.

    The cache's own mangled project name is not the transcript's -- 99 of 139
    cache directories on one machine have no counterpart under
    `~/.claude/projects`, which is why `for_session` breaks ties on it rather
    than requiring it to match. So a marker keyed on the *transcript's* project
    name can never match a cache-side one, and with no log resolved for this
    session there is no cache project to scope against at all: the only sound
    question left is whether a server this session calls had unread evidence
    anywhere (ADR-0009). Answering it with the transcript's name reported a
    pruned cache for a log that was merely unread.
    """
    root, cache = tmp_path / "projects", tmp_path / "cache"
    _transcript(root, ["search"], project="-work-project")
    path = log.write_server_log(
        cache,
        "books",
        [log.connected("some-other-session"), log.completed("some-other-session", "search")],
        project="-cache-mangled-name-n0kpsc",
    )
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], '{"debug": "Calling MCP t', *lines[1:]]) + "\n")

    index = read_mcp_logs((cache,))
    assert index.incomplete_project_servers == frozenset({("-cache-mangled-name-n0kpsc", "books")})

    (session,) = _collect(root, cache)
    assert session.mcp_log_state == "log_discovery_incomplete"
