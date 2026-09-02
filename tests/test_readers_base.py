from datetime import UTC, datetime

from openaidr.model import Session
from openaidr.readers.base import ReaderFailure, Window, collect_from


class _StubReader:
    def __init__(self, agent_kind: str, sessions: list[Session]) -> None:
        self.agent_kind = agent_kind
        self._sessions = sessions

    def collect(self, window: Window) -> tuple[list[Session], list[ReaderFailure]]:
        return self._sessions, []


class _ReportingReader:
    """A reader that already caught a per-file problem and reports it as data,
    per the Claude Code reader's own contract (Task 5)."""

    agent_kind = "cursor"

    def collect(self, window: Window) -> tuple[list[Session], list[ReaderFailure]]:
        return [], [ReaderFailure(agent_kind="cursor", message="cursor.db: locked")]


class _ExplodingReader:
    agent_kind = "cursor"

    def collect(self, window: Window) -> tuple[list[Session], list[ReaderFailure]]:
        raise RuntimeError("unreadable database")


def _session(session_id: str) -> Session:
    return Session(
        session_id=session_id,
        agent_kind="claude-code",
        source="claude",
        started_at=datetime(2026, 8, 1, tzinfo=UTC),
        model=None,
        working_directory=None,
        machine=None,
        user=None,
        agent_version=None,
        entrypoint=None,
        turns=(),
    )


def test_collects_from_every_reader() -> None:
    readers = [_StubReader("claude-code", [_session("a")])]
    sessions, failures = collect_from(readers, Window(since=None))
    assert [s.session_id for s in sessions] == ["a"]
    assert failures == []


def test_a_readers_own_reported_failure_survives_collect_from() -> None:
    """A reader that already caught its own error still gets to report it."""
    readers = [_StubReader("claude-code", [_session("a")]), _ReportingReader()]
    sessions, failures = collect_from(readers, Window(since=None))
    assert [s.session_id for s in sessions] == ["a"]
    assert failures == [ReaderFailure(agent_kind="cursor", message="cursor.db: locked")]


def test_one_failing_kind_is_isolated_and_reported() -> None:
    """A failure in one kind is reported and isolated, never aborting the others."""
    readers = [_StubReader("claude-code", [_session("a")]), _ExplodingReader()]
    sessions, failures = collect_from(readers, Window(since=None))
    assert [s.session_id for s in sessions] == ["a"]
    assert len(failures) == 1
    assert failures[0].agent_kind == "cursor"
    assert "unreadable database" in failures[0].message
