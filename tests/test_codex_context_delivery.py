"""Client-delivered context is not an invocation or a tool result."""

from pathlib import Path

import pytest

from openaidr.readers.codex import CodexReader
from tests.fixtures.codex_rollout import Rollout


def _delivery() -> dict[str, object]:
    # Native desktop shape: a function output supplied as turn input, with
    # its own item id but no invocation's call_id. Values are synthetic.
    return {
        "type": "function_call_output",
        "id": "fco_delivery",
        "namespace": "codex_app",
        "name": "send_message_to_thread",
        "output": "Continue with the local verification.",
    }


def test_delivery_preserves_later_calls_usage_and_incremental_identity(tmp_path: Path) -> None:
    rollout = Rollout("work", originator="codex_work_desktop")
    rollout.turn("first")
    rollout.user("Start local verification")
    rollout.usage("r1")
    rollout.complete()
    path = rollout.write(tmp_path)
    reader = CodexReader(tmp_path)
    before, failures = reader.collect_file(path)
    assert failures == []
    original_spans = [t.key for t in before[0].turns]

    rollout.turn("second")
    rollout.response(_delivery())
    rollout.item({"type": "FunctionCallOutput", "id": "fco_delivery"})
    rollout.exec("shell", "printf ok", output="ok")
    rollout.patch("patch", "*** Begin Patch\n*** Add File: note.txt\n+ok\n*** End Patch")
    rollout.mcp("mcp", "example", "read", {})
    rollout.usage("r2")
    rollout.complete()
    path.write_text(rollout.text())

    after, failures = reader.collect_file(path)
    assert failures == []
    session = after[0]
    calls = [c for t in session.turns for c in t.tool_calls]
    assert {c.provider_call_id for c in calls} == {"shell", "patch", "mcp"}
    assert all(c.status == "ok" for c in calls)
    assert [t.key for t in session.turns][: len(original_spans)] == original_spans
    assert len(session.responses) == 2
    assert sum(r.usage.input_tokens or 0 for r in session.responses) == 1300
    assert sum(r.usage.output_tokens or 0 for r in session.responses) == 100
    delivery = [c for c in session.context_items if c.source == "injected"]
    assert len(delivery) == 1
    assert delivery[0].text == _delivery()["output"]
    assert session.initial_prompt == "Start local verification"
    assert reader.collect_file(path) == (after, [])
    assert CodexReader(tmp_path).collect_file(path) == (after, [])


def _client_tool_outputs() -> list[dict[str, object]]:
    # Native desktop shapes from the client's own tools: a companion page, its
    # snapshot, and a person's edit to a writing block. Each has an item id and
    # a string output, and no call_id. Values are synthetic.
    return [
        {
            "type": "function_call_output",
            "id": "fco_page",
            "namespace": "agent",
            "name": "codex_apps_open_page",
            "output": '{"page_id":null}',
        },
        {
            "type": "function_call_output",
            "id": "fco_snapshot",
            "namespace": "agent",
            "name": "codex_apps_selected_page_part_1_of_1",
            "output": 'Companion Page snapshot part 1 of 1:\n{"page_id":null}',
        },
        {
            "type": "function_call_output",
            "id": "fco_edits",
            "namespace": "agent",
            "name": "codex_apps_writing_block_edits",
            "output": "The user manually edited these writing blocks.",
        },
    ]


def test_client_tool_outputs_are_context_and_keep_the_session(tmp_path: Path) -> None:
    rollout = Rollout("work", originator="codex_work_desktop")
    rollout.turn("first")
    for output in _client_tool_outputs()[:2]:
        rollout.response(output)
    rollout.complete()
    rollout.turn("second")
    rollout.user("Draft a refund note")
    rollout.response(_client_tool_outputs()[2])
    rollout.exec("shell", "cat notes.md", output="notes")
    rollout.usage("r1")
    rollout.complete()
    path = rollout.write(tmp_path)

    sessions, failures = CodexReader(tmp_path).collect_file(path)
    assert failures == []
    session = sessions[0]
    calls = [c for t in session.turns for c in t.tool_calls]
    assert [c.provider_call_id for c in calls] == ["shell"]
    assert calls[0].status == "ok"
    injected = [c.text for c in session.context_items if c.source == "injected"]
    assert injected == [str(o["output"]) for o in _client_tool_outputs()]
    assert session.initial_prompt == "Draft a refund note"


@pytest.mark.parametrize(
    "change",
    [
        {"type": "custom_tool_call_output"},
        {"id": None},
        {"output": None},
        {"output": {"page_id": None}},
    ],
)
def test_unrecognized_outputs_still_withhold_the_session(
    tmp_path: Path, change: dict[str, object]
) -> None:
    rollout = Rollout("work")
    rollout.turn("first")
    rollout.response({**_delivery(), **change})
    path = rollout.write(tmp_path)
    sessions, failures = CodexReader(tmp_path).collect_file(path)
    assert sessions == []
    assert len(failures) == 1
    assert "malformed rollout record" in failures[0].message


def test_delivery_named_output_with_call_id_remains_a_tool_result(tmp_path: Path) -> None:
    rollout = Rollout("work")
    rollout.turn("first")
    rollout.call("real-call", "send_message_to_thread", {}, namespace="codex_app")
    rollout.response({**_delivery(), "call_id": "real-call"})
    rollout.complete()
    path = rollout.write(tmp_path)
    sessions, failures = CodexReader(tmp_path).collect_file(path)
    assert failures == []
    calls = [c for t in sessions[0].turns for c in t.tool_calls]
    assert len(calls) == 1
    assert calls[0].result == _delivery()["output"]
    assert not any(c.source == "injected" for c in sessions[0].context_items)
