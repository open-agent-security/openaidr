"""Placing Codex's MCP connections from its log database (`docs/specs/codex.md`)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from openaidr.readers.base import Window
from openaidr.readers.codex import CodexReader
from openaidr.readers.codex_log import CodexLogs
from tests.fixtures.codex_rollout import (
    Rollout,
    failed_line,
    request_span,
    started_line,
    write_logs,
)

_STARTED = "rmcp::service"
_FAILED = "codex_mcp::rmcp_client"
_OTHER = "codex_core::shell_snapshot"


def _place(home: Path, thread: str):
    logs = CodexLogs.open(home)
    try:
        placement, failure = logs.place(thread)
    finally:
        logs.close()
    assert failure is None
    return placement


def test_a_line_is_placed_by_the_request_that_created_its_thread(tmp_path: Path) -> None:
    """Two threads started in one app-server process: each takes the servers
    its own `thread/start` started, never the other's, whatever the times."""
    a, b = request_span(10), request_span(11)
    write_logs(
        tmp_path,
        [
            (_STARTED, started_line("github", name="gh-server", version="2.1", span=a), None, "p1"),
            (_STARTED, started_line("github", span=b), None, "p1"),
            (_OTHER, f"{a}:thread_spawn: snapshot created", "thread-a", "p1"),
            (_OTHER, f"{b}:thread_spawn: snapshot created", "thread-b", "p1"),
            # Fails after both threads already logged: time would misplace it.
            (
                _FAILED,
                failed_line("broken", "No such file or directory (os error 2)", span=b),
                None,
                "p1",
            ),
        ],
    )

    first, second = _place(tmp_path, "thread-a"), _place(tmp_path, "thread-b")

    assert first.state == second.state == "applied"
    assert [
        (c.server, c.connected, c.advertised_name, c.advertised_version) for c in first.connections
    ] == [("github", True, "gh-server", "2.1")]
    assert [(c.server, c.connected, c.failure_category) for c in second.connections] == [
        ("github", True, None),
        ("broken", False, "spawn"),
    ]


def test_a_request_span_match_outside_the_process_is_not_the_same_span(tmp_path: Path) -> None:
    """A request id and a connection id are both assigned per app-server
    process, so the same pair can recur in an unrelated process. A thread
    whose own activity spans two processes must not pick up a server
    another thread started in the other one just because the numbers happen
    to match (P2 Codex review finding on PR #76)."""
    span = request_span(5)
    write_logs(
        tmp_path,
        [
            # Thread "t"'s own request span, logged in its real process "pA".
            (_OTHER, f"{span}:thread_spawn: snapshot created", "t", "pA"),
            # An unrelated process "pB" reuses the same (request_id,
            # connection) pair for a server that has nothing to do with "t".
            (_STARTED, started_line("other-thread-server", span=span), None, "pB"),
            # "t" also has activity in "pB" sometime later, so "pB" is one of
            # the processes place() considers for it.
            (_OTHER, "turn", "t", "pB"),
        ],
    )

    placement = _place(tmp_path, "t")

    assert (placement.state, placement.connections) == ("applied", ())


def test_a_line_outside_any_request_is_placed_only_where_the_process_served_one_thread(
    tmp_path: Path,
) -> None:
    write_logs(
        tmp_path,
        [
            (_STARTED, started_line("only"), None, "solo"),
            (_OTHER, "turn", "thread-1", "solo"),
            (_STARTED, started_line("shared"), None, "busy"),
            (_OTHER, "turn", "thread-2", "busy"),
            (_OTHER, "turn", "thread-3", "busy"),
        ],
    )

    solo = _place(tmp_path, "thread-1")
    busy = _place(tmp_path, "thread-2")

    assert [c.server for c in solo.connections] == ["only"]
    assert (busy.state, busy.connections) == ("placement_ambiguous", ())


def test_a_timestamp_out_of_range_drops_only_its_own_row(tmp_path: Path) -> None:
    """`datetime.fromtimestamp` on SQLite's max integer raises `OSError`, not
    a condition `place()`'s own `sqlite3.Error` catch covers; the row is
    unreadable, not the whole log (P2 Codex review finding on PR #76)."""
    path = write_logs(
        tmp_path,
        [
            (_STARTED, started_line("only"), None, "solo"),
            (_OTHER, "turn", "thread-1", "solo"),
        ],
    )
    db = sqlite3.connect(path)
    db.execute("UPDATE logs SET ts = ? WHERE target = ?", (2**63 - 1, _STARTED))
    db.commit()
    db.close()

    solo = _place(tmp_path, "thread-1")

    assert solo.connections == ()


def test_failures_are_categorised_from_codexs_own_words(tmp_path: Path) -> None:
    span = request_span(1)
    write_logs(
        tmp_path,
        [
            (
                _FAILED,
                failed_line("spawn", "No such file or directory (os error 2)", span=span),
                None,
                "p",
            ),
            (
                _FAILED,
                failed_line(
                    "net",
                    "handshaking with MCP server failed: Send message error Transport "
                    "[rmcp::transport::worker::WorkerTransport<StreamableHttpClientAdapter>] "
                    "error: Client error: HTTP request failed: error sending request for url",
                    span=span,
                ),
                None,
                "p",
            ),
            (
                _FAILED,
                failed_line(
                    "auth", "handshaking with MCP server failed: 401 Unauthorized", span=span
                ),
                None,
                "p",
            ),
            (
                _FAILED,
                failed_line(
                    "proto", "handshaking with MCP server failed: unexpected EOF", span=span
                ),
                None,
                "p",
            ),
            (_OTHER, f"{span}: created", "t", "p"),
        ],
    )

    placement = _place(tmp_path, "t")

    assert {c.server: (c.failure_category, c.transport) for c in placement.connections} == {
        "spawn": ("spawn", None),
        "net": ("network", "streamable-http"),
        "auth": ("auth", None),
        "proto": ("protocol", None),
    }
    assert all(c.connected is False and c.failure_detail for c in placement.connections)


def test_lines_that_disagree_leave_the_outcome_unknown(tmp_path: Path) -> None:
    span = request_span(1)
    write_logs(
        tmp_path,
        [
            (_FAILED, failed_line("flaky", "timed out", span=span), None, "p"),
            (_STARTED, started_line("flaky", span=span), None, "p"),
            (_OTHER, f"{span}: created", "t", "p"),
        ],
    )

    (connection,) = _place(tmp_path, "t").connections

    assert (connection.connected, connection.failure_category) == (None, None)


def test_a_thread_the_database_never_names_has_no_log(tmp_path: Path) -> None:
    """`codex exec` writes no rows, so its threads always land here."""
    write_logs(tmp_path, [(_OTHER, "turn", "someone-else", "p")])

    assert _place(tmp_path, "exec-thread").state == "no_log_for_session"


def test_no_database_is_no_log_root(tmp_path: Path) -> None:
    assert _place(tmp_path, "t").state == "no_log_root"


def test_the_newest_database_is_read(tmp_path: Path) -> None:
    write_logs(
        tmp_path,
        [(_STARTED, started_line("old"), None, "p"), (_OTHER, "x", "t", "p")],
        name="logs_1.sqlite",
    )
    write_logs(
        tmp_path,
        [(_STARTED, started_line("new"), None, "p"), (_OTHER, "x", "t", "p")],
        name="logs_2.sqlite",
    )

    assert [c.server for c in _place(tmp_path, "t").connections] == ["new"]


def test_the_reader_places_connections_and_carries_each_calls_transport(tmp_path: Path) -> None:
    span = request_span(7)
    write_logs(
        tmp_path,
        [
            (
                _FAILED,
                failed_line(
                    "remote",
                    "handshaking with MCP server failed: http/request failed: error sending request",
                    span=span,
                ),
                None,
                "p",
            ),
            (_STARTED, started_line("local", span=span), None, "p"),
            (_OTHER, f"{span}: created", "thread", "p"),
        ],
    )
    rollout = Rollout("thread", source="vscode")
    rollout.turn("t1")
    rollout.mcp("c1", "remote", "fetch", {})
    rollout.mcp("c2", "local", "read", {})
    rollout.complete()
    rollout.write(tmp_path)

    (session,), failures = CodexReader(tmp_path).collect(Window(since=None))

    assert failures == []
    assert session.mcp_log_state == "applied"
    assert [(c.server, c.connected) for c in session.mcp_connections] == [
        ("remote", False),
        ("local", True),
    ]
    transports = {c.mcp_server: c.transport for t in session.turns for c in t.tool_calls}
    assert transports == {"remote": "streamable-http", "local": None}
