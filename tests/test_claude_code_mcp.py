"""The MCP connection log: what it adds, and every way it declines to."""

from __future__ import annotations

import json
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


def _collect(root: Path, cache: Path | None):
    index = read_mcp_logs((cache,) if cache is not None else (root / "absent",))
    sessions, _ = ClaudeCodeReader(root=root, mcp_logs=index).collect(Window(since=None))
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


def test_no_cache_directory_is_reported_not_assumed_empty(tmp_path: Path) -> None:
    """A machine we cannot read and a machine running no MCP must differ.

    Reported as a state rather than an absence, because a wrong cache path
    would otherwise make every transport `None` and read exactly like a fleet
    with no remote servers at all.
    """
    root = tmp_path / "projects"
    _transcript(root, ["search"])
    (session,) = _collect(root, None)
    assert session.mcp_log_state == "no_log_root"
    assert session.mcp_connections == ()
    assert [c.status for c in _mcp_calls([session])] == ["unknown"]


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
