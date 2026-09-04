from datetime import UTC, datetime
from pathlib import Path

from openaidr.collector import collect, default_readers
from openaidr.kinds import parse_kind_filter
from openaidr.model import Session
from openaidr.readers.base import ReaderFailure, Window
from tests.fixtures.claude_jsonl import user_text, write_session


class _StubReader:
    agent_kind = "cursor"

    def collect(self, window: Window) -> tuple[list[Session], list[ReaderFailure]]:
        return [
            Session(
                session_id="cursor:x",
                agent_kind="cursor",
                source="cursor",
                started_at=datetime(2026, 8, 2, tzinfo=UTC),
                model=None,
                working_directory=None,
                machine=None,
                user=None,
                agent_version="2.1.241",
                entrypoint="cli",
                turns=(),
            )
        ], []


def test_collects_claude_code_sessions_by_default(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        "-p",
        [user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "look at the repository")],
    )
    result = collect(parse_kind_filter(None), Window(since=None), default_readers(root=tmp_path))
    assert [s.session_id for s in result.sessions] == ["claude-code:s1"]
    assert result.failures == []


def test_a_kind_filter_drops_other_kinds_outright(tmp_path: Path) -> None:
    """A user-excluded kind is a user instruction, not a coverage gap."""
    write_session(
        tmp_path,
        "-p",
        [user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "look at the repository")],
    )
    readers = [*default_readers(root=tmp_path), _StubReader()]
    result = collect(parse_kind_filter(["claude-code"]), Window(since=None), readers)
    assert [s.agent_kind for s in result.sessions] == ["claude-code"]


def test_sessions_are_ordered_newest_first(tmp_path: Path) -> None:
    write_session(
        tmp_path, "-a", [user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "the first session")]
    )
    write_session(
        tmp_path, "-b", [user_text("s2", "u2", "2026-08-03T10:00:00.000Z", "the second session")]
    )
    result = collect(parse_kind_filter(None), Window(since=None), default_readers(root=tmp_path))
    assert [s.session_id for s in result.sessions] == ["claude-code:s2", "claude-code:s1"]


def test_a_repeated_session_identity_is_reported_not_silently_replaced() -> None:
    """Cold start is a session map keyed by identity, per the spec's collection
    design — but a second result naming an identity already seen is a
    collision Claude Code's own session-id contract does not predict, so it
    is reported alongside the kept session rather than silently discarded.
    """
    result = collect(parse_kind_filter(None), Window(since=None), [_StubReader(), _StubReader()])
    assert [s.session_id for s in result.sessions] == ["cursor:x"]
    assert len(result.failures) == 1
    assert "cursor:x" in result.failures[0].message


def test_a_nonexistent_root_collects_cleanly_to_nothing(tmp_path: Path) -> None:
    """A root nobody has written to yet is not this collector's failure to report."""
    result = collect(
        parse_kind_filter(None), Window(since=None), default_readers(root=tmp_path / "missing")
    )
    assert result.sessions == []
    assert result.failures == []


def test_an_empty_root_collects_cleanly_to_nothing(tmp_path: Path) -> None:
    result = collect(parse_kind_filter(None), Window(since=None), default_readers(root=tmp_path))
    assert result.sessions == []
    assert result.failures == []
