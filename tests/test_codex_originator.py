"""Native client metadata stays separate from runtime and evidence identity."""

from dataclasses import asdict
from pathlib import Path

import pytest

from openaidr.readers.base import Window
from openaidr.readers.codex import CodexReader
from tests.fixtures.codex_rollout import Rollout


@pytest.mark.parametrize(
    ("originator", "expected"),
    [
        ("codex_work_desktop", "codex_work_desktop"),
        ("Codex Desktop", "Codex Desktop"),
        ("future-client", "future-client"),
        (None, None),
        ("", None),
        (42, None),
        ({"client": "codex_work_desktop"}, None),
    ],
)
def test_originator_is_native_metadata_without_changing_identity(
    tmp_path: Path, originator: object, expected: str | None
) -> None:
    rollout = Rollout("client-test", source="vscode", meta={"originator": originator})
    rollout.turn("t1")
    rollout.user("hello")
    rollout.exec("c1", "printf hello", output="hello")
    rollout.usage("r1")
    path = rollout.write(tmp_path)
    reader = CodexReader(tmp_path)

    cold, failures = reader.collect(Window(since=None))
    incremental, append_failures = reader.collect_file(path)

    assert failures == append_failures == []
    assert cold == incremental
    session = cold[0]
    assert session.originator == expected
    assert session.agent_kind == "codex"
    assert session.entrypoint == "vscode"
    assert session.session_id == "codex:client-test"
    assert "originator" not in session.public_descriptor()

    before = asdict(session)
    rollout.assistant("complete")
    with path.open("a") as stream:
        stream.write(rollout.text().splitlines()[-1] + "\n")
    grown, failures = reader.collect_file(path)
    assert failures == []
    assert grown[0].originator == expected
    assert grown[0].session_id == session.session_id
    assert grown[0].responses == session.responses
    assert [call.span for turn in grown[0].turns for call in turn.tool_calls] == [
        call.span for turn in session.turns for call in turn.tool_calls
    ]
    assert asdict(session) == before
