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
from tests.fixtures.claude_jsonl import assistant_tool_use, tool_result, user_text, write_session

SESSION = "11111111-2222-3333-4444-555555555555"


def _transcript(root: Path, tools: list[str]) -> None:
    """A session calling `tools` over MCP, plus one built-in so it always parses.

    Upstream yields nothing for a transcript with no tool calls at all, so a
    connection-only test still needs one call to have a session to attach to.
    """
    records = [
        user_text(SESSION, "u0", "2026-01-01T00:00:00Z", "go"),
        assistant_tool_use(SESSION, "anchor", "2026-01-01T00:00:01Z", "toolu_anchor", "Read"),
        tool_result(SESSION, "ranchor", "2026-01-01T00:00:02Z", "toolu_anchor", "x"),
    ]
    for index, name in enumerate(tools):
        call_id = f"toolu_{index}"
        records.append(
            assistant_tool_use(
                SESSION,
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
        records.append(tool_result(SESSION, f"r{index}", "2026-01-01T00:00:02Z", call_id, "done"))
    write_session(root, "project", records)


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
    logs = index.for_session(SESSION)
    assert logs is not None
    connection = logs.connections["books"]
    assert connection.failure_category == expected


def test_a_torn_final_line_does_not_lose_the_file(tmp_path: Path) -> None:
    """The log is appended to live, so a half-written last record is ordinary."""
    cache = tmp_path / "cache"
    path = log.write_server_log(cache, "books", [log.connected(SESSION)])
    path.write_text(path.read_text() + '{"debug": "Tool ', encoding="utf-8")
    index = read_mcp_logs((cache,))
    logs = index.for_session(SESSION)
    assert logs is not None
    assert logs.connections["books"].transport == "stdio"


def test_the_cache_root_can_be_overridden(tmp_path: Path, monkeypatch) -> None:
    """Which is what lets this be tested without a real cache on the machine."""
    monkeypatch.setenv("CLAUDE_CLI_CACHE_DIR", str(tmp_path))
    assert cache_roots() == (tmp_path,)


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
