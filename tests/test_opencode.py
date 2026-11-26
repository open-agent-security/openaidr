"""The `opencode` reader, against databases shaped like OpenCode 1.19.5's."""

from __future__ import annotations

import json
import os
import pwd
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from openaidr.collector import collect, default_readers
from openaidr.kinds import parse_kind_filter
from openaidr.model import Session, ToolCall
from openaidr.readers.base import Window
from openaidr.readers.opencode import OpenCodeReader, default_root
from tests.fixtures.opencode_db import (
    CHILD,
    DIRECTORY,
    ROOT,
    T0,
    assistant,
    create,
    part,
    session,
    step_finish,
    text,
    tool,
    user,
)

ALL = Window(since=None)
ID = f"opencode:{ROOT}"


def _reader(tmp_path: Path) -> OpenCodeReader:
    return OpenCodeReader(root=tmp_path / "data", config_root=tmp_path / "config")


def _read(tmp_path: Path) -> tuple[list[Session], list[str]]:
    sessions, failures = _reader(tmp_path).collect(ALL)
    return sessions, [f.message for f in failures]


def _one(tmp_path: Path, session_id: str = ID) -> Session:
    sessions, failures = _read(tmp_path)
    assert failures == []
    return next(s for s in sessions if s.session_id == session_id)


def _calls(found: Session) -> list[ToolCall]:
    return [call for turn in found.turns for call in turn.tool_calls]


def _db(tmp_path: Path) -> sqlite3.Connection:
    return create(tmp_path / "data" / "opencode.db")


def _basic(tmp_path: Path) -> sqlite3.Connection:
    db = _db(tmp_path)
    session(db)
    user(db, "msg_01", 1)
    part(db, "prt_01", "msg_01", 1, text("Fix the checkout test"))
    part(db, "prt_02", "msg_01", 1, text("<file>cart.ts</file>", synthetic=True))
    assistant(db, "msg_02", 2, "msg_01")
    part(db, "prt_03", "msg_02", 2, {"type": "step-start"})
    part(db, "prt_04", "msg_02", 2, text("Running the tests."))
    part(
        db,
        "prt_05",
        "msg_02",
        3,
        tool(
            "call_1",
            "bash",
            "completed",
            output="1 failing",
            metadata={"exit": 1},
            command="npm test",
            workdir="/home/dev/shop/web",
        ),
    )
    part(
        db, "prt_06", "msg_02", 3, tool("call_2", "read", "completed", output="x", filePath="/a.ts")
    )
    part(db, "prt_07", "msg_02", 4, step_finish())
    return db


def test_a_session_reads_with_its_identity(tmp_path: Path) -> None:
    _basic(tmp_path)
    found = _one(tmp_path)
    assert (found.agent_kind, found.source, found.session_id) == ("opencode", "opencode", ID)
    assert found.working_directory == DIRECTORY
    assert found.agent_version == "1.19.5"
    assert found.started_at == T0
    assert found.generated_title == "Fix the checkout bug" and found.title is None
    assert found.initial_prompt == "Fix the checkout test"
    assert [(t.key, t.role) for t in found.turns] == [("msg_01", "user"), ("msg_02", "assistant")]
    assert found.turns[1].text == "Running the tests."
    assert found.model == "claude-sonnet-4-5"
    assert found.subagents_recorded and found.compactions_recorded


def test_a_placeholder_title_is_no_title(tmp_path: Path) -> None:
    db = _db(tmp_path)
    session(db, title="New session - 2026-11-10T09:00:00.000Z")
    assert _one(tmp_path).generated_title is None


def test_calls_take_their_outcome_from_the_part(tmp_path: Path) -> None:
    _basic(tmp_path)
    bash, read = _calls(_one(tmp_path))
    assert (bash.span, bash.provider_call_id) == (f"{ID}:msg_02:0", "call_1")
    # Completed, but its exit status says it failed.
    assert (bash.status, bash.exit_code, bash.duration_ms) == ("error", 1, 250)
    assert bash.error_text == bash.result == "1 failing"
    assert bash.working_directory == "/home/dev/shop/web"
    assert (bash.tool_class, bash.canonical_arguments) == ("shell", {"command": "npm test"})
    assert (read.span, read.tool_class, read.canonical_arguments) == (
        f"{ID}:msg_02:1",
        "file_read",
        {"path": "/a.ts"},
    )
    assert read.result == "x"


def test_a_synthetic_text_is_a_context_item(tmp_path: Path) -> None:
    _basic(tmp_path)
    (item,) = _one(tmp_path).context_items
    assert (item.source, item.text) == ("injected", "<file>cart.ts</file>")


@pytest.mark.parametrize(
    "error,metadata,status,denial",
    [
        (
            "The user rejected permission to use this specific tool call.",
            None,
            "rejected",
            "user-rejected",
        ),
        (
            (
                "The user rejected permission to use this specific tool call with the following "
                "feedback: use the staging db"
            ),
            None,
            "rejected",
            "user-rejected",
        ),
        (
            (
                "The user has specified a rule which prevents you from using this specific tool "
                'call. Here are some of the relevant rules [{"permission":"bash"}]'
            ),
            None,
            "rejected",
            "permission-rule",
        ),
        ("Tool execution aborted", {"interrupted": True}, "interrupted", None),
        ("ENOENT: no such file", None, "error", None),
    ],
)
def test_only_opencodes_own_words_make_an_error_a_refusal(
    tmp_path: Path, error: str, metadata: dict[str, object] | None, status: str, denial: str | None
) -> None:
    db = _db(tmp_path)
    session(db)
    assistant(db, "msg_02", 2, "msg_01")
    part(
        db,
        "prt_05",
        "msg_02",
        3,
        tool("c", "bash", "error", error=error, metadata=metadata, command="rm -rf x"),
    )
    (call,) = _calls(_one(tmp_path))
    assert (call.status, call.denial_kind) == (status, denial)
    assert call.error_text == (error if status == "error" else None)


@pytest.mark.parametrize("state", ["pending", "running"])
def test_a_call_still_running_is_pending(tmp_path: Path, state: str) -> None:
    db = _db(tmp_path)
    session(db)
    assistant(db, "msg_02", 2, "msg_01")
    part(db, "prt_05", "msg_02", 3, tool("c", "bash", state, command="sleep 9"))
    (call,) = _calls(_one(tmp_path))
    assert (call.status, call.exit_code, call.result) == ("pending", None, None)


def test_file_tools_give_canonical_edits(tmp_path: Path) -> None:
    db = _db(tmp_path)
    session(db)
    assistant(db, "msg_02", 2, "msg_01")
    patch = "*** Begin Patch\n*** Add File: .env\n+KEY=1\n*** End Patch\n"
    part(
        db,
        "prt_1",
        "msg_02",
        2,
        tool("c1", "edit", "completed", filePath="/a", oldString="o", newString="n"),
    )
    part(
        db,
        "prt_2",
        "msg_02",
        2,
        tool("c2", "edit", "completed", filePath="/b", oldString="", newString="new"),
    )
    part(db, "prt_3", "msg_02", 2, tool("c3", "apply_patch", "completed", patchText=patch))
    part(db, "prt_4", "msg_02", 2, tool("c4", "write", "completed", filePath="/c", content="z"))
    part(
        db,
        "prt_5",
        "msg_02",
        2,
        tool("c5", "task", "completed", subagent_type="explore", prompt="p", description="d"),
    )
    part(db, "prt_6", "msg_02", 2, tool("c6", "skill", "completed", name="pdf"))
    edit, create_, patched, written, task, skill = _calls(_one(tmp_path))
    assert edit.canonical_arguments["edits"] == [
        {"path": "/a", "kind": "update", "old_text": "o", "new_text": "n"}
    ]
    assert create_.canonical_arguments["edits"][0]["kind"] == "add"  # type: ignore[index]
    assert patched.canonical_arguments == {
        "path": ".env",
        "new_text": "KEY=1",
        "edits": [{"path": ".env", "kind": "add", "old_text": "", "new_text": "KEY=1"}],
    }
    assert written.canonical_arguments == {"path": "/c", "content": "z"}
    assert (task.tool_class, task.canonical_arguments) == ("delegate", {"agent": "explore"})
    assert (skill.tool_class, skill.attributed_skill) == ("skill", "pdf")


def _config(tmp_path: Path, body: str) -> None:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "opencode.jsonc").write_text(body, encoding="utf-8")


def test_an_mcp_tool_splits_against_the_configured_servers(tmp_path: Path) -> None:
    _config(
        tmp_path,
        """{
          // the servers
          "mcp": {
            "git-hub": {"type": "remote", "url": "https://gh.example/mcp"},
            "my.db": {"type": "local", "command": ["db-mcp", "--url=http://x//y"]}, /* trailing */
          },
        }""",
    )
    db = _db(tmp_path)
    session(db)
    assistant(db, "msg_02", 2, "msg_01")
    part(db, "prt_1", "msg_02", 2, tool("c1", "git-hub_get_issue", "completed", number=7))
    part(
        db,
        "prt_2",
        "msg_02",
        2,
        tool("c2", "my_db_run_query", "error", error="MCP tool returned an error"),
    )
    part(db, "prt_3", "msg_02", 2, tool("c3", "unknown_tool", "completed"))
    issue, query, unknown = _calls(_one(tmp_path))
    assert (issue.mcp_server, issue.tool_name, issue.tool_class) == ("git-hub", "get_issue", "mcp")
    assert (query.mcp_server, query.tool_name, query.status) == ("my.db", "run_query", "error")
    assert (unknown.mcp_server, unknown.tool_name, unknown.tool_class) == (
        None,
        "unknown_tool",
        None,
    )


def test_two_servers_that_both_fit_leave_the_call_unsplit(tmp_path: Path) -> None:
    _config(tmp_path, '{"mcp": {"git": {"type": "local"}, "git_hub": {"type": "local"}}}')
    db = _db(tmp_path)
    session(db)
    assistant(db, "msg_02", 2, "msg_01")
    part(db, "prt_1", "msg_02", 2, tool("c1", "git_hub_search", "completed"))
    (call,) = _calls(_one(tmp_path))
    assert (call.mcp_server, call.tool_name) == (None, "git_hub_search")


def test_a_projects_own_configuration_and_the_newer_layout_are_read(tmp_path: Path) -> None:
    project = Path(DIRECTORY)
    db = _db(tmp_path)
    session(db)
    assistant(db, "msg_02", 2, "msg_01")
    part(db, "prt_1", "msg_02", 2, tool("c1", "docs_search", "completed"))
    reader = _reader(tmp_path)
    configs = [str(p) for p in reader._config_files(str(project))]
    assert str(project / "opencode.json") in configs
    assert str(project / ".opencode" / "opencode.jsonc") in configs
    _config(tmp_path, '{"mcp": {"servers": {"docs": {"type": "remote", "url": "u"}}}}')
    (call,) = _calls(_one(tmp_path))
    assert (call.mcp_server, call.tool_name) == ("docs", "search")


def test_a_built_in_is_never_split(tmp_path: Path) -> None:
    _config(tmp_path, '{"mcp": {"list": {"type": "local"}}}')
    db = _db(tmp_path)
    session(db)
    assistant(db, "msg_02", 2, "msg_01")
    part(db, "prt_1", "msg_02", 2, tool("c1", "list_mcp_resources", "completed"))
    (call,) = _calls(_one(tmp_path))
    assert (call.mcp_server, call.tool_class) == (None, "other")


def test_each_step_is_one_response(tmp_path: Path) -> None:
    db = _basic(tmp_path)
    part(
        db,
        "prt_08",
        "msg_02",
        5,
        step_finish(input_tokens=10, output=5, reasoning=0, read=0, write=0),
    )
    found = _one(tmp_path)
    first, second = found.responses
    assert first.response_id == "prt_07" and first.model == "claude-sonnet-4-5"
    assert (first.usage.input_tokens, first.usage.cache_read_input_tokens) == (1200, 4000)
    assert first.usage.cache_creation_input_tokens == 300
    # OpenCode's output leaves reasoning out; the model's output holds both.
    assert (first.usage.output_tokens, first.usage.reasoning_tokens) == (80, 20)
    assert second.usage.output_tokens == 5
    # Two steps in one message: the turn cannot name one response.
    assert found.turns[1].response_id is None


def test_one_step_is_the_turns_response(tmp_path: Path) -> None:
    _basic(tmp_path)
    assert _one(tmp_path).turns[1].response_id == "prt_07"


def test_a_sub_agent_is_its_own_session_under_its_root(tmp_path: Path) -> None:
    db = _basic(tmp_path)
    session(db, CHILD, parent=ROOT, title="Explore the cart (@explore subagent)")
    user(db, "msg_10", 3, session_id=CHILD)
    part(db, "prt_10", "msg_10", 3, text("Find the cart code"), session_id=CHILD)
    assistant(db, "msg_11", 4, "msg_10", session_id=CHILD)
    part(
        db,
        "prt_11",
        "msg_11",
        4,
        tool("c9", "grep", "completed", pattern="cart", path="/src"),
        session_id=CHILD,
    )
    part(db, "prt_12", "msg_11", 4, step_finish(), session_id=CHILD)
    child = _one(tmp_path, f"opencode:{ROOT}:{CHILD}")
    assert all(t.is_sidechain for t in child.turns)
    assert all(r.is_sidechain for r in child.responses)
    (grep,) = _calls(child)
    assert grep.span == f"opencode:{ROOT}:{CHILD}:msg_11:0"
    assert (grep.tool_class, grep.canonical_arguments) == ("file_search", {"path": "/src"})


def test_a_compaction_and_a_provider_error_are_recorded(tmp_path: Path) -> None:
    db = _db(tmp_path)
    session(db)
    user(db, "msg_01", 1)
    part(db, "prt_01", "msg_01", 1, {"type": "compaction", "auto": True})
    assistant(
        db,
        "msg_02",
        2,
        "msg_01",
        error={
            "name": "APIError",
            "data": {"message": "Rate limit exceeded", "statusCode": 429, "isRetryable": True},
        },
    )
    assistant(
        db,
        "msg_03",
        3,
        "msg_01",
        error={"name": "MessageAbortedError", "data": {"message": "Aborted"}},
    )
    found = _one(tmp_path)
    (compaction,) = found.compactions
    assert compaction.trigger == "auto"
    assert [(t.key, t.role) for t in found.turns] == [
        ("msg_02", "assistant"),
        ("msg_02.error", "error"),
        ("msg_03", "assistant"),
    ]
    assert found.turns[1].text == "Rate limit exceeded"


def test_a_change_returns_only_the_sessions_it_touched(tmp_path: Path) -> None:
    db = _basic(tmp_path)
    session(db, "ses_other", title="other")
    reader = _reader(tmp_path)
    path = tmp_path / "data" / "opencode.db"
    first, failures = reader.collect_file(path.with_name("opencode.db-wal"))
    assert failures == [] and {s.session_id for s in first} == {ID, "opencode:ses_other"}
    assert reader.collect_file(path) == ([], [])
    part(
        db,
        "prt_05",
        "msg_02",
        3,
        tool("call_1", "bash", "completed", output="ok", metadata={"exit": 0}, command="npm test"),
    )
    changed, _ = reader.collect_file(path)
    assert [s.session_id for s in changed] == [ID]
    assert _calls(changed[0])[0].span == f"{ID}:msg_02:0"
    assert reader.collect_file(tmp_path / "data" / "notes.txt") == ([], [])


def test_a_database_with_no_session_table_is_reported(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    sqlite3.connect(tmp_path / "data" / "opencode.db").execute("CREATE TABLE x (y)")
    sessions, failures = _read(tmp_path)
    assert sessions == [] and "unsupported schema" in failures[0]


def test_a_part_that_is_not_json_is_reported_and_skipped(tmp_path: Path) -> None:
    db = _basic(tmp_path)
    db.execute("INSERT INTO part VALUES ('prt_99', 'msg_02', ?, 0, 0, '{bad')", (ROOT,))
    db.commit()
    sessions, failures = _read(tmp_path)
    assert len(_calls(sessions[0])) == 2
    assert len(failures) == 1 and "part prt_99 is not a JSON object" in failures[0]


def test_the_window_reads_sessions_with_rows_written_since(tmp_path: Path) -> None:
    _basic(tmp_path)
    reader = _reader(tmp_path)
    assert reader.collect(Window(since=T0.replace(year=2027)))[0] == []
    assert len(reader.collect(Window(since=T0))[0]) == 1


def test_a_parts_later_update_counts_as_activity(tmp_path: Path) -> None:
    """OpenCode updates a tool part in place as its call runs, without
    touching the session's own `time_updated` (ADR-0001); a window that
    starts after the part's creation must still see that update."""
    db = _db(tmp_path)
    session(db, updated=0)
    user(db, "msg_01", 1)
    assistant(db, "msg_02", 2, "msg_01")
    part(db, "prt_01", "msg_02", 3, tool("call_1", "bash", "running", command="npm test"))
    part(db, "prt_01", "msg_02", 500, tool("call_1", "bash", "completed", output="ok"))
    reader = _reader(tmp_path)
    assert reader.collect(Window(since=T0 + timedelta(seconds=100)))[0] != []


def test_no_data_directory_is_no_sessions_and_no_failure(tmp_path: Path) -> None:
    assert _read(tmp_path) == ([], [])


def test_default_configuration_is_not_this_machines(tmp_path: Path) -> None:
    """The global config and `~/.opencode` are empty directories.

    A file in either place names MCP servers, or fails the read. The test
    fixture points both at empty directories, not this machine's own
    `~/.config/opencode` and `~/.opencode`. The empty directory may still
    sit under the login home, as a pytest base temporary directory can."""
    reader = OpenCodeReader(root=tmp_path / "data")
    login_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "opencode"

    assert reader._config == config
    assert config.resolve() != (login_home / ".config" / "opencode").resolve()
    assert list(config.glob("*")) == []
    assert (Path.home() / ".opencode").resolve() != (login_home / ".opencode").resolve()
    assert not (Path.home() / ".opencode").exists()
    assert Path.home() / ".opencode" / "opencode.json" in reader._config_files(None)


def test_the_default_collection_reads_opencode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert default_root() == tmp_path / "xdg" / "opencode"
    _basic(tmp_path)
    readers = default_readers(
        root=tmp_path / "c",
        devin_root=tmp_path / "d",
        codex_root=tmp_path / "x",
        cursor_root=tmp_path / "cu",
        pi_root=tmp_path / "pi",
        opencode_root=tmp_path / "data",
    )
    collection = collect(parse_kind_filter(["opencode"]), ALL, readers)
    assert [s.session_id for s in collection.sessions] == [ID]


def test_a_channel_database_is_read_too(tmp_path: Path) -> None:
    db = create(tmp_path / "data" / "opencode-beta.db")
    session(db, "ses_beta", title="beta")
    sessions, _ = _read(tmp_path)
    assert [s.session_id for s in sessions] == ["opencode:ses_beta"]
    assert json.dumps(sessions[0].public_descriptor())


def test_a_named_database_outside_the_root_is_watched_and_read_incrementally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    named = tmp_path / "external" / "store.db"
    monkeypatch.setenv("OPENCODE_DB", str(named))
    db = create(named)
    session(db, "ses_named", title="named")
    reader = _reader(tmp_path)
    assert reader.locations() == (tmp_path / "data", tmp_path / "external")
    changed, failures = reader.collect_file(named.with_name("store.db-wal"))
    assert failures == [] and [s.session_id for s in changed] == ["opencode:ses_named"]


def test_a_named_database_with_a_nonstandard_name_is_read_incrementally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    named = tmp_path / "data" / "store.sqlite"
    monkeypatch.setenv("OPENCODE_DB", "store.sqlite")
    db = create(named)
    session(db, "ses_named", title="named")
    reader = _reader(tmp_path)
    assert reader.locations() == (tmp_path / "data",)
    changed, failures = reader.collect_file(named)
    assert failures == [] and [s.session_id for s in changed] == ["opencode:ses_named"]
