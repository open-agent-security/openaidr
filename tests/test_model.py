from openaidr.model import LOCAL_ONLY, Session, span_id


def test_span_id_names_the_session_turn_and_call() -> None:
    assert span_id("s1", "turn-uuid", 0) == "s1:turn-uuid:0"


def test_span_id_indexes_within_the_turn_not_the_session() -> None:
    assert span_id("s1", "turn-uuid", 2) == "s1:turn-uuid:2"


def test_span_id_is_unchanged_by_later_turns() -> None:
    """A session grows by appending; an existing span must keep its key."""
    first = span_id("s1", "turn-a", 0)
    _later = span_id("s1", "turn-b", 0)
    assert span_id("s1", "turn-a", 0) == first


def test_public_descriptor_carries_no_local_only_information() -> None:
    session = Session(
        session_id="claude-code:s1",
        agent_kind="claude-code",
        source="claude",
        started_at=None,
        model="claude-opus-5",
        working_directory="/Users/someone/secret-project",
        machine="host",
        user="someone",
        agent_version="2.1.241",
        entrypoint="cli",
        turns=(),
    )
    descriptor = session.public_descriptor()
    assert descriptor == {
        "session_id": "claude-code:s1",
        "agent_kind": "claude-code",
        "started_at": None,
        "turn_count": 0,
    }
    assert not LOCAL_ONLY & set(descriptor)
