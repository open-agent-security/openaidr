"""The Cursor reader, against stores, logs and transcripts shaped like the real ones.

Every shape here is one `agent-cli-local` 2026.10.19-6bbf2c4 wrote when run
against a mock model (`docs/specs/cursor.md#evidence`).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

import pytest

from openaidr.accounts import sign_in_records, signed_in
from openaidr.collector import collect, default_readers
from openaidr.kinds import parse_kind_filter
from openaidr.model import Session, ToolCall
from openaidr.readers.base import Window
from openaidr.readers.cursor import CursorReader
from tests.fixtures import cursor_store as cs
from tests.fixtures.cursor_store import T0, Conversation


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".cursor"


@pytest.fixture
def logs(tmp_path: Path) -> Path:
    return tmp_path / "tmp" / f"cursor-agent-logs-{os.getuid()}"


def _reader(home: Path, logs: Path) -> CursorReader:
    return CursorReader(root=home, log_root=logs)


def _read(home: Path, logs: Path) -> dict[str, Session]:
    sessions, failures = _reader(home, logs).collect(Window(since=None))
    assert failures == []
    return {s.session_id: s for s in sessions}


def _calls(session: Session) -> list[ToolCall]:
    return [call for turn in session.turns for call in turn.tool_calls]


SHELL_OK: Mapping[str, object] = {
    "success": {"command": "echo hello", "stdout": "hello\n", "executionTime": 105}
}


# --- identity and the conversation -------------------------------------------


def test_a_conversation_is_a_session_of_its_prompt_messages(home: Path, logs: Path) -> None:
    c = Conversation("a1", title="Fix the build")
    c.user("run the tests")
    c.call(
        "Shell",
        {"command": "echo hello", "description": "say hello"},
        SHELL_OK,
        result="Exit code: 0",
    )
    message = c.say("All done.")
    c.write(home / "chats")

    session = _read(home, logs)["cursor:a1"]

    assert session.agent_kind == "cursor"
    assert session.source == "cursor"
    assert session.working_directory == "/work/app"
    assert session.started_at == T0
    assert session.title == "Fix the build"
    assert session.initial_prompt == "run the tests"
    assert [t.role for t in session.turns] == ["user", "assistant", "assistant"]
    assert session.turns[-1].key == message
    assert session.turns[-1].text == "All done."
    [call] = _calls(session)
    assert call.span == f"cursor:a1:{session.turns[1].key}:0"
    assert session.subagents_recorded is True
    assert session.compactions_recorded is False


def test_the_harness_context_is_a_context_item_not_a_user_turn(home: Path, logs: Path) -> None:
    c = Conversation("a1")
    c.user("hello")
    c.write(home / "chats")

    session = _read(home, logs)["cursor:a1"]

    [item] = session.context_items
    assert item.source == "system"
    assert item.name == "user_info"
    assert [t.role for t in session.turns] == ["user"]


def test_the_default_name_is_not_a_title(home: Path, logs: Path) -> None:
    c = Conversation("a1")
    c.user("hello")
    c.write(home / "chats")

    assert _read(home, logs)["cursor:a1"].title is None


def test_a_repeated_message_key_takes_an_occurrence_suffix(home: Path, logs: Path) -> None:
    c = Conversation("a1")
    c.user("same words")
    c.user("same words")
    c.write(home / "chats")

    keys = [t.key for t in _read(home, logs)["cursor:a1"].turns]

    assert len(set(keys)) == 2
    assert keys[1] == f"{keys[0]}#2"


def test_appending_messages_moves_no_span(home: Path, logs: Path) -> None:
    c = Conversation("a1")
    c.user("go")
    c.call("Shell", {"command": "echo hello"}, SHELL_OK)
    c.write(home / "chats")
    before = _calls(_read(home, logs)["cursor:a1"])[0].span

    c.call("Shell", {"command": "ls"}, SHELL_OK)
    c.say("done")
    c.write(home / "chats")
    after = _calls(_read(home, logs)["cursor:a1"])

    assert after[0].span == before
    assert len({call.span for call in after}) == 2


# --- outcomes ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("output", "status", "denial", "exit_code"),
    [
        (SHELL_OK, "ok", None, 0),
        ({"success": {"exitCode": 3, "stdout": ""}}, "ok", None, 3),
        ({"failure": {"exitCode": 2, "stderr": "ls: cannot access"}}, "error", None, 2),
        (
            {"failure": {"exitCode": 130, "aborted": True, "abortReason": "USER_ABORT"}},
            "interrupted",
            None,
            130,
        ),
        ({"timeout": {"timeoutMs": 30000}}, "error", None, None),
        ({"spawnError": {"error": "spawn bash ENOENT"}}, "error", None, None),
        (
            {"rejected": {"command": "rm -rf x", "workingDirectory": "/work/app"}},
            "rejected",
            None,
            None,
        ),
        (
            {"rejected": {"reason": "User rejected the operation"}},
            "rejected",
            "user-rejected",
            None,
        ),
        (
            {"rejected": {"reason": "Blocked by (readonly) permissions configuration"}},
            "rejected",
            "permission-rule",
            None,
        ),
        (
            {"rejected": {"reason": "Shell was blocked by a hook: no"}},
            "rejected",
            "permission-rule",
            None,
        ),
        ({"permissionDenied": {"error": "sandbox"}}, "rejected", "permission-rule", None),
    ],
)
def test_a_shell_calls_status_is_cursors_typed_outcome(
    home: Path,
    logs: Path,
    output: Mapping[str, object],
    status: str,
    denial: str | None,
    exit_code: int | None,
) -> None:
    c = Conversation("a1")
    c.user("go")
    c.call("Shell", {"command": "x"}, output, result="Exit code: 0\n\nCommand output: looks fine")
    c.write(home / "chats")

    [call] = _calls(_read(home, logs)["cursor:a1"])

    assert (call.status, call.denial_kind, call.exit_code) == (status, denial, exit_code)


def test_a_shell_outcome_beside_other_result_fields_is_still_read(home: Path, logs: Path) -> None:
    """A shell result carries `sandboxPolicy`, `isBackground` and `pid` beside
    its case, so the case is found by name, not by being the only key."""
    c = Conversation("a1")
    c.user("go")
    c.call(
        "Shell",
        {"command": "ls"},
        {"failure": {"exitCode": 1}, "sandboxPolicy": {"type": "none"}, "isBackground": False},
    )
    c.write(home / "chats")

    [call] = _calls(_read(home, logs)["cursor:a1"])

    assert (call.status, call.exit_code) == ("error", 1)


def test_a_result_with_no_typed_outcome_is_unknown_and_no_result_is_pending(
    home: Path, logs: Path
) -> None:
    c = Conversation("a1")
    c.user("go")
    c.call("Shell", {"command": "ls"}, {}, result="Exit code: 0")
    c.call("Shell", {"command": "sleep 60"}, None)
    c.write(home / "chats")

    statuses = [call.status for call in _calls(_read(home, logs)["cursor:a1"])]

    assert statuses == ["unknown", "pending"]


def test_the_result_text_never_decides_the_status(home: Path, logs: Path) -> None:
    c = Conversation("a1")
    c.user("go")
    c.call("Shell", {"command": "x"}, {"failure": {"exitCode": 1}}, result="Exit code: 0 success")
    c.write(home / "chats")

    [call] = _calls(_read(home, logs)["cursor:a1"])

    assert call.status == "error"
    assert call.result == "Exit code: 0 success"


def test_a_refused_shell_call_carries_its_working_directory(home: Path, logs: Path) -> None:
    c = Conversation("a1")
    c.user("go")
    c.call(
        "Shell",
        {"command": "rm -rf x"},
        {"rejected": {"command": "rm -rf x", "workingDirectory": "/work/app"}},
    )
    c.write(home / "chats")

    [call] = _calls(_read(home, logs)["cursor:a1"])

    assert call.working_directory == "/work/app"


def test_an_mcp_identifier_the_client_recorded_is_named_by_its_configuration(
    home: Path, logs: Path
) -> None:
    c = Conversation("a1", cwd="/work/app")
    c.user("go")
    c.call(
        "CallMcpTool",
        {"server": "user-probe", "toolName": "echo", "arguments": {}},
        {"success": {"content": [{"text": {"text": "echo"}}]}},
        result="echo",
        case=15,
    )
    c.write(home / "chats")
    cs.write_server_metadata(home, "/work/app", "user-probe", "probe")

    [call] = _calls(_read(home, logs)["cursor:a1"])

    assert call.mcp_server == "probe"


def test_an_mcp_call_is_named_by_its_server_and_tool(home: Path, logs: Path) -> None:
    c = Conversation("a1")
    c.user("go")
    c.call(
        "CallMcpTool",
        {"server": "probe", "toolName": "echo", "arguments": {"text": "hi"}, "description": "echo"},
        {"success": {"content": [{"text": {"text": "echo: hi"}}]}},
        result="echo: hi",
        case=15,
    )
    c.call(
        "CallMcpTool",
        {"server": "probe", "toolName": "fail", "arguments": {}},
        {"success": {"content": [{"text": {"text": "HTTP 403 Forbidden"}}], "isError": True}},
        result="HTTP 403 Forbidden",
        case=15,
    )
    c.call(
        "CallMcpTool",
        {"server": "probe", "toolName": "nosuch", "arguments": {}},
        {"error": {"error": "Tool probe-nosuch was not found."}},
        raw_errors=["Tool probe-nosuch was not found."],
        case=15,
    )
    c.call(
        "CallMcpTool",
        {"server": "probe", "toolName": "echo", "arguments": {}},
        {"rejected": {"reason": "User rejected MCP: probe-echo"}},
        case=15,
    )
    c.write(home / "chats")

    calls = _calls(_read(home, logs)["cursor:a1"])

    assert [(c.mcp_server, c.tool_name, c.tool_class) for c in calls] == [
        ("probe", "echo", "mcp")
    ] * 1 + [
        ("probe", "fail", "mcp"),
        ("probe", "nosuch", "mcp"),
        ("probe", "echo", "mcp"),
    ]
    assert calls[0].arguments == {"text": "hi"}
    assert [c.status for c in calls] == ["ok", "error", "error", "rejected"]
    assert calls[1].error_text == "HTTP 403 Forbidden"
    assert calls[2].error_text == "Tool probe-nosuch was not found."
    assert calls[3].denial_kind == "user-rejected"


# --- tool classes ----------------------------------------------------------------


def test_both_model_families_spellings_get_their_class_and_canonical_arguments(
    home: Path, logs: Path
) -> None:
    patch = (
        "*** Begin Patch\n*** Add File: /work/app/notes.txt\n+first line\n+token=abc\n*** End Patch"
    )
    ok = {"success": {"path": "/work/app/f"}}
    c = Conversation("a1")
    c.user("go")
    c.call("ApplyPatch", {"patch": patch}, ok, case=12)
    c.call("Write", {"path": "/work/app/config.env", "contents": "API_TOKEN=x\n"}, ok, case=12)
    c.call(
        "StrReplace", {"path": "/work/app/a.py", "old_string": "a", "new_string": "b"}, ok, case=12
    )
    c.call("ReadFile", {"path": "/work/app/a.py"}, ok, case=8)
    c.call("Read", {"path": "/work/app/a.py"}, ok, case=8)
    c.call("rg", {"pattern": "x", "path": "/work/app"}, ok, case=5)
    c.call("Grep", {"pattern": "x", "path": "/work/app"}, ok, case=5)
    c.call("Glob", {"glob_pattern": "*.py", "target_directory": "/work/app"}, ok, case=4)
    c.call("Delete", {"path": "/work/app/old.txt"}, ok, case=3)
    c.call("Subagent", {"prompt": "look", "subagent_type": "explore"}, ok, case=19)
    c.call("Task", {"prompt": "look", "subagent_type": "generalPurpose"}, ok, case=19)
    c.call("TodoWrite", {"todos": []}, ok, case=9)
    c.write(home / "chats")

    calls = _calls(_read(home, logs)["cursor:a1"])
    found = [(c.tool_name, c.tool_class, c.canonical_arguments) for c in calls]

    assert found == [
        (
            "ApplyPatch",
            "file_edit",
            {
                "edits": [
                    {
                        "path": "/work/app/notes.txt",
                        "kind": "add",
                        "new_text": "first line\ntoken=abc",
                    }
                ],
                "path": "/work/app/notes.txt",
                "new_text": "first line\ntoken=abc",
            },
        ),
        ("Write", "file_write", {"path": "/work/app/config.env", "content": "API_TOKEN=x\n"}),
        ("StrReplace", "file_edit", {"path": "/work/app/a.py", "new_text": "b"}),
        ("ReadFile", "file_read", {"path": "/work/app/a.py"}),
        ("Read", "file_read", {"path": "/work/app/a.py"}),
        ("rg", "file_search", {"path": "/work/app"}),
        ("Grep", "file_search", {"path": "/work/app"}),
        ("Glob", "file_search", {"path": "/work/app"}),
        ("Delete", "file_write", {"path": "/work/app/old.txt"}),
        ("Subagent", "delegate", {"agent": "explore"}),
        ("Task", "delegate", {"agent": "generalPurpose"}),
        ("TodoWrite", "other", {}),
    ]


def test_a_tool_the_table_does_not_know_has_no_class(home: Path, logs: Path) -> None:
    c = Conversation("a1")
    c.user("go")
    c.call("RecordScreen", {}, {"success": {}})
    c.write(home / "chats")

    [call] = _calls(_read(home, logs)["cursor:a1"])

    assert call.tool_class is None


# --- times ---------------------------------------------------------------------


def test_a_turn_takes_its_steps_time_and_a_call_its_duration(home: Path, logs: Path) -> None:
    c = Conversation("a1")
    c.user("go")
    c.call("Shell", {"command": "ls"}, SHELL_OK, duration=250)
    c.say("done")
    c.write(home / "chats")

    session = _read(home, logs)["cursor:a1"]

    user, call_turn, text_turn = session.turns
    assert user.occurred_at == T0 + timedelta(milliseconds=100)
    assert call_turn.occurred_at == T0 + timedelta(milliseconds=200)
    assert call_turn.tool_calls[0].duration_ms == 250
    assert call_turn.occurred_at is not None and text_turn.occurred_at is not None
    assert text_turn.occurred_at > call_turn.occurred_at
    assert (
        session.last_activity_at is not None and session.last_activity_at >= text_turn.occurred_at
    )


def test_last_activity_at_takes_a_calls_completion_not_only_its_start(
    home: Path, logs: Path
) -> None:
    """A sub-agent store has no `meta.json`, so a long call's own time is the
    only signal: the newest step is when it finished, not when it started."""
    parent = Conversation("p1")
    parent.user("go")
    call = parent.call(
        "Task",
        {"prompt": "look", "subagent_type": "explore"},
        {"success": {"agentId": "c1"}},
        case=19,
    )
    parent.write(home / "chats")
    child = Conversation(
        "c1",
        subagent={
            "parentAgentId": "p1",
            "rootParentAgentId": "p1",
            "toolCallId": call,
            "typeName": "explore",
        },
    )
    child.user("look")
    child.call("Shell", {"command": "sleep 10"}, SHELL_OK, duration=10_000)
    child.write(home / "chats")

    sub = _read(home, logs)["cursor:p1:c1"]

    call_turn = sub.turns[-1]
    assert call_turn.occurred_at is not None
    assert call_turn.tool_calls[0].duration_ms == 10_000
    assert sub.last_activity_at == call_turn.occurred_at + timedelta(milliseconds=10_000)


# --- usage from the debug log --------------------------------------------------


def test_each_usage_line_is_a_response_with_input_apart_from_the_cache(
    home: Path, logs: Path
) -> None:
    c = Conversation("a1")
    c.user("go")
    c.call("Shell", {"command": "ls"}, SHELL_OK)
    c.say("done")
    c.write(home / "chats")
    at = T0 + timedelta(seconds=1)
    cs.write_log(
        logs,
        [
            cs.turn_start(at, "a1", "req-1"),
            cs.usage(at, "gpt-5.5", input_tokens=4605, output_tokens=42, cache_read=2302),
            cs.usage(
                at,
                "gpt-5.5",
                input_tokens=4742,
                output_tokens=40,
                cache_read=2371,
                cache_write=100,
                max_tokens=200000,
            ),
        ],
    )

    session = _read(home, logs)["cursor:a1"]

    first, second = session.responses
    assert first.response_id == "req-1:1"
    assert first.request_id == "req-1"
    assert first.model == "gpt-5.5"
    assert first.final is True
    assert first.usage.input_tokens == 4605 - 2302
    assert first.usage.cache_read_input_tokens == 2302
    assert first.usage.cache_creation_input_tokens is None
    assert first.usage.output_tokens == 42
    assert first.context_window is None
    assert second.usage.input_tokens == 4742 - 2371 - 100
    assert second.usage.cache_creation_input_tokens == 100
    assert second.context_window == 200000
    assert session.model == "gpt-5.5"
    assert session.entrypoint == "headless"
    assert session.mcp_log_state == "applied"


def test_without_usage_lines_a_turns_totals_are_its_one_response(home: Path, logs: Path) -> None:
    """The standard CLI runs the agent loop on Cursor's server and writes no
    usage line; print mode puts the turn's totals on its outcome instead."""
    c = Conversation("a1")
    c.user("go")
    c.call("Shell", {"command": "ls"}, SHELL_OK)
    c.say("done")
    c.write(home / "chats")
    start, end = T0 + timedelta(seconds=1), T0 + timedelta(seconds=3)
    cs.write_log(
        logs,
        [
            cs.turn_start(start, "a1", "req-1"),
            cs.turn_outcome(
                end,
                "a1",
                "req-1",
                input_tokens=1373,
                output_tokens=120,
                cache_read=4119,
                model_intent="claude-4.5-sonnet",
            ),
        ],
    )

    session = _read(home, logs)["cursor:a1"]

    (response,) = session.responses
    assert response.response_id == "req-1:turn"
    assert response.request_id == "req-1"
    assert response.model == "claude-4.5-sonnet"
    assert response.occurred_at == end
    assert response.final is True
    assert response.usage.input_tokens == 1373
    assert response.usage.cache_read_input_tokens == 4119
    assert response.usage.cache_creation_input_tokens is None
    assert response.usage.output_tokens == 120


def test_a_turns_totals_add_nothing_where_its_usage_lines_exist(home: Path, logs: Path) -> None:
    """The local-inference build writes both: its lines are the responses, and
    the totals are their sum, so counting both would count every token twice."""
    c = Conversation("a1")
    c.user("go")
    c.say("done")
    c.user("again")
    c.say("done again")
    c.write(home / "chats")
    at = T0 + timedelta(seconds=1)
    cs.write_log(
        logs,
        [
            cs.turn_start(at, "a1", "req-1"),
            cs.usage(at, "gpt-5.5", input_tokens=2672, output_tokens=60, cache_read=2004),
            cs.turn_outcome(at, "a1", "req-1", input_tokens=668, output_tokens=60, cache_read=2004),
            cs.turn_start(at, "a1", "req-2"),
            cs.turn_outcome(at, "a1", "req-2", input_tokens=50, output_tokens=5),
        ],
    )

    ids = [r.response_id for r in _read(home, logs)["cursor:a1"].responses]
    assert ids == ["req-1:1", "req-2:turn"]


def test_a_turn_with_no_totals_and_no_usage_lines_has_no_response(home: Path, logs: Path) -> None:
    c = Conversation("a1")
    c.user("go")
    c.say("done")
    c.write(home / "chats")
    at = T0 + timedelta(seconds=1)
    cs.write_log(logs, [cs.turn_start(at, "a1", "req-1")])

    assert _read(home, logs)["cursor:a1"].responses == ()


def test_a_turn_links_to_its_response_only_when_the_counts_agree(home: Path, logs: Path) -> None:
    c = Conversation("a1")
    c.user("go")
    c.call("Shell", {"command": "ls"}, SHELL_OK)
    c.say("done")
    c.write(home / "chats")
    at = T0 + timedelta(seconds=1)
    lines = [cs.turn_start(at, "a1", "req-1")] + [
        cs.usage(at, "gpt-5.5", input_tokens=10, output_tokens=1) for _ in range(2)
    ]
    cs.write_log(logs, lines)

    linked = [t.response_id for t in _read(home, logs)["cursor:a1"].turns]
    assert linked == [None, "req-1:1", "req-1:2"]

    cs.write_log(logs, [*lines, cs.usage(at, "gpt-5.5", input_tokens=10, output_tokens=1)])
    assert [t.response_id for t in _read(home, logs)["cursor:a1"].turns] == [None, None, None]


def test_another_conversations_usage_is_not_this_sessions(home: Path, logs: Path) -> None:
    for agent in ("a1", "a2"):
        c = Conversation(agent)
        c.user("go")
        c.write(home / "chats")
    at = T0 + timedelta(seconds=1)
    cs.write_log(
        logs,
        [
            cs.turn_start(at, "a1", "req-1"),
            cs.usage(at, "gpt-5.5", input_tokens=10, output_tokens=1),
            cs.turn_start(at, "a2", "req-2", surface="interactive"),
            cs.usage(at, "claude-sonnet-4-5", input_tokens=20, output_tokens=2),
        ],
    )

    sessions = _read(home, logs)

    assert [r.response_id for r in sessions["cursor:a1"].responses] == ["req-1:1"]
    assert [r.model for r in sessions["cursor:a2"].responses] == ["claude-sonnet-4-5"]
    assert sessions["cursor:a2"].entrypoint == "interactive"


def test_without_a_log_a_session_has_no_responses_and_says_why(home: Path, logs: Path) -> None:
    c = Conversation("a1")
    c.user("go")
    c.write(home / "chats")

    session = _read(home, logs)["cursor:a1"]
    assert session.responses == ()
    assert session.mcp_log_state == "no_log_root"

    cs.write_log(logs, [cs.turn_start(T0, "other", "req-9")])
    assert _read(home, logs)["cursor:a1"].mcp_log_state == "no_log_for_session"


# --- sub-agents ------------------------------------------------------------------


def test_a_sub_agent_is_its_roots_child_with_its_own_usage(home: Path, logs: Path) -> None:
    parent = Conversation("p1")
    parent.user("go")
    call = parent.call(
        "Task",
        {"prompt": "look", "subagent_type": "explore"},
        {"success": {"agentId": "c1"}},
        case=19,
    )
    parent.say("done")
    parent.write(home / "chats")
    child = Conversation(
        "c1",
        subagent={
            "parentAgentId": "p1",
            "rootParentAgentId": "p1",
            "toolCallId": call,
            "typeName": "explore",
        },
    )
    child.user("look")
    child.call("Shell", {"command": "ls -la"}, SHELL_OK)
    child.say("Listed.")
    child.write(home / "chats")
    at = T0 + timedelta(seconds=1)
    cs.write_log(
        logs,
        [
            cs.turn_start(at, "p1", "req-1"),
            cs.usage(at, "gpt-5.5", input_tokens=100, output_tokens=5),
            cs.usage(at, "gpt-5.5", input_tokens=40, output_tokens=2, subagent_call=call),
            cs.usage(at, "gpt-5.5", input_tokens=41, output_tokens=3, subagent_call=call),
            cs.usage(at, "gpt-5.5", input_tokens=120, output_tokens=6),
        ],
    )

    sessions = _read(home, logs)
    main, sub = sessions["cursor:p1"], sessions["cursor:p1:c1"]

    assert [r.usage.output_tokens for r in main.responses] == [5, 6]
    assert [r.usage.output_tokens for r in sub.responses] == [2, 3]
    assert all(r.is_sidechain for r in sub.responses)
    assert all(t.is_sidechain for t in sub.turns)
    assert not any(t.is_sidechain for t in main.turns)
    assert sub.working_directory == "/work/app"
    assert sub.entrypoint == "headless"
    assert [t.response_id for t in sub.turns] == [None, f"req-1:{call}:1", f"req-1:{call}:2"]
    # One response, one id, across every session: the child's are not its parent's.
    ids = [r.response_id for s in (main, sub) for r in s.responses]
    assert len(ids) == len(set(ids))


# --- client errors and MCP failures --------------------------------------------


def test_a_turn_that_ended_in_an_error_is_an_error_turn_with_cursors_words(
    home: Path, logs: Path
) -> None:
    c = Conversation("a1")
    c.user("go")
    c.call("Shell", {"command": "ls"}, SHELL_OK)
    c.write(home / "chats")
    words = "Unauthorized (HTTP 401): your API key/token was rejected — refresh it and try again."
    cs.write_transcript(
        home,
        "/work/app",
        "a1",
        [
            {"role": "user", "message": {"content": [{"type": "text", "text": "go"}]}},
            {"type": "turn_ended", "status": "error", "error": words},
        ],
    )
    at = T0 + timedelta(seconds=5)
    cs.write_log(
        logs, [cs.turn_start(T0, "a1", "req-1"), cs.turn_error(at, "a1", "req-1", "redacted")]
    )

    session = _read(home, logs)["cursor:a1"]

    error = session.turns[-1]
    assert (error.role, error.key, error.text, error.occurred_at) == ("error", "e1", words, at)
    assert [t.position for t in session.turns] == list(range(len(session.turns)))


def test_with_no_transcript_the_logs_error_text_is_the_error_turn(home: Path, logs: Path) -> None:
    c = Conversation("a1")
    c.user("go")
    c.write(home / "chats")
    cs.write_log(
        logs,
        [
            cs.turn_start(T0, "a1", "req-1"),
            cs.turn_error(T0 + timedelta(seconds=5), "a1", "req-1", "Rate limited"),
        ],
    )

    assert _read(home, logs)["cursor:a1"].turns[-1].text == "Rate limited"


def test_an_untexted_error_does_not_misalign_a_later_errors_time(home: Path, logs: Path) -> None:
    """With no transcript, a textless error outcome is dropped before pairing
    times, so a later error keeps its own time rather than an earlier one's."""
    c = Conversation("a1")
    c.user("go")
    c.write(home / "chats")
    second_at = T0 + timedelta(seconds=10)
    cs.write_log(
        logs,
        [
            cs.turn_start(T0, "a1", "req-1"),
            cs.turn_error(T0 + timedelta(seconds=5), "a1", "req-1", None),
            cs.turn_error(second_at, "a1", "req-2", "Rate limited"),
        ],
    )

    error = _read(home, logs)["cursor:a1"].turns[-1]
    assert (error.text, error.occurred_at) == ("Rate limited", second_at)


def test_a_server_that_failed_to_load_is_a_failed_connection(home: Path, logs: Path) -> None:
    c = Conversation("a1")
    c.user("go")
    c.write(home / "chats")
    cs.write_log(
        logs,
        [
            cs.mcp_failed(
                T0,
                "deadhttp",
                "SSE error: TypeError: fetch failed: connect ECONNREFUSED 127.0.0.1:18999",
            ),
            cs.mcp_failed(T0, "badbin", "spawn nosuch ENOENT", transport=None),
            cs.turn_start(T0, "a1", "req-1"),
        ],
    )

    connections = {m.server: m for m in _read(home, logs)["cursor:a1"].mcp_connections}

    assert connections["deadhttp"].connected is False
    assert connections["deadhttp"].transport == "sse"
    assert connections["deadhttp"].failure_category == "network"
    assert connections["badbin"].transport is None
    assert connections["badbin"].failure_category == "spawn"


# --- failures --------------------------------------------------------------------


def test_no_chats_directory_is_no_sessions_and_no_failure(home: Path, logs: Path) -> None:
    assert _reader(home, logs).collect(Window(since=None)) == ([], [])


def test_a_store_with_no_conversation_yet_is_no_session(home: Path, logs: Path) -> None:
    import sqlite3

    directory = home / "chats" / "abc" / "a1"
    directory.mkdir(parents=True)
    connection = sqlite3.connect(directory / "store.db")
    connection.executescript(
        "CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB); CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
    )
    connection.close()

    assert _reader(home, logs).collect(Window(since=None)) == ([], [])


def test_a_malformed_meta_row_is_a_reported_failure(home: Path, logs: Path) -> None:
    import sqlite3

    directory = home / "chats" / "abc" / "a1"
    directory.mkdir(parents=True)
    connection = sqlite3.connect(directory / "store.db")
    connection.executescript(
        "CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB); CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
        "INSERT INTO meta VALUES ('0', 'not hex');"
    )
    connection.commit()
    connection.close()

    sessions, failures = _reader(home, logs).collect(Window(since=None))

    assert sessions == []
    assert len(failures) == 1 and "meta row" in failures[0].message


def test_a_store_without_the_tables_is_an_unsupported_schema(home: Path, logs: Path) -> None:
    import sqlite3

    directory = home / "chats" / "abc" / "a1"
    directory.mkdir(parents=True)
    sqlite3.connect(directory / "store.db").close()

    _, failures = _reader(home, logs).collect(Window(since=None))

    assert len(failures) == 1 and "unsupported schema" in failures[0].message


# --- growth and the window -------------------------------------------------------


def test_collect_file_returns_a_session_only_when_it_changed(home: Path, logs: Path) -> None:
    reader = _reader(home, logs)
    c = Conversation("a1")
    c.user("go")
    store = c.write(home / "chats")

    first, failures = reader.collect_file(store)
    assert failures == [] and [s.session_id for s in first] == ["cursor:a1"]
    assert reader.collect_file(store) == ([], [])

    c.call("Shell", {"command": "ls"}, SHELL_OK)
    c.write(home / "chats")
    again, _ = reader.collect_file(store.parent / "store.db-wal")
    assert [len(s.turns) for s in again] == [2]

    log = cs.write_log(
        logs,
        [
            cs.turn_start(T0, "a1", "req-1"),
            cs.usage(T0, "gpt-5.5", input_tokens=10, output_tokens=1),
        ],
    )
    by_log, _ = reader.collect_file(log)
    assert [len(s.responses) for s in by_log] == [1]

    assert reader.collect_file(home / "cli-config.json") == ([], [])


def test_a_cold_read_matches_what_the_incremental_reads_returned(home: Path, logs: Path) -> None:
    reader = _reader(home, logs)
    c = Conversation("a1")
    c.user("go")
    store = c.write(home / "chats")
    held = {s.session_id: s for s in reader.collect_file(store)[0]}
    c.call("Shell", {"command": "ls"}, SHELL_OK)
    c.write(home / "chats")
    log = cs.write_log(
        logs,
        [
            cs.turn_start(T0, "a1", "req-1"),
            cs.usage(T0, "gpt-5.5", input_tokens=10, output_tokens=1),
        ],
    )
    for path in (store, log):
        held.update({s.session_id: s for s in reader.collect_file(path)[0]})

    assert held == _read(home, logs)


def test_a_deleted_log_still_rebuilds_the_session_it_had_enriched(home: Path, logs: Path) -> None:
    """A watcher reports a log's deletion by naming it again in `collect_file`.
    The session it enriched must still be rebuilt, this time without it."""
    reader = _reader(home, logs)
    c = Conversation("a1")
    c.user("go")
    c.write(home / "chats")
    log = cs.write_log(
        logs,
        [
            cs.turn_start(T0, "a1", "req-1"),
            cs.usage(T0, "gpt-5.5", input_tokens=10, output_tokens=1),
        ],
    )
    first, _ = reader.collect_file(log)
    assert [r.usage.output_tokens for s in first for r in s.responses] == [1]

    log.unlink()
    again, _ = reader.collect_file(log)

    assert [s.session_id for s in again] == ["cursor:a1"]
    assert again[0].responses == ()


def test_a_changed_server_metadata_file_rebuilds_every_session(home: Path, logs: Path) -> None:
    """`SERVER_METADATA.json` names no project directory to narrow a rebuild
    to, so a change to it rebuilds every session a watcher has read; only
    the one its new name reaches comes back changed."""
    reader = _reader(home, logs)
    c = Conversation("a1", cwd="/work/app")
    c.user("go")
    c.call(
        "CallMcpTool",
        {"server": "user-probe", "toolName": "echo", "arguments": {}},
        {"success": {"content": [{"text": {"text": "echo"}}]}},
        result="echo",
        case=15,
    )
    store = c.write(home / "chats")
    first, _ = reader.collect_file(store)
    assert [call.mcp_server for s in first for call in _calls(s)] == ["user-probe"]

    metadata = cs.write_server_metadata(home, "/work/app", "user-probe", "probe")
    again, failures = reader.collect_file(metadata)

    assert failures == []
    assert [s.session_id for s in again] == ["cursor:a1"]
    assert [call.mcp_server for call in _calls(again[0])] == ["probe"]


def test_the_window_keeps_sessions_active_since_it(home: Path, logs: Path) -> None:
    c = Conversation("a1")
    c.user("go")
    c.write(home / "chats")

    reader = _reader(home, logs)
    assert reader.collect(Window(since=T0 - timedelta(days=1)))[0] != []
    assert reader.collect(Window(since=T0 + timedelta(days=1)))[0] == []


def test_the_default_readers_read_cursor_from_its_named_root(home: Path) -> None:
    c = Conversation("a1")
    c.user("go")
    c.write(home / "chats")

    result = collect(
        parse_kind_filter(["cursor"]), Window(since=None), default_readers(cursor_root=home)
    )

    assert [s.session_id for s in result.sessions] == ["cursor:a1"]


# --- who is signed in ------------------------------------------------------------


def test_cursors_account_is_its_cli_configs_auth_info(home: Path) -> None:
    home.mkdir(parents=True)
    (home / "cli-config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "authInfo": {
                    "email": "dev@example.com",
                    "displayName": "Dev One",
                    "userId": 1234,
                    "authId": "auth0|user_1",
                    "teamId": 7,
                },
            }
        ),
        encoding="utf-8",
    )

    account = signed_in("cursor", config_dir=home)

    assert account is not None
    assert (
        account.kind,
        account.account_id,
        account.email,
        account.display_name,
        account.plan,
    ) == (
        "cursor",
        "1234",
        "dev@example.com",
        "Dev One",
        None,
    )
    desktop = Path(os.environ["VSCODE_APPDATA"]) / "Cursor" / "User" / "globalStorage"
    assert sign_in_records("cursor", config_dir=home) == (
        home / "cli-config.json",
        desktop / "state.vscdb",
        desktop / "state.vscdb-wal",
    )


@pytest.mark.parametrize(
    "document",
    [{"version": 1}, {"authInfo": {"email": "x@example.com"}}, {"authInfo": "nope"}, []],
)
def test_without_a_user_cursor_names_no_one(home: Path, document: object) -> None:
    home.mkdir(parents=True)
    (home / "cli-config.json").write_text(json.dumps(document), encoding="utf-8")

    assert signed_in("cursor", config_dir=home) is None


def test_an_auth_id_names_the_account_when_there_is_no_user_id(home: Path) -> None:
    home.mkdir(parents=True)
    (home / "cli-config.json").write_text(json.dumps({"authInfo": {"authId": "auth0|u"}}), "utf-8")

    account = signed_in("cursor", config_dir=home)

    assert account is not None and account.account_id == "auth0|u"
