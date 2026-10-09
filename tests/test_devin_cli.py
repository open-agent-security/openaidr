"""The `devin-cli` reader, against synthetic stores shaped like Devin CLI 3000.11.3's."""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from openaidr.collector import collect, default_readers
from openaidr.kinds import parse_kind_filter
from openaidr.model import Session
from openaidr.readers.base import Window
from openaidr.readers.devin_cli import DevinCliReader
from tests.fixtures.devin_store import (
    T0,
    Node,
    StoredSession,
    append_nodes,
    assistant,
    call,
    create_store,
    description_failure,
    lock,
    log_line,
    mcp_http,
    mcp_stdio,
    metrics,
    result,
    run_log,
    startup,
    user,
    write_session,
)

ALL = Window(since=None)


def _read(root: Path) -> tuple[list[Session], list[str]]:
    sessions, failures = DevinCliReader(root=root).collect(ALL)
    return sessions, [failure.message for failure in failures]


def _one(root: Path, session_id: str = "devin-cli:brave-otter") -> Session:
    sessions, failures = _read(root)
    assert failures == []
    return next(s for s in sessions if s.session_id == session_id)


def _simple(root: Path, nodes: list[Node], main: int, **fields: Any) -> Path:
    store = create_store(root)
    write_session(store, StoredSession(id="brave-otter", nodes=nodes, main_chain_id=main, **fields))
    return store


# --- discovery and failures -------------------------------------------------


def test_no_data_root_is_no_sessions_and_no_failure(tmp_path: Path) -> None:
    assert _read(tmp_path / "absent") == ([], [])


def test_a_root_without_a_store_is_no_sessions_and_no_failure(tmp_path: Path) -> None:
    (tmp_path / "cli").mkdir()
    assert _read(tmp_path / "cli") == ([], [])


def test_a_relative_root_does_not_crash_the_store_connection(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "cli"
    _mcp_session(root)
    monkeypatch.chdir(tmp_path)
    sessions, failures = DevinCliReader(root=Path("cli")).collect(ALL)
    assert failures == []
    assert [s.session_id for s in sessions] == ["devin-cli:brave-otter"]


def test_collect_file_recognizes_an_absolute_path_against_a_relative_root(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(store, StoredSession(id="a", nodes=[Node(1, None, user("x"))], main_chain_id=1))
    monkeypatch.chdir(tmp_path)
    sessions, failures = DevinCliReader(root=Path("cli")).collect_file(store)
    assert failures == []
    assert [s.session_id for s in sessions] == ["devin-cli:a"]


def test_a_store_missing_a_required_table_is_an_unsupported_schema(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    root.mkdir()
    sqlite3.connect(root / "sessions.db").execute(
        "CREATE TABLE sessions (id TEXT)"
    ).connection.close()
    sessions, failures = _read(root)
    assert sessions == []
    assert len(failures) == 1 and "unsupported" in failures[0]


def test_a_newer_schema_is_read_and_reported(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = create_store(root, migrations=18)
    write_session(
        store, StoredSession(id="brave-otter", nodes=[Node(1, None, user("hi"))], main_chain_id=1)
    )
    sessions, failures = _read(root)
    assert [s.session_id for s in sessions] == ["devin-cli:brave-otter"]
    assert len(failures) == 1 and "V18" in failures[0]


def test_an_unparseable_node_is_reported_and_skipped(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = _simple(root, [Node(1, None, user("hi"))], main=1)
    db = sqlite3.connect(store)
    db.execute(
        "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at)"
        " VALUES ('brave-otter', 2, 1, '{not json', ?)",
        (T0,),
    )
    db.execute("UPDATE sessions SET main_chain_id = 2")
    db.commit()
    db.close()
    sessions, failures = _read(root)
    assert len(failures) == 1 and "node 2" in failures[0]
    assert [t.text for t in sessions[0].turns] == ["hi"]


def test_an_unparseable_chain_head_keeps_its_own_branch_not_an_abandoned_one(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cli"
    store = _simple(
        root,
        [
            Node(1, None, user("q")),
            Node(2, 1, assistant("abandoned try", request_id="r1", metrics=metrics())),
        ],
        main=1,
    )
    db = sqlite3.connect(store)
    # The declared head (node 3) is malformed, but its own parent link says
    # it continues node 1 directly -- not the abandoned reply at node 2,
    # which merely has a higher row_id.
    db.execute(
        "INSERT INTO message_nodes (session_id, node_id, parent_node_id, chat_message, created_at)"
        " VALUES ('brave-otter', 3, 1, '{not json', ?)",
        (T0,),
    )
    db.execute("UPDATE sessions SET main_chain_id = 3")
    db.commit()
    db.close()
    sessions, failures = _read(root)
    assert len(failures) == 1 and "node 3" in failures[0]
    assert [t.text for t in sessions[0].turns] == ["q"]


def test_hidden_sessions_are_not_read(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(
        store,
        StoredSession(id="helper", hidden=1, nodes=[Node(1, None, user("x"))], main_chain_id=1),
    )
    assert _read(root) == ([], [])


def test_the_window_is_applied_to_last_activity(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(
        store,
        StoredSession(
            id="old", last_activity_at=T0, nodes=[Node(1, None, user("a"))], main_chain_id=1
        ),
    )
    write_session(
        store,
        StoredSession(
            id="new",
            last_activity_at=T0 + 86_400,
            nodes=[Node(1, None, user("b"))],
            main_chain_id=1,
        ),
    )
    since = datetime.fromtimestamp(T0 + 3600, UTC)
    sessions, _ = DevinCliReader(root=root).collect(Window(since=since))
    assert [s.session_id for s in sessions] == ["devin-cli:new"]


def test_the_window_considers_a_nodes_own_later_timestamp(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(
        store,
        StoredSession(
            id="brave-otter",
            last_activity_at=T0,
            nodes=[
                Node(1, None, user("q")),
                Node(2, 1, assistant("a", request_id="r1", metrics=metrics(), created_at=T0 + 100)),
            ],
            main_chain_id=2,
        ),
    )
    since = datetime.fromtimestamp(T0 + 50, UTC)
    sessions, _ = DevinCliReader(root=root).collect(Window(since=since))
    assert [s.session_id for s in sessions] == ["devin-cli:brave-otter"]


def test_a_subagents_last_activity_is_scoped_to_its_own_tree(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(
        store,
        StoredSession(
            id="brave-otter",
            last_activity_at=T0 + 100,
            main_chain_id=1,
            subagents=[("explorer-1", 11)],
            nodes=[
                Node(1, None, user("q")),
                Node(10, None, user("look", typed=False)),
                Node(
                    11,
                    10,
                    assistant(
                        calls=[call("c9", "mcp__gh__search", {})],
                        request_id="s1",
                        metrics=metrics(),
                    ),
                ),
                Node(12, 11, result("c9", "x", success=True)),
            ],
        ),
    )
    sessions, _ = DevinCliReader(root=root).collect(ALL)
    by_id = {s.session_id: s for s in sessions}
    assert by_id["devin-cli:brave-otter"].last_activity_at == datetime.fromtimestamp(T0 + 100, UTC)
    assert by_id["devin-cli:brave-otter:explorer-1"].last_activity_at == datetime.fromtimestamp(
        T0, UTC
    )

    since = datetime.fromtimestamp(T0 + 50, UTC)
    filtered, _ = DevinCliReader(root=root).collect(Window(since=since))
    assert [s.session_id for s in filtered] == ["devin-cli:brave-otter"]


def test_placement_uses_a_nodes_effective_time_not_the_stale_column(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(
        store,
        StoredSession(
            id="brave-otter",
            last_activity_at=T0 + 60,
            nodes=[
                Node(1, None, user("q")),
                Node(2, 1, assistant("a", request_id="r1", metrics=metrics(), created_at=T0 + 200)),
            ],
            main_chain_id=2,
        ),
    )
    lock(root, "brave-otter", 9)
    # The locked pid has logs around both the stale column (60) and the
    # node's true, later effective time (200) -- a pid reused in between.
    run_log(
        root,
        9,
        [*startup(50), *mcp_stdio(55, "wrong-server"), log_line(59, "INFO", "chisel", "x")],
        stamp="20260101-000050",
    )
    run_log(
        root,
        9,
        [*startup(190), *mcp_stdio(195, "right-server"), log_line(205, "INFO", "chisel", "x")],
        stamp="20260101-000150",
    )
    session = _one(root)
    assert session.mcp_log_state == "applied"
    assert [c.server for c in session.mcp_connections] == ["right-server"]


# --- session fields and the conversation tree --------------------------------


def test_session_fields(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _simple(
        root,
        [
            Node(1, None, user("fix the build")),
            Node(2, 1, assistant("done", request_id="r1", metrics=metrics(), model="swe-2-high")),
        ],
        main=2,
        title="Fix the build",
        working_directory="/work/app",
    )
    session = _one(root)
    assert session.agent_kind == "devin-cli"
    assert session.source == "devin"
    assert session.started_at == datetime.fromtimestamp(T0, UTC)
    assert session.last_activity_at == datetime.fromtimestamp(T0 + 60, UTC)
    assert session.working_directory == "/work/app"
    assert session.model == "swe-2-high"
    assert session.initial_prompt == "fix the build"
    assert session.title is None
    assert session.generated_title == "Fix the build"
    assert session.git_branch is None
    assert session.compactions_recorded is True
    assert session.subagents_recorded is True


def test_turns_follow_the_main_chain_not_abandoned_branches(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _simple(
        root,
        [
            Node(1, None, user("q")),
            Node(2, 1, assistant("first try", request_id="r1", metrics=metrics())),
            Node(3, 1, assistant("regenerated", request_id="r2", metrics=metrics())),
        ],
        main=3,
    )
    session = _one(root)
    assert [(t.key, t.role, t.text) for t in session.turns] == [
        ("1", "user", "q"),
        ("3", "assistant", "regenerated"),
    ]
    assert [t.position for t in session.turns] == [0, 1]
    # The abandoned branch was a real request, so its usage still counts.
    assert sorted(r.response_id for r in session.responses) == ["r1", "r2"]


def test_a_session_reports_the_model_of_the_newest_response_not_the_last_appearing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cli"
    store = _simple(
        root,
        [
            Node(1, None, user("q")),
            Node(2, 1, assistant("r1a", request_id="r1", model="model-a", metrics=metrics())),
            Node(3, 2, assistant("r2", request_id="r2", model="model-b", metrics=metrics())),
        ],
        main=3,
    )
    before = _one(root)
    assert before.model == "model-b"
    # A later regeneration appends a newer record under the earlier response
    # id, carrying a different model.
    append_nodes(
        store,
        "brave-otter",
        [Node(4, 3, assistant("r1-regen", request_id="r1", model="model-c", metrics=metrics()))],
        main_chain_id=4,
    )
    after = _one(root)
    assert after.model == "model-c"


def test_a_harness_written_user_node_is_context_not_a_turn(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _simple(
        root,
        [
            Node(1, None, {"role": "system", "content": "You are Devin."}),
            Node(2, 1, user("real ask")),
            Node(3, 2, user("continue", typed=False)),
        ],
        main=3,
    )
    session = _one(root)
    assert [t.text for t in session.turns] == ["real ask"]
    assert [(c.source, c.text) for c in session.context_items] == [
        ("system", "You are Devin."),
        ("injected", "continue"),
    ]
    assert session.context_items[0].span == "devin-cli:brave-otter:context:1"
    assert session.context_items[1].occurred_at == datetime.fromtimestamp(T0, UTC)


def test_a_context_items_span_follows_its_node_not_its_position(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = _simple(
        root,
        [
            Node(1, None, user("q")),
            Node(2, 1, user("continue-a", typed=False)),
            Node(3, 1, user("continue-b", typed=False)),
        ],
        main=2,
    )
    before = _one(root)
    assert [(c.text, c.span) for c in before.context_items] == [
        ("continue-a", "devin-cli:brave-otter:context:2")
    ]
    # Regeneration moves main_chain_id to the sibling branch -- a different
    # context node now sits at the same position in `context`.
    append_nodes(store, "brave-otter", [], main_chain_id=3)
    after = _one(root)
    assert [(c.text, c.span) for c in after.context_items] == [
        ("continue-b", "devin-cli:brave-otter:context:3")
    ]


def test_content_given_as_parts_is_joined(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    message = {
        "role": "user",
        "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
    }
    message["metadata"] = {"is_user_input": True}
    _simple(root, [Node(1, None, message)], main=1)
    assert _one(root).turns[0].text == "a\nb"


def test_initial_prompt_falls_back_to_typed_prompt_history(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _simple(
        root,
        [Node(1, None, user("continue", typed=False))],
        main=1,
        prompts=[("ls -la", 1), ("ship it", 0)],
    )
    assert _one(root).initial_prompt == "ship it"


def test_a_record_time_prefers_the_messages_own_call_time(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _simple(
        root,
        [
            Node(1, None, user("q"), created_at=T0),
            Node(
                2,
                1,
                assistant("a", created_at="2026-01-01T00:00:05.250Z", metrics=metrics()),
                created_at=T0 + 5,
            ),
            Node(
                3,
                2,
                assistant(
                    "b", request_id="r2", created_at=(T0 + 7) * 1000 + 125, metrics=metrics()
                ),
                created_at=T0 + 7,
            ),
        ],
        main=3,
    )
    session = _one(root)
    base = datetime.fromtimestamp(T0, UTC)
    assert [t.occurred_at for t in session.turns] == [
        base,
        base + timedelta(seconds=5, milliseconds=250),
        base + timedelta(seconds=7, milliseconds=125),
    ]


# --- tool calls ----------------------------------------------------------------


def _calls(root: Path, calls: list[dict], results: list[dict], *, last: bool = False) -> list:
    nodes = [Node(1, None, user("go")), Node(2, 1, assistant(calls=calls, metrics=metrics()))]
    for index, message in enumerate(results):
        nodes.append(Node(3 + index, 2 + index, message))
    tail = 2 + len(results)
    if not last:
        nodes.append(Node(tail + 1, tail, assistant("ok", request_id="r-end", metrics=metrics())))
        tail += 1
    _simple(root, nodes, main=tail)
    return list(_one(root).turns[1].tool_calls)


def test_a_shell_call_and_its_result(tmp_path: Path) -> None:
    [shell] = _calls(
        tmp_path / "cli",
        [call("c1", "exec", {"command": "npm test"})],
        [result("c1", "3 passed", success=True, exit_code=0, cwd="/work/project/pkg")],
    )
    assert shell.span == "devin-cli:brave-otter:2:0"
    assert shell.tool_name == "exec"
    assert shell.mcp_server is None
    assert shell.status == "ok"
    assert shell.arguments == {"command": "npm test"}
    assert shell.result == "3 passed"
    assert shell.result_size == len("3 passed")
    assert shell.provider_call_id == "c1"
    assert shell.exit_code == 0
    assert shell.working_directory == "/work/project/pkg"
    assert shell.tool_class == "shell"
    assert shell.canonical_arguments == {"command": "npm test"}
    assert shell.duration_ms is None


@pytest.mark.parametrize(
    ("meta", "status", "denial"),
    [
        ({"success": True}, "ok", None),
        ({"success": False, "failure_reason": "Rejected"}, "rejected", "user-rejected"),
        (
            {"success": False, "failure_reason": {"Rejected": {"reason": "no"}}},
            "rejected",
            "user-rejected",
        ),
        ({"success": False, "failure_reason": "Blocked"}, "rejected", "permission-rule"),
        ({"success": False, "failure_reason": "ToolError"}, "error", None),
        ({"success": False, "failure_reason": "ValidationError"}, "error", None),
        ({"success": False, "failure_reason": "UnsearchedTool"}, "error", None),
        # Until a stored refusal is observed, a failure with no reason is not
        # called the tool's fault: it may be a person declining.
        ({"success": False}, "unknown", None),
        ({}, "unknown", None),
    ],
)
def test_status_comes_from_the_results_own_record(
    tmp_path: Path, meta: dict, status: str, denial: str | None
) -> None:
    [one] = _calls(
        tmp_path / "cli", [call("c1", "read", {"file_path": "/a"})], [result("c1", "body", **meta)]
    )
    assert (one.status, one.denial_kind) == (status, denial)
    assert one.error_text == ("body" if status == "error" else None)


def test_a_call_without_a_result_is_pending_at_the_tip_and_unknown_behind_it(
    tmp_path: Path,
) -> None:
    [tip] = _calls(tmp_path / "a", [call("c1", "exec", {"command": "sleep 9"})], [], last=True)
    assert tip.status == "pending"
    [behind] = _calls(tmp_path / "b", [call("c1", "exec", {"command": "sleep 9"})], [])
    assert behind.status == "unknown"


def test_an_unmatched_sibling_call_is_pending_while_its_result_tail_is_active(
    tmp_path: Path,
) -> None:
    # c1's result lands first, but no new turn has started: c2 is still
    # pending, not unknown -- its result could still arrive.
    done, waiting = _calls(
        tmp_path / "cli",
        [call("c1", "exec", {"command": "a"}), call("c2", "exec", {"command": "b"}, index=1)],
        [result("c1", "done", success=True)],
        last=True,
    )
    assert done.status == "ok"
    assert waiting.status == "pending"


def test_an_overflowed_result_is_truncated(tmp_path: Path) -> None:
    [one] = _calls(
        tmp_path / "cli",
        [call("c1", "exec", {"command": "cat big"})],
        [result("c1", "head…", success=True, overflow_metadata={"path": "/tmp/x"})],
    )
    assert one.truncated is True


def test_arguments_that_are_not_an_object_are_reported(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    nodes = [
        Node(1, None, user("go")),
        Node(2, 1, assistant(calls=[call("c1", "exec", "[1, 2]")], metrics=metrics())),
    ]
    _simple(root, nodes, main=2)
    sessions, failures = _read(root)
    assert sessions[0].turns[1].tool_calls[0].arguments == {}
    assert len(failures) == 1 and "c1" in failures[0]


def test_an_openai_shaped_call_is_read(tmp_path: Path) -> None:
    shaped = {
        "id": "c1",
        "type": "function",
        "function": {"name": "write", "arguments": '{"file_path": "/a", "content": "x"}'},
    }
    [one] = _calls(tmp_path / "cli", [shaped], [result("c1", "ok", success=True)])
    assert (one.tool_name, one.tool_class) == ("write", "file_write")
    assert one.canonical_arguments == {"path": "/a", "content": "x"}


def test_an_mcp_tool_splits_into_server_and_tool(tmp_path: Path) -> None:
    [one] = _calls(
        tmp_path / "cli",
        [call("c1", "mcp__github__create_issue", {"title": "t"})],
        [result("c1", "ok", success=True)],
    )
    assert (one.mcp_server, one.tool_name, one.tool_class) == ("github", "create_issue", "mcp")


def test_the_generic_mcp_tool_recovers_its_server_and_tool(tmp_path: Path) -> None:
    arguments = {"server_name": "linear", "tool_name": "list_issues", "arguments": {"q": "x"}}
    [one] = _calls(
        tmp_path / "cli",
        [call("c1", "mcp_call_tool", arguments)],
        [result("c1", "[]", success=True)],
    )
    assert (one.mcp_server, one.tool_name, one.tool_class) == ("linear", "list_issues", "mcp")
    assert one.arguments == arguments


@pytest.mark.parametrize(
    ("name", "arguments", "tool_class", "canonical"),
    [
        ("exec", {"command": "ls"}, "shell", {"command": "ls"}),
        ("get_output", {"shell_id": "s"}, "shell_control", {}),
        ("read", {"file_path": "/a", "offset": 1}, "file_read", {"path": "/a"}),
        ("grep", {"pattern": "x"}, "file_search", {}),
        (
            "write",
            {"file_path": "/a", "content": "c"},
            "file_write",
            {"path": "/a", "content": "c"},
        ),
        (
            "edit",
            {"file_path": "/a", "old_string": "o", "new_string": "n"},
            "file_edit",
            {"path": "/a", "new_text": "n"},
        ),
        ("webfetch", {"url": "https://x.test"}, "web_fetch", {"url": "https://x.test"}),
        ("run_subagent", {"task": "t"}, "delegate", {}),
        ("skill", {"name": "deploy"}, "skill", {"skill": "deploy"}),
        ("todo_write", {"todos": []}, "other", {}),
        ("brand_new_tool", {}, None, {}),
    ],
)
def test_tool_class_and_canonical_arguments(
    tmp_path: Path, name: str, arguments: dict, tool_class: str | None, canonical: dict
) -> None:
    [one] = _calls(
        tmp_path / "cli", [call("c1", name, arguments)], [result("c1", "r", success=True)]
    )
    assert one.tool_name == name
    assert one.tool_class == tool_class
    assert one.canonical_arguments == canonical


# --- usage ----------------------------------------------------------------------


def test_one_response_per_request_and_retries_count_once(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    usage = metrics(input_tokens=10, output_tokens=5, cache_read=200, cache_creation=None)
    _simple(
        root,
        [
            Node(1, None, user("q")),
            Node(2, 1, assistant("a", request_id="r1", metrics=usage, finish_reason=None)),
            Node(3, 1, assistant("a", request_id="r1", metrics=usage)),
            Node(4, 3, assistant("b", request_id=None, metrics=metrics())),
        ],
        main=4,
    )
    session = _one(root)
    assert [r.response_id for r in session.responses] == ["r1", "node-4"]
    first = session.responses[0]
    assert (first.request_id, first.model, first.final, first.is_sidechain) == (
        "r1",
        "claude-sonnet-4-5",
        True,
        False,
    )
    assert first.usage.input_tokens == 10
    assert first.usage.output_tokens == 5
    assert first.usage.cache_read_input_tokens == 200
    assert first.usage.cache_creation_input_tokens is None
    assert first.usage.reasoning_tokens is None
    assert [t.response_id for t in session.turns] == [None, "r1", "node-4"]


def test_subagents_are_their_own_sessions_and_nothing_counts_twice(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(
        store,
        StoredSession(
            id="brave-otter",
            main_chain_id=3,
            subagents=[("explorer-1", 11)],
            nodes=[
                Node(1, None, user("q")),
                Node(
                    2,
                    1,
                    assistant(
                        calls=[call("c1", "run_subagent", {"task": "look"})],
                        request_id="p1",
                        metrics=metrics(),
                    ),
                ),
                Node(3, 2, result("c1", "found it", success=True)),
                Node(10, None, user("look", typed=False)),
                Node(11, 10, assistant("found it", request_id="s1", metrics=metrics())),
            ],
        ),
    )
    sessions, failures = _read(root)
    assert failures == []
    by_id = {s.session_id: s for s in sessions}
    parent = by_id["devin-cli:brave-otter"]
    child = by_id["devin-cli:brave-otter:explorer-1"]
    assert [r.response_id for r in parent.responses] == ["p1"]
    assert [r.response_id for r in child.responses] == ["s1"]
    assert all(r.is_sidechain for r in child.responses)
    assert all(t.is_sidechain for t in child.turns)
    assert [t.text for t in child.turns] == ["found it"]


def test_a_compaction_is_read_from_the_nodes_own_metadata(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _simple(
        root,
        [
            Node(1, None, user("q")),
            Node(
                2,
                1,
                user("summary of earlier work", typed=False),
                created_at=T0 + 30,
                metadata={"summarized_from": 1, "num_tokens_preceding": 180_000},
            ),
        ],
        main=2,
    )
    [compaction] = _one(root).compactions
    assert compaction.trigger == "unknown"
    assert compaction.pre_tokens == 180_000
    assert compaction.dropped_tokens is None
    assert compaction.occurred_at == datetime.fromtimestamp(T0 + 30, UTC)


# --- MCP connections from the run log ----------------------------------------------


def _mcp_session(root: Path, *, calls: list[dict] | None = None) -> Path:
    nodes = [Node(1, None, user("go"))]
    if calls:
        nodes.append(Node(2, 1, assistant(calls=calls, metrics=metrics())))
        nodes.append(Node(3, 2, result(calls[0]["id"], "ok", success=True)))
    store = create_store(root)
    write_session(store, StoredSession(id="brave-otter", nodes=nodes, main_chain_id=len(nodes)))
    return store


def test_connections_are_placed_through_the_sessions_lock(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _mcp_session(root, calls=[call("c1", "mcp__gh__search", {"q": "x"})])
    lock(root, "brave-otter", 4242)
    run_log(
        root,
        4242,
        [
            *startup(0),
            *mcp_stdio(1, "gh"),
            *mcp_http(1, "remote", "https://mcp.example.test/mcp"),
            *mcp_stdio(2, "broken", outcome="cannot find binary path"),
            log_line(90, "INFO", "chisel", "still running"),
        ],
    )
    session = _one(root)
    assert session.mcp_log_state == "applied"
    assert session.agent_version == "3000.11.3"
    assert session.entrypoint == "cli"
    by_server = {c.server: c for c in session.mcp_connections}
    assert (by_server["gh"].transport, by_server["gh"].connected, by_server["gh"].endpoint) == (
        "stdio",
        True,
        None,
    )
    assert by_server["gh"].duration_ms == 250
    assert (by_server["remote"].transport, by_server["remote"].endpoint) == (
        "streamable-http",
        "https://mcp.example.test/mcp",
    )
    broken = by_server["broken"]
    assert (broken.connected, broken.failure_category, broken.failure_detail) == (
        False,
        "spawn",
        "cannot find binary path",
    )
    assert session.turns[1].tool_calls[0].transport == "stdio"


def test_an_acp_process_is_the_acp_entrypoint(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _mcp_session(root)
    lock(root, "brave-otter", 7)
    run_log(root, 7, [*startup(0, acp=True), log_line(70, "INFO", "chisel", "x")])
    assert _one(root).entrypoint == "acp"


@pytest.mark.parametrize(
    ("reason", "category"),
    [
        (
            "Streamable HTTP connection failed for 'r' at 'u': client error (Connect): tcp connect error",
            "network",
        ),
        ("HTTP status client error (401 Unauthorized)", "auth"),
        ("Auth required", "auth"),
        ("HTTP status server error (503 Service Unavailable)", "http_status"),
        ("connection timed out after 30s", "timeout"),
        ("cannot find binary path", "spawn"),
        ("MCP server is blocked by policy", "policy"),
        ("Server is not in your organization's allowlist", "policy"),
        ("something new", "unknown"),
    ],
)
def test_failure_categories(tmp_path: Path, reason: str, category: str) -> None:
    root = tmp_path / "cli"
    _mcp_session(root)
    lock(root, "brave-otter", 9)
    run_log(
        root,
        9,
        [*startup(0), *mcp_stdio(1, "s", outcome=reason), log_line(70, "INFO", "chisel", "x")],
    )
    [connection] = _one(root).mcp_connections
    assert connection.failure_category == category


def test_the_description_cache_pass_is_not_the_sessions_attempt(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _mcp_session(root)
    lock(root, "brave-otter", 9)
    run_log(
        root,
        9,
        [
            *startup(0),
            *mcp_stdio(1, "flaky"),
            *mcp_stdio(2, "flaky", outcome="tcp connect error"),
            description_failure(2, "flaky", "tcp connect error"),
            log_line(70, "INFO", "chisel", "x"),
        ],
    )
    [connection] = _one(root).mcp_connections
    assert connection.connected is True


def test_disagreeing_attempts_leave_connected_unknown(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _mcp_session(root)
    lock(root, "brave-otter", 9)
    run_log(
        root,
        9,
        [
            *startup(0),
            *mcp_stdio(1, "flaky"),
            *mcp_stdio(2, "flaky", outcome="tcp connect error"),
            log_line(70, "INFO", "chisel", "x"),
        ],
    )
    [connection] = _one(root).mcp_connections
    assert connection.connected is None


def test_connection_fields_come_from_one_attempt_not_a_newer_pending_one(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cli"
    _mcp_session(root)
    lock(root, "brave-otter", 9)
    run_log(
        root,
        9,
        [
            *startup(0),
            *mcp_http(1, "gh", "https://old", outcome="ok"),
            # A later reconnect to a different endpoint has not resolved yet;
            # its endpoint must not be lent to the attempt that actually
            # connected.
            log_line(70, "INFO", "toolbox::tools::mcp", "Connecting to MCP server 'gh'"),
            log_line(
                70,
                "INFO",
                "toolbox::tools::mcp::config",
                "Connecting to streamable HTTP MCP server 'gh' at: https://new",
                10,
            ),
        ],
    )
    [connection] = _one(root).mcp_connections
    assert connection.connected is True
    assert connection.endpoint == "https://old"


def test_no_logs_directory_is_no_log_root(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _mcp_session(root)
    lock(root, "brave-otter", 9)
    session = _one(root)
    assert (session.mcp_log_state, session.mcp_connections) == ("no_log_root", ())


def test_a_session_without_a_lock_has_no_log(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _mcp_session(root)
    run_log(root, 9, [*startup(0), *mcp_stdio(1, "gh")])
    session = _one(root)
    assert (session.mcp_log_state, session.agent_version) == ("no_log_for_session", None)


def test_an_unreadable_lock_is_discovery_incomplete_not_no_log(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _mcp_session(root)
    locks = root / "session_locks"
    locks.mkdir(parents=True)
    (locks / "brave-otter.lock").write_text("not-a-pid\n")
    run_log(root, 9, [*startup(0), *mcp_stdio(1, "gh")])
    sessions, failures = _read(root)
    assert failures != []
    session = next(s for s in sessions if s.session_id == "devin-cli:brave-otter")
    assert session.mcp_log_state == "log_discovery_incomplete"


def test_a_log_that_does_not_span_the_session_is_not_its_log(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _mcp_session(root)
    lock(root, "brave-otter", 9)
    # The same PID, an hour earlier: a reused PID, not this session's process.
    run_log(
        root,
        9,
        [
            "2025-12-31T23:00:00.000000Z  INFO chisel: old",
            "2025-12-31T23:00:05.000000Z  INFO chisel: end",
        ],
        stamp="20251231-230000",
    )
    assert _one(root).mcp_log_state == "no_log_for_session"


def test_a_log_inside_the_window_but_short_of_its_end_is_not_its_log(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(
        store,
        StoredSession(
            id="brave-otter",
            last_activity_at=T0 + 3600,
            nodes=[Node(1, None, user("x"))],
            main_chain_id=1,
        ),
    )
    lock(root, "brave-otter", 9)
    # A same-pid process that ran and ended well before the session's last
    # activity -- a reused pid, not the session's last holder.
    run_log(
        root,
        9,
        [log_line(1200, "INFO", "chisel", "start"), log_line(1210, "INFO", "chisel", "end")],
    )
    assert _one(root).mcp_log_state == "no_log_for_session"


def test_two_logs_spanning_the_boundary_leave_placement_unresolved(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _mcp_session(root)
    lock(root, "brave-otter", 9)
    # The pid is reused right at the session's last activity (T0+60): one
    # process log ends there, the next starts there, and both satisfy
    # _spans() within its slack -- which one actually held the session
    # cannot be established.
    run_log(
        root,
        9,
        [*startup(58), *mcp_stdio(58, "server-a"), log_line(60, "INFO", "chisel", "x")],
        stamp="20260101-000058",
    )
    run_log(
        root,
        9,
        [*startup(60), *mcp_stdio(60, "server-b"), log_line(62, "INFO", "chisel", "x")],
        stamp="20260101-000060",
    )
    assert _one(root).mcp_log_state == "log_discovery_incomplete"


def test_two_sessions_in_one_process_are_placement_ambiguous(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    for session_id in ("brave-otter", "calm-heron"):
        write_session(
            store, StoredSession(id=session_id, nodes=[Node(1, None, user("x"))], main_chain_id=1)
        )
        lock(root, session_id, 9)
    run_log(
        root, 9, [*startup(0, acp=True), *mcp_stdio(1, "gh"), log_line(70, "INFO", "chisel", "x")]
    )
    sessions, failures = _read(root)
    assert failures == []
    for session in sessions:
        assert session.mcp_log_state == "placement_ambiguous"
        assert session.mcp_connections == ()
        # Version and entrypoint are the process's, so they hold for both.
        assert session.agent_version == "3000.11.3"


def test_an_unreadable_peer_lock_withholds_a_confident_placement(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(
        store, StoredSession(id="brave-otter", nodes=[Node(1, None, user("x"))], main_chain_id=1)
    )
    write_session(
        store, StoredSession(id="calm-heron", nodes=[Node(1, None, user("y"))], main_chain_id=1)
    )
    lock(root, "brave-otter", 9)
    (root / "session_locks" / "calm-heron.lock").write_text("not-a-pid")
    run_log(root, 9, [*startup(0), *mcp_stdio(1, "gh"), log_line(70, "INFO", "chisel", "x")])
    sessions, _ = _read(root)
    session = next(s for s in sessions if s.session_id == "devin-cli:brave-otter")
    assert session.mcp_log_state == "log_discovery_incomplete"


def test_subagents_share_their_parents_process(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(
        store,
        StoredSession(
            id="brave-otter",
            main_chain_id=1,
            subagents=[("explorer-1", 11)],
            nodes=[
                Node(1, None, user("q")),
                Node(10, None, user("look", typed=False)),
                Node(
                    11,
                    10,
                    assistant(
                        calls=[call("c9", "mcp__gh__search", {})],
                        request_id="s1",
                        metrics=metrics(),
                    ),
                ),
                Node(12, 11, result("c9", "x", success=True)),
            ],
        ),
    )
    lock(root, "brave-otter", 9)
    run_log(root, 9, [*startup(0), *mcp_stdio(1, "gh"), log_line(70, "INFO", "chisel", "x")])
    child = _one(root, "devin-cli:brave-otter:explorer-1")
    assert child.mcp_log_state == "applied"
    assert child.turns[0].tool_calls[0].transport == "stdio"


def test_a_gzipped_log_is_read(tmp_path: Path) -> None:
    import gzip

    root = tmp_path / "cli"
    _mcp_session(root)
    lock(root, "brave-otter", 9)
    plain = run_log(
        root, 9, [*startup(0), *mcp_stdio(1, "gh"), log_line(70, "INFO", "chisel", "x")]
    )
    with gzip.open(plain.with_name(plain.name + ".gz"), "wt") as handle:
        handle.write(plain.read_text())
    plain.unlink()
    assert [c.server for c in _one(root).mcp_connections] == ["gh"]


def test_a_log_parse_failure_is_reported_from_collect(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _mcp_session(root)
    lock(root, "brave-otter", 9)
    bad = root / "logs" / "devin_20260101-000000_9.log.gz"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(b"not actually gzip")
    sessions, failures = _read(root)
    assert any("devin_20260101-000000_9.log.gz" in f for f in failures)
    session = next(s for s in sessions if s.session_id == "devin-cli:brave-otter")
    assert session.mcp_log_state == "log_discovery_incomplete"


# --- growing stores ---------------------------------------------------------------


def test_collect_file_returns_only_sessions_with_new_rows(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(store, StoredSession(id="a", nodes=[Node(1, None, user("x"))], main_chain_id=1))
    write_session(store, StoredSession(id="b", nodes=[Node(1, None, user("y"))], main_chain_id=1))
    reader = DevinCliReader(root=root)
    first, _ = reader.collect_file(store)
    assert sorted(s.session_id for s in first) == ["devin-cli:a", "devin-cli:b"]
    again, _ = reader.collect_file(store)
    assert again == []
    append_nodes(store, "b", [Node(2, 1, assistant("z", metrics=metrics()))], main_chain_id=2)
    grown, failures = reader.collect_file(Path(f"{store}-wal"))
    assert failures == []
    assert [s.session_id for s in grown] == ["devin-cli:b"]
    assert [t.text for t in grown[0].turns] == ["y", "z"]


def test_collect_file_re_emits_a_peer_whose_placement_turns_ambiguous(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(store, StoredSession(id="a", nodes=[Node(1, None, user("x"))], main_chain_id=1))
    lock(root, "a", 9)
    run_log(root, 9, [*startup(0), *mcp_stdio(1, "gh"), log_line(70, "INFO", "chisel", "x")])
    reader = DevinCliReader(root=root)
    first, _ = reader.collect_file(store)
    assert [s.session_id for s in first] == ["devin-cli:a"]
    assert first[0].mcp_log_state == "applied"

    write_session(store, StoredSession(id="b", nodes=[Node(1, None, user("y"))], main_chain_id=1))
    lock(root, "b", 9)
    grown, _ = reader.collect_file(store)
    by_id = {s.session_id: s for s in grown}
    assert set(by_id) == {"devin-cli:a", "devin-cli:b"}
    assert by_id["devin-cli:a"].mcp_log_state == "placement_ambiguous"
    assert by_id["devin-cli:b"].mcp_log_state == "placement_ambiguous"


def test_collect_file_re_emits_peers_when_a_changed_lock_is_unreadable(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(store, StoredSession(id="a", nodes=[Node(1, None, user("x"))], main_chain_id=1))
    lock(root, "a", 9)
    run_log(root, 9, [*startup(0), *mcp_stdio(1, "gh"), log_line(70, "INFO", "chisel", "x")])
    reader = DevinCliReader(root=root)
    first, _ = reader.collect_file(store)
    assert [s.session_id for s in first] == ["devin-cli:a"]
    assert first[0].mcp_log_state == "applied"

    write_session(store, StoredSession(id="b", nodes=[Node(1, None, user("y"))], main_chain_id=1))
    (root / "session_locks" / "b.lock").write_text("not-a-pid")
    grown, _ = reader.collect_file(store)
    by_id = {s.session_id: s for s in grown}
    assert set(by_id) == {"devin-cli:a", "devin-cli:b"}
    assert by_id["devin-cli:a"].mcp_log_state == "log_discovery_incomplete"


def test_collect_file_re_emits_a_peer_freed_from_a_stale_shared_pid(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(store, StoredSession(id="a", nodes=[Node(1, None, user("x"))], main_chain_id=1))
    write_session(store, StoredSession(id="b", nodes=[Node(1, None, user("y"))], main_chain_id=1))
    lock(root, "a", 9)
    lock(root, "b", 9)
    run_log(root, 9, [*startup(0), *mcp_stdio(1, "gh"), log_line(70, "INFO", "chisel", "x")])
    reader = DevinCliReader(root=root)
    first, _ = reader.collect_file(store)
    by_id = {s.session_id: s for s in first}
    assert by_id["devin-cli:a"].mcp_log_state == "placement_ambiguous"
    assert by_id["devin-cli:b"].mcp_log_state == "placement_ambiguous"

    # "a" resumes under a new process; it no longer shares pid 9 with "b".
    append_nodes(store, "a", [Node(2, 1, assistant("more", metrics=metrics()))], main_chain_id=2)
    lock(root, "a", 10)
    run_log(root, 10, [*startup(0), *mcp_stdio(1, "gh"), log_line(70, "INFO", "chisel", "x")])
    grown, _ = reader.collect_file(store)
    by_id = {s.session_id: s for s in grown}
    assert set(by_id) == {"devin-cli:a", "devin-cli:b"}
    assert by_id["devin-cli:b"].mcp_log_state == "applied"


def test_a_log_event_re_emits_a_peer_freed_from_an_old_pid(tmp_path: Path) -> None:
    """The lock moves to a new process and only that process's log is reported:
    no store row changes, yet the peer left behind on the old pid is no longer
    ambiguous."""
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(store, StoredSession(id="a", nodes=[Node(1, None, user("x"))], main_chain_id=1))
    write_session(store, StoredSession(id="b", nodes=[Node(1, None, user("y"))], main_chain_id=1))
    lock(root, "a", 9)
    lock(root, "b", 9)
    run_log(root, 9, [*startup(0), *mcp_stdio(1, "gh"), log_line(70, "INFO", "chisel", "x")])
    reader = DevinCliReader(root=root)
    reader.collect_file(store)

    lock(root, "a", 10)
    log = run_log(root, 10, [*startup(0), *mcp_stdio(1, "gh"), log_line(70, "INFO", "chisel", "x")])
    grown, _ = reader.collect_file(log)
    by_id = {s.session_id: s for s in grown}
    assert by_id["devin-cli:b"].mcp_log_state == "applied"
    assert by_id["devin-cli:a"].mcp_log_state == "applied"


def test_an_incremental_consumer_always_holds_what_a_cold_read_would(tmp_path: Path) -> None:
    """The one rule every re-emission case is an instance of: after any event,
    whichever file it names, a consumer that replaces the sessions
    `collect_file` returned holds exactly what a cold read returns."""
    root = tmp_path / "cli"
    store = create_store(root)
    reader = DevinCliReader(root=root)
    held: dict[str, Session] = {}

    def event(path: Path) -> None:
        emitted, _ = reader.collect_file(path)
        held.update({s.session_id: s for s in emitted})
        cold, _ = DevinCliReader(root=root).collect(ALL)
        assert held == {s.session_id: s for s in cold}

    log_9 = [*startup(0), *mcp_stdio(1, "gh"), log_line(70, "INFO", "chisel", "x")]
    write_session(store, StoredSession(id="a", nodes=[Node(1, None, user("x"))], main_chain_id=1))
    lock(root, "a", 9)
    event(run_log(root, 9, log_9))
    # A second session joins the same process: both become ambiguous.
    write_session(store, StoredSession(id="b", nodes=[Node(1, None, user("y"))], main_chain_id=1))
    lock(root, "b", 9)
    event(store)
    # "a" moves to a new process, announced only by that process's log.
    lock(root, "a", 10)
    event(run_log(root, 10, log_9, stamp="20260101-000001"))
    # "b"'s lock is caught mid-write, then names a process with no log yet.
    (root / "session_locks" / "b.lock").write_text("")
    event(root / "session_locks" / "b.lock")
    lock(root, "b", 11)
    event(root / "session_locks" / "b.lock")
    # Ordinary growth, through the WAL.
    append_nodes(store, "a", [Node(2, 1, assistant("z", metrics=metrics()))], main_chain_id=2)
    event(Path(f"{store}-wal"))
    # The missing log arrives.
    event(run_log(root, 11, log_9, stamp="20260101-000002"))
    assert held["devin-cli:b"].mcp_log_state == "applied"


def test_collect_file_ignores_other_paths(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    create_store(root)
    assert DevinCliReader(root=root).collect_file(tmp_path / "elsewhere.db") == ([], [])


def test_a_replaced_store_is_read_cold(tmp_path: Path) -> None:
    """A replacement store reuses row ids for different content, which a
    row-id cursor would skip; the new content is what is emitted."""
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(store, StoredSession(id="a", nodes=[Node(1, None, user("x"))], main_chain_id=1))
    reader = DevinCliReader(root=root)
    reader.collect_file(store)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{store}{suffix}").unlink(missing_ok=True)
    store = create_store(root)
    write_session(store, StoredSession(id="a", nodes=[Node(1, None, user("new"))], main_chain_id=1))
    again, _ = reader.collect_file(store)
    assert [(s.session_id, s.turns[0].text) for s in again] == [("devin-cli:a", "new")]


# --- wiring -------------------------------------------------------------------------


def test_the_default_readers_include_devin(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    _simple(root, [Node(1, None, user("hi"))], main=1)
    collection = collect(
        parse_kind_filter(["devin-cli"]),
        ALL,
        default_readers(root=tmp_path / "no-claude", devin_root=root),
    )
    assert [s.session_id for s in collection.sessions] == ["devin-cli:brave-otter"]


def test_the_default_root_follows_xdg_data_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from openaidr.readers import devin_cli

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert devin_cli.default_root() == tmp_path / "devin" / "cli"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert (
        devin_cli.default_root()
        == Path(os.path.expanduser("~")) / ".local" / "share" / "devin" / "cli"
    )


def test_a_hidden_helper_in_the_same_process_does_not_make_placement_ambiguous(
    tmp_path: Path,
) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(
        store,
        StoredSession(id="brave-otter", nodes=[Node(1, None, user("x"))], main_chain_id=1),
    )
    write_session(
        store,
        StoredSession(id="summarizer", hidden=1, nodes=[Node(1, None, user("y"))], main_chain_id=1),
    )
    lock(root, "brave-otter", 9)
    lock(root, "summarizer", 9)
    run_log(root, 9, [*startup(0), *mcp_stdio(1, "gh"), log_line(70, "INFO", "chisel", "x")])
    assert _one(root).mcp_log_state == "applied"


def test_a_subagent_head_on_the_main_tree_claims_only_its_own_branch(tmp_path: Path) -> None:
    root = tmp_path / "cli"
    store = create_store(root)
    write_session(
        store,
        StoredSession(
            id="brave-otter",
            main_chain_id=2,
            subagents=[("odd-1", 3)],
            nodes=[
                Node(1, None, user("q")),
                Node(2, 1, assistant("main", request_id="m1", metrics=metrics())),
                Node(3, 1, assistant("side", request_id="s1", metrics=metrics())),
            ],
        ),
    )
    sessions, _ = _read(root)
    by_id = {s.session_id: s for s in sessions}
    assert [r.response_id for r in by_id["devin-cli:brave-otter"].responses] == ["m1"]
    assert [r.response_id for r in by_id["devin-cli:brave-otter:odd-1"].responses] == ["s1"]
    assert [t.text for t in by_id["devin-cli:brave-otter"].turns] == ["q", "main"]
    assert [t.text for t in by_id["devin-cli:brave-otter:odd-1"].turns] == ["side"]
