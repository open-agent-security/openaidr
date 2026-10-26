"""The Cursor reader against the desktop app's state database and structured log.

Every shape here is one Cursor 3.27.12 wrote when its agent panel ran against a
mock model (`docs/specs/cursor.md#the-desktop-app`, ADR-0039).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from openaidr.accounts import signed_in
from openaidr.collector import default_readers
from openaidr.model import Session, ToolCall
from openaidr.readers.base import Window
from openaidr.readers.cursor import CursorReader, default_user_data_root
from tests.fixtures import cursor_store as cs
from tests.fixtures.cursor_store import T0, Conversation

SHELL_OK = {"success": {"command": "echo hello", "stdout": "hello\n", "executionTime": 105}}


@pytest.fixture
def user_data(tmp_path: Path) -> Path:
    return tmp_path / "config" / "Cursor"


def _reader(tmp_path: Path, user_data: Path) -> CursorReader:
    return CursorReader(
        root=tmp_path / "home" / ".cursor", log_root=tmp_path / "logs", user_data_root=user_data
    )


def _read(tmp_path: Path, user_data: Path) -> dict[str, Session]:
    sessions, failures = _reader(tmp_path, user_data).collect(Window(since=None))
    assert failures == []
    return {s.session_id: s for s in sessions}


def _calls(session: Session) -> list[ToolCall]:
    return [call for turn in session.turns for call in turn.tool_calls]


# --- the conversation ----------------------------------------------------------


def test_a_desktop_conversation_is_a_session_read_with_the_clis_decoder(
    tmp_path: Path, user_data: Path
) -> None:
    c = Conversation("45f04b0a-5ce7", title="Fix the build", cwd="/work/app")
    c.user("run the tests")
    c.call("Shell", {"command": "echo hello"}, SHELL_OK, result="Exit code: 0")
    c.call(
        "Write",
        {"path": "/work/app/notes.txt", "contents": "hello\n"},
        {"success": {"path": "/work/app/notes.txt", "linesAdded": 1}},
    )
    message = c.say("All done.")
    c.write_desktop(user_data)

    session = _read(tmp_path, user_data)["cursor:45f04b0a-5ce7"]

    assert session.agent_kind == "cursor"
    assert session.entrypoint == "ide"
    assert session.title == "Fix the build"
    assert session.working_directory == "/work/app"
    assert session.started_at == T0
    assert session.model == "default"
    assert session.initial_prompt == "run the tests"
    assert session.turns[-1].key == message
    shell, write = _calls(session)
    assert (shell.status, shell.exit_code, shell.tool_class) == ("ok", 0, "shell")
    assert write.canonical_arguments == {"path": "/work/app/notes.txt", "content": "hello\n"}
    assert session.mcp_log_state == "not_attempted"


def test_an_mcp_call_names_the_server_its_configuration_names(
    tmp_path: Path, user_data: Path
) -> None:
    # The panel calls a server from `~/.cursor/mcp.json` by an identifier of its
    # own, `user-<name>`, and records the pair under the workspace's project.
    c = Conversation("c1", cwd="/work/app")
    c.user("list the directories")
    for server in ("user-filesystem", "user-retired"):
        c.call(
            "CallMcpTool",
            {"server": server, "toolName": "list_allowed_directories", "arguments": {}},
            {"success": {"content": [{"text": {"text": "/work/app"}}]}},
            result="/work/app",
            case=15,
        )
    c.write_desktop(user_data)
    data_root = tmp_path / "home" / ".cursor"
    cs.write_server_metadata(data_root, "/work/app", "user-filesystem", "filesystem")

    found, retired = _calls(_read(tmp_path, user_data)["cursor:c1"])

    assert (found.mcp_server, found.tool_name, found.tool_class) == (
        "filesystem",
        "list_allowed_directories",
        "mcp",
    )
    # A server with no record keeps the identifier Cursor resolved.
    assert retired.mcp_server == "user-retired"


def test_an_unreadable_server_record_keeps_the_identifier_and_is_reported(
    tmp_path: Path, user_data: Path
) -> None:
    c = Conversation("c1", cwd="/work/app")
    c.user("list the directories")
    c.call(
        "CallMcpTool",
        {"server": "user-filesystem", "toolName": "list_allowed_directories", "arguments": {}},
        {"success": {"content": [{"text": {"text": "/work/app"}}]}},
        result="/work/app",
        case=15,
    )
    c.write_desktop(user_data)
    record = cs.write_server_metadata(
        tmp_path / "home" / ".cursor", "/work/app", "user-filesystem", "filesystem"
    )
    record.write_text("{not json", encoding="utf-8")

    sessions, failures = _reader(tmp_path, user_data).collect(Window(since=None))

    [call] = _calls({s.session_id: s for s in sessions}["cursor:c1"])
    assert call.mcp_server == "user-filesystem"
    assert [f.message for f in failures] == [
        (
            f"{record}: Expecting property name enclosed in double quotes: line 1 column 2"
            " (char 1); its server is named by its identifier"
        )
    ]


@pytest.mark.parametrize("state", ["base64", "hex", "checkpoint"])
def test_the_root_is_read_however_cursor_keeps_it(
    tmp_path: Path, user_data: Path, state: str
) -> None:
    c = Conversation("c1")
    c.user("hello")
    c.write_desktop(user_data, state=state)

    assert [t.text for t in _read(tmp_path, user_data)["cursor:c1"].turns] == ["hello"]


def test_a_conversation_with_no_root_yet_is_no_session(tmp_path: Path, user_data: Path) -> None:
    cs.write_state_db(
        user_data,
        {"composerData:draft": json.dumps({"composerId": "draft", "conversationState": "~"})},
    )

    assert _read(tmp_path, user_data) == {}


def test_a_sub_agent_is_its_roots_child_and_runs_where_its_parent_does(
    tmp_path: Path, user_data: Path
) -> None:
    parent = Conversation("p1")
    parent.user("delegate")
    call = parent.call(
        "Task", {"description": "Count", "prompt": "count", "subagent_type": "x"}, None
    )
    parent.write_desktop(user_data)
    child = Conversation(
        "k1",
        subagent={
            "rootParentConversationId": "p1",
            "parentComposerId": "p1",
            "toolCallId": call,
        },
    )
    child.user("count")
    child.write_desktop(user_data, extra={"workspaceIdentifier": None})

    session = _read(tmp_path, user_data)["cursor:p1:k1"]

    assert all(turn.is_sidechain for turn in session.turns)
    assert session.working_directory == "/work/app"


def test_a_conversation_a_program_started_has_no_entrypoint(
    tmp_path: Path, user_data: Path
) -> None:
    c = Conversation("auto1")
    c.user("nightly")
    c.write_desktop(user_data, extra={"createdFromLocalScheduledTask": {"taskId": "t1"}})

    assert _read(tmp_path, user_data)["cursor:auto1"].entrypoint is None


def test_hex_text_blobs_are_read_as_cursor_reads_them(tmp_path: Path, user_data: Path) -> None:
    c = Conversation("c1")
    c.user("hello")
    path = c.write_desktop(user_data)
    connection = sqlite3.connect(path)
    connection.execute("UPDATE cursorDiskKV SET value = hex(value) WHERE key LIKE 'agentKv:blob:%'")
    connection.commit()
    connection.close()

    assert [t.text for t in _read(tmp_path, user_data)["cursor:c1"].turns] == ["hello"]


# --- failures ------------------------------------------------------------------


def test_no_database_is_no_sessions_and_no_failure(tmp_path: Path, user_data: Path) -> None:
    assert _read(tmp_path, user_data) == {}


def test_a_database_without_the_table_is_an_unsupported_schema(
    tmp_path: Path, user_data: Path
) -> None:
    directory = user_data / "User" / "globalStorage"
    directory.mkdir(parents=True)
    sqlite3.connect(directory / "state.vscdb").close()

    sessions, failures = _reader(tmp_path, user_data).collect(Window(since=None))

    assert sessions == []
    assert ["unsupported schema" in f.message for f in failures] == [True]


def test_a_malformed_conversation_is_reported_and_the_others_are_read(
    tmp_path: Path, user_data: Path
) -> None:
    c = Conversation("good")
    c.user("hello")
    c.write_desktop(user_data)
    cs.write_state_db(user_data, {"composerData:bad": "{not json"})

    sessions, failures = _reader(tmp_path, user_data).collect(Window(since=None))

    assert [s.session_id for s in sessions] == ["cursor:good"]
    assert [("conversation bad" in f.message) for f in failures] == [True]


def test_a_malformed_conversation_is_still_reported_when_its_row_does_not_change(
    tmp_path: Path, user_data: Path
) -> None:
    """A row's digest caches the conversation it decoded to, so a later pass
    over an unchanged row must still raise the warning a failed parse raised,
    not silently drop the conversation it still omits (ADR-0039)."""
    c = Conversation("good")
    c.user("hello")
    c.write_desktop(user_data)
    cs.write_state_db(user_data, {"composerData:bad": "{not json"})
    reader = _reader(tmp_path, user_data)

    first_sessions, first_failures = reader.collect(Window(since=None))
    second_sessions, second_failures = reader.collect(Window(since=None))

    assert [s.session_id for s in first_sessions] == ["cursor:good"]
    assert [s.session_id for s in second_sessions] == ["cursor:good"]
    assert [("conversation bad" in f.message) for f in first_failures] == [True]
    assert [("conversation bad" in f.message) for f in second_failures] == [True]


# --- usage and errors from the structured log -----------------------------------


def test_a_usage_line_in_a_turn_is_its_conversations_response(
    tmp_path: Path, user_data: Path
) -> None:
    c = Conversation("c1")
    c.user("go")
    c.say("done")
    c.write_desktop(user_data)
    cs.write_ide_log(
        user_data,
        [
            cs.ide_turn_start(T0, "c1", "r1"),
            cs.ide_usage(
                T0 + timedelta(seconds=1), input_tokens=1200, output_tokens=40, cache_read=600
            ),
            cs.ide_turn_outcome(T0 + timedelta(seconds=2), "c1", "r1"),
        ],
    )

    session = _read(tmp_path, user_data)["cursor:c1"]

    [response] = session.responses
    assert response.response_id == "r1:1"
    assert response.occurred_at == T0 + timedelta(seconds=1)
    assert response.usage.input_tokens == 600
    assert response.usage.cache_read_input_tokens == 600
    assert response.usage.output_tokens == 40
    assert session.turns[-1].response_id == "r1:1"


def test_a_usage_line_while_two_turns_are_open_belongs_to_neither(
    tmp_path: Path, user_data: Path
) -> None:
    for name in ("c1", "c2"):
        c = Conversation(name)
        c.user("go")
        c.write_desktop(user_data)
    cs.write_ide_log(
        user_data,
        [
            cs.ide_turn_start(T0, "c1", "r1"),
            cs.ide_turn_start(T0, "c2", "r2"),
            cs.ide_usage(T0 + timedelta(seconds=1), input_tokens=10, output_tokens=1),
            cs.ide_turn_outcome(T0 + timedelta(seconds=2), "c2", "r2"),
            cs.ide_usage(T0 + timedelta(seconds=3), input_tokens=20, output_tokens=2),
            cs.ide_turn_outcome(T0 + timedelta(seconds=4), "c1", "r1"),
        ],
    )

    sessions = _read(tmp_path, user_data)

    assert [r.usage.input_tokens for r in sessions["cursor:c1"].responses] == [20]
    assert sessions["cursor:c2"].responses == ()


def test_a_usage_line_in_a_sub_agents_span_is_the_childs(tmp_path: Path, user_data: Path) -> None:
    parent = Conversation("p1")
    parent.user("delegate")
    parent.write_desktop(user_data)
    child = Conversation(
        "k1",
        subagent={
            "rootParentConversationId": "p1",
            "parentComposerId": "p1",
            "toolCallId": "call_1",
        },
    )
    child.user("count")
    child.write_desktop(user_data)
    cs.write_ide_log(
        user_data,
        [
            cs.ide_turn_start(T0, "p1", "r1"),
            cs.ide_delegate(T0 + timedelta(seconds=1), "call_1"),
            cs.ide_usage(T0 + timedelta(seconds=2), input_tokens=10, output_tokens=1),
            cs.ide_settled(T0 + timedelta(seconds=3), "call_1"),
            cs.ide_usage(T0 + timedelta(seconds=4), input_tokens=20, output_tokens=2),
            cs.ide_turn_outcome(T0 + timedelta(seconds=5), "p1", "r1"),
        ],
    )

    sessions = _read(tmp_path, user_data)

    [mine] = sessions["cursor:p1"].responses
    [theirs] = sessions["cursor:p1:k1"].responses
    assert (mine.response_id, mine.usage.input_tokens) == ("r1:1", 20)
    assert (theirs.response_id, theirs.usage.input_tokens, theirs.is_sidechain) == (
        "r1:call_1:1",
        10,
        True,
    )


def test_a_usage_line_inside_two_sub_agent_spans_belongs_to_neither(
    tmp_path: Path, user_data: Path
) -> None:
    for name, call in (("k1", "call_1"), ("k2", "call_2")):
        child = Conversation(
            name,
            subagent={
                "rootParentConversationId": "p1",
                "parentComposerId": "p1",
                "toolCallId": call,
            },
        )
        child.user("count")
        child.write_desktop(user_data)
    cs.write_ide_log(
        user_data,
        [
            cs.ide_turn_start(T0, "p1", "r1"),
            cs.ide_delegate(T0, "call_1"),
            cs.ide_delegate(T0, "call_2"),
            cs.ide_usage(T0 + timedelta(seconds=1), input_tokens=10, output_tokens=1),
        ],
    )

    sessions = _read(tmp_path, user_data)

    assert sessions["cursor:p1:k1"].responses == ()
    assert sessions["cursor:p1:k2"].responses == ()


def test_a_turn_that_ended_in_an_error_is_an_error_turn(tmp_path: Path, user_data: Path) -> None:
    c = Conversation("c1")
    c.user("go")
    c.write_desktop(user_data)
    cs.write_ide_log(
        user_data,
        [
            cs.ide_turn_start(T0, "c1", "r1"),
            cs.ide_turn_outcome(T0 + timedelta(seconds=1), "c1", "r1", "Unauthorized (HTTP 401)"),
        ],
    )

    session = _read(tmp_path, user_data)["cursor:c1"]

    [error] = [t for t in session.turns if t.role == "error"]
    assert error.text == "Unauthorized (HTTP 401)"
    assert error.occurred_at == T0 + timedelta(seconds=1)


# --- growing sessions ------------------------------------------------------------


def test_collect_file_returns_only_the_conversations_that_changed(
    tmp_path: Path, user_data: Path
) -> None:
    a = Conversation("a")
    a.user("one")
    db = a.write_desktop(user_data)
    b = Conversation("b")
    b.user("two")
    b.write_desktop(user_data)
    reader = _reader(tmp_path, user_data)
    first, _ = reader.collect_file(db)
    assert sorted(s.session_id for s in first) == ["cursor:a", "cursor:b"]
    assert reader.collect_file(db) == ([], [])

    b.say("done")
    b.write_desktop(user_data)
    changed, failures = reader.collect_file(db.with_name("state.vscdb-wal"))

    assert failures == []
    assert [s.session_id for s in changed] == ["cursor:b"]


def test_a_new_log_line_returns_the_session_it_enriches(tmp_path: Path, user_data: Path) -> None:
    c = Conversation("c1")
    c.user("go")
    db = c.write_desktop(user_data)
    reader = _reader(tmp_path, user_data)
    reader.collect_file(db)

    log = cs.write_ide_log(
        user_data,
        [cs.ide_turn_start(T0, "c1", "r1"), cs.ide_usage(T0, input_tokens=5, output_tokens=1)],
    )
    changed, _ = reader.collect_file(log)

    assert [len(s.responses) for s in changed] == [1]


def test_the_default_readers_read_the_desktop_app_from_its_named_root(user_data: Path) -> None:
    c = Conversation("c1")
    c.user("hello")
    c.write_desktop(user_data)

    readers = default_readers(cursor_user_data_root=user_data)
    [cursor] = [r for r in readers if r.agent_kind == "cursor"]

    sessions, _ = cursor.collect(Window(since=None))
    assert [s.session_id for s in sessions] == ["cursor:c1"]


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({"VSCODE_PORTABLE": "/p", "VSCODE_APPDATA": "/a"}, "/p/user-data"),
        ({"VSCODE_APPDATA": "/a", "XDG_CONFIG_HOME": "/x"}, "/a/Cursor"),
        ({"XDG_CONFIG_HOME": "/x"}, "/x/Cursor"),
    ],
)
def test_the_user_data_root_is_resolved_in_cursors_order(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str], expected: str
) -> None:
    for name in ("VSCODE_PORTABLE", "VSCODE_APPDATA", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("sys.platform", "linux")

    assert default_user_data_root() == Path(expected)


# --- who is signed in ------------------------------------------------------------


def _sign_in(user_data: Path, auth_id: str | None = "auth0|user_1") -> None:
    path = cs.write_state_db(user_data, {})
    connection = sqlite3.connect(path)
    rows = {
        "cursorAuth/cachedEmail": "dev@example.com",
        "cursorAuth/stripeMembershipType": "pro",
        "cursorAuth/accessToken": "never-read",
    }
    if auth_id is not None:
        rows["cursorAuth/stripeMembershipAuthId"] = auth_id
    connection.executemany("INSERT INTO ItemTable VALUES (?, ?)", sorted(rows.items()))
    connection.commit()
    connection.close()


def _cli_config(config: Path, auth_id: str) -> None:
    config.mkdir(parents=True)
    (config / "cli-config.json").write_text(
        json.dumps({"authInfo": {"email": "dev@example.com", "userId": 1234, "authId": auth_id}}),
        encoding="utf-8",
    )


def test_the_desktop_apps_sign_in_names_the_account_and_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VSCODE_APPDATA", str(tmp_path / "appdata"))
    _sign_in(tmp_path / "appdata" / "Cursor")

    account = signed_in("cursor", config_dir=tmp_path / "no-cli")

    assert account is not None
    assert (account.account_id, account.email, account.plan) == (
        "auth0|user_1",
        "dev@example.com",
        "pro",
    )


@pytest.mark.parametrize(("desktop_auth", "plan"), [("auth0|user_1", "pro"), ("auth0|other", None)])
def test_the_clis_account_takes_the_desktop_plan_only_for_the_same_auth_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, desktop_auth: str, plan: str | None
) -> None:
    monkeypatch.setenv("VSCODE_APPDATA", str(tmp_path / "appdata"))
    _sign_in(tmp_path / "appdata" / "Cursor", desktop_auth)
    _cli_config(tmp_path / "cli", "auth0|user_1")

    account = signed_in("cursor", config_dir=tmp_path / "cli")

    assert account is not None
    assert (account.account_id, account.plan) == ("1234", plan)


def test_a_signed_out_desktop_app_names_no_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VSCODE_APPDATA", str(tmp_path / "appdata"))
    _sign_in(tmp_path / "appdata" / "Cursor", auth_id=None)

    assert signed_in("cursor", config_dir=tmp_path / "no-cli") is None
