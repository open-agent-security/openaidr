"""Per-turn time, and the session's last activity (ADR-0010).

Every transcript record carries a timestamp. These tests assert what this
package makes of them: a turn is dated by its own record, a session is dated by
the newest record of any kind, every value is an instant in UTC, and nothing is
invented where the transcript stated nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from openaidr.readers.base import Window
from openaidr.readers.claude_code import ClaudeCodeReader
from tests.fixtures.claude_jsonl import (
    assistant_tool_use,
    system_event,
    tool_result,
    user_text,
    write_session,
)


def _sessions(root: Path):
    sessions, _failures = ClaudeCodeReader(root=root).collect(Window(since=None))
    return sessions


def test_a_turn_is_dated_by_the_record_that_produced_it(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        "-p",
        [
            user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "first"),
            user_text("s1", "u2", "2026-08-01T14:30:00.000Z", "second"),
        ],
    )
    turns = _sessions(tmp_path)[0].turns
    assert [t.occurred_at for t in turns] == [
        datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        datetime(2026, 8, 1, 14, 30, tzinfo=UTC),
    ]


def test_a_turns_time_is_its_own_not_the_sessions_start(tmp_path: Path) -> None:
    """The point of the field. A session running for hours dates its last turn
    by its last record, where `started_at` would date it by its first."""
    write_session(
        tmp_path,
        "-p",
        [
            user_text("s1", "u1", "2026-08-01T09:00:00.000Z", "start of a long session"),
            user_text("s1", "u2", "2026-08-01T21:45:00.000Z", "twelve hours later"),
        ],
    )
    session = _sessions(tmp_path)[0]
    assert session.started_at == datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    assert session.turns[-1].occurred_at == datetime(2026, 8, 1, 21, 45, tzinfo=UTC)


def test_every_reported_time_is_an_aware_instant_in_utc(tmp_path: Path) -> None:
    """Consumers compare, sort and fold these across machines in other zones, so
    a naive value would raise on comparison and a non-UTC one would serialise
    inconsistently. Asked of every time on the model, not one of them."""
    write_session(
        tmp_path,
        "-p",
        [
            user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "zulu"),
            user_text("s1", "u2", "2026-08-01T16:00:00+05:30", "offset"),
            user_text("s1", "u3", "2026-08-01T18:00:00", "no offset at all"),
        ],
    )
    session = _sessions(tmp_path)[0]
    times = [session.started_at, session.last_activity_at] + [t.occurred_at for t in session.turns]
    assert all(t is not None for t in times)
    assert all(t.tzinfo is not None and t.utcoffset() == UTC.utcoffset(None) for t in times)  # type: ignore[union-attr]


def test_an_offset_bearing_time_is_converted_rather_than_passed_through(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        "-p",
        [user_text("s1", "u1", "2026-08-01T16:00:00+05:30", "written in another zone")],
    )
    assert _sessions(tmp_path)[0].turns[0].occurred_at == datetime(2026, 8, 1, 10, 30, tzinfo=UTC)


def test_a_zoneless_time_is_taken_as_utc_like_the_dependency(tmp_path: Path) -> None:
    """One rule across the model (ADR-0010). The dependency resolves the session
    start this way, and a model whose two time fields disagree about zones is one
    a consumer has to memorise."""
    write_session(
        tmp_path,
        "-p",
        [user_text("s1", "u1", "2026-08-01T10:00:00", "no offset")],
    )
    session = _sessions(tmp_path)[0]
    assert session.turns[0].occurred_at == datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    assert session.turns[0].occurred_at == session.started_at


def test_a_record_with_no_timestamp_leaves_its_turn_undated(tmp_path: Path) -> None:
    """Absence is not falsehood: no collection time, no neighbour's value."""
    record = user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "dated")
    undated = user_text("s1", "u2", "2026-08-01T11:00:00.000Z", "undated")
    del undated["timestamp"]
    write_session(tmp_path, "-p", [record, undated])
    turns = _sessions(tmp_path)[0].turns
    assert turns[0].occurred_at == datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    assert turns[1].occurred_at is None


def test_last_activity_counts_records_that_produce_no_turn(tmp_path: Path) -> None:
    """The reason the field is not `max(turn.occurred_at)`. A tool result and a
    `system` boundary are activity in the session but project to no turn, so a
    max over turns alone understates how recently the session did anything."""
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "call-1", "Read", {}),
            tool_result("s1", "u2", "2026-08-01T10:00:05.000Z", "call-1", "ok"),
            system_event("s1", "u3", "2026-08-01T10:30:00.000Z", "compact_boundary"),
        ],
    )
    session = _sessions(tmp_path)[0]
    newest_turn = max(t.occurred_at for t in session.turns if t.occurred_at)
    assert newest_turn == datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    assert session.last_activity_at == datetime(2026, 8, 1, 10, 30, tzinfo=UTC)


def test_last_activity_is_never_before_the_session_start(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        "-p",
        [
            user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "first"),
            user_text("s1", "u2", "2026-08-02T10:00:00.000Z", "next day"),
        ],
    )
    session = _sessions(tmp_path)[0]
    assert session.started_at is not None and session.last_activity_at is not None
    assert session.last_activity_at >= session.started_at


def test_a_session_whose_records_carry_no_time_reports_none_not_now(tmp_path: Path) -> None:
    record = user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "only turn")
    del record["timestamp"]
    write_session(tmp_path, "-p", [record])
    session = _sessions(tmp_path)[0]
    assert session.last_activity_at is None
    assert session.turns[0].occurred_at is None


def test_one_file_holding_two_sessions_dates_each_by_its_own_records(tmp_path: Path) -> None:
    """A resume mints a new session id but keeps writing to the same file, so a
    file-wide fold would give the older session the newer one's clock."""
    write_session(
        tmp_path,
        "-p",
        [
            user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "first session"),
            user_text("s2", "u2", "2026-08-05T10:00:00.000Z", "resumed as a new id"),
        ],
    )
    by_id = {s.session_id: s for s in _sessions(tmp_path)}
    assert by_id["claude-code:s1"].last_activity_at == datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
    assert by_id["claude-code:s2"].last_activity_at == datetime(2026, 8, 5, 10, 0, tzinfo=UTC)


def test_parallel_calls_in_one_record_share_their_turns_time(tmp_path: Path) -> None:
    """Stated rather than corrected: the grain is the record, so within a turn
    only span order separates calls. A consumer sorting by time must tiebreak."""
    record = assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "call-1", "Read", {})
    record["message"]["content"].append(
        {"type": "tool_use", "id": "call-2", "name": "Grep", "input": {}}
    )
    write_session(tmp_path, "-p", [record])
    turn = _sessions(tmp_path)[0].turns[0]
    assert len(turn.tool_calls) == 2
    assert turn.occurred_at == datetime(2026, 8, 1, 10, 0, tzinfo=UTC)
