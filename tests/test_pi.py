"""The `pi` reader, against session files shaped like Pi 1.5.2's."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from openaidr.collector import collect, default_readers
from openaidr.kinds import parse_kind_filter
from openaidr.model import Session, ToolCall
from openaidr.readers.base import Window
from openaidr.readers.pi import PiReader, default_root
from tests.fixtures.pi_session import (
    CWD,
    SESSION,
    T0,
    append,
    assistant,
    header,
    iso,
    message,
    tool_call,
    tool_result,
    user,
    write,
)

ALL = Window(since=None)
ID = f"pi:{SESSION}"


def _read(root: Path, agent_dir: Path | None = None) -> tuple[list[Session], list[str]]:
    reader = PiReader(root=root, agent_dir=agent_dir or root / "no-agent-dir")
    sessions, failures = reader.collect(ALL)
    return sessions, [f.message for f in failures]


def _one(root: Path, agent_dir: Path | None = None) -> Session:
    sessions, failures = _read(root, agent_dir)
    assert failures == []
    (session,) = sessions
    return session


def _calls(session: Session) -> list[ToolCall]:
    return [call for turn in session.turns for call in turn.tool_calls]


def _basic() -> list[dict[str, object]]:
    return [
        header(),
        user("u1", None, 1, "Fix the failing checkout test"),
        assistant(
            "a1",
            "u1",
            2,
            "Looking.",
            tool_call("c1", "bash", command="npm test"),
            tool_call("c2", "read", path="src/cart.ts"),
            responseId="msg_01",
        ),
        tool_result(
            "r1",
            "a1",
            3,
            "c1",
            "bash",
            "1 failing\n\nCommand exited with code 1",
            error=True,
            durationMs=900,
        ),
        tool_result("r2", "r1", 4, "c2", "read", "export const cart = 1", durationMs=3),
        assistant(
            "a2",
            "r2",
            5,
            tool_call("c3", "edit", path="src/cart.ts", edits=[{"oldText": "1", "newText": "2"}]),
            tool_call("c4", "write", path=".env", content="KEY=1"),
        ),
        tool_result("r3", "a2", 6, "c3", "edit", "Edited src/cart.ts"),
        {
            "type": "session_info",
            "id": "n1",
            "parentId": "r3",
            "timestamp": iso(7),
            "name": "Checkout fix",
        },
    ]


def test_a_session_reads_with_its_identity_and_turns(tmp_path: Path) -> None:
    write(tmp_path, _basic())
    session = _one(tmp_path)
    assert session.session_id == ID
    assert (session.agent_kind, session.source) == ("pi", "pi")
    assert session.working_directory == CWD
    assert session.started_at == T0
    assert session.title == "Checkout fix"
    assert session.initial_prompt == "Fix the failing checkout test"
    assert [(t.key, t.role) for t in session.turns] == [
        ("u1", "user"),
        ("a1", "assistant"),
        ("a2", "assistant"),
    ]
    assert session.model == "claude-sonnet-4-5"
    assert session.compactions_recorded and not session.subagents_recorded


def test_calls_take_their_outcome_from_the_result(tmp_path: Path) -> None:
    write(tmp_path, _basic())
    bash, read, edit, written = _calls(_one(tmp_path))
    assert bash.span == f"{ID}:a1:0"
    assert (bash.status, bash.exit_code, bash.duration_ms) == ("error", 1, 900)
    assert bash.tool_class == "shell" and bash.canonical_arguments == {"command": "npm test"}
    assert bash.working_directory == CWD
    assert (read.span, read.status, read.tool_class) == (f"{ID}:a1:1", "ok", "file_read")
    assert read.canonical_arguments == {"path": "src/cart.ts"}
    assert edit.tool_class == "file_edit"
    assert edit.canonical_arguments == {
        "path": "src/cart.ts",
        "new_text": "2",
        "edits": [{"path": "src/cart.ts", "kind": "update", "old_text": "1", "new_text": "2"}],
    }
    # No result yet: the call is still running, or was never answered.
    assert (written.status, written.tool_class) == ("pending", "file_write")
    assert written.canonical_arguments == {"path": ".env", "content": "KEY=1"}


def test_a_shell_success_exits_zero(tmp_path: Path) -> None:
    write(
        tmp_path,
        [
            header(),
            assistant("a1", None, 1, tool_call("c1", "bash", command="ls")),
            tool_result("r1", "a1", 2, "c1", "bash", "README.md"),
        ],
    )
    (call,) = _calls(_one(tmp_path))
    assert (call.status, call.exit_code) == ("ok", 0)


@pytest.mark.parametrize(
    "text,status,denial",
    [
        ("Tool execution was blocked", "rejected", "permission-rule"),
        ("Operation aborted", "interrupted", None),
        ("partial output\n\nCommand aborted", "interrupted", None),
        ("Tool nope not found", "error", None),
        # An extension's own reason is free text: nothing says it was a block.
        ("Blocked by my-guard: rm is not allowed", "error", None),
    ],
)
def test_only_pis_own_words_make_a_failure_a_refusal_or_an_interruption(
    tmp_path: Path, text: str, status: str, denial: str | None
) -> None:
    write(
        tmp_path,
        [
            header(),
            assistant("a1", None, 1, tool_call("c1", "bash", command="rm -rf build")),
            tool_result("r1", "a1", 2, "c1", "bash", text, error=True),
        ],
    )
    (call,) = _calls(_one(tmp_path))
    assert (call.status, call.denial_kind, call.exit_code) == (status, denial, None)


def test_a_codemode_scripts_calls_are_their_own_turn(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"dev-radius": {"url": "https://radius.example/mcp"}}})
    )
    nested = {
        "calls": [
            {
                "id": "n1",
                "name": "mcp__dev_radius__search",
                "arguments": {"q": "cart"},
                "status": "ok",
                "durationMs": 40,
            },
            {
                "id": "n2",
                "name": "bash",
                "argumentsBytes": 9000,
                "status": "error",
                "error": "boom",
            },
            {"id": "n3", "name": "read", "arguments": {"path": "a"}, "status": "unfinished"},
        ],
        "complete": False,
    }
    write(
        tmp_path / "sessions",
        [
            header(),
            assistant("a1", None, 1, tool_call("c1", "codemode", code="await tools.read(...)")),
            tool_result("r1", "a1", 2, "c1", "codemode", "done", nestedCalls=nested),
        ],
    )
    session = _one(tmp_path / "sessions", agent_dir)
    script, search, shell, read = _calls(session)
    assert (script.tool_class, script.status, script.span) == ("script", "ok", f"{ID}:a1:0")
    assert [t.key for t in session.turns] == ["a1", "r1"]
    assert (search.span, search.mcp_server, search.tool_name) == (
        f"{ID}:r1:0",
        "dev-radius",
        "search",
    )
    assert (search.tool_class, search.status, search.duration_ms) == ("mcp", "ok", 40)
    assert (shell.status, shell.error_text, shell.truncated) == ("error", "boom", True)
    assert shell.arguments == {}
    assert read.status == "unknown"


def test_an_mcp_call_with_no_configuration_keeps_its_written_server(tmp_path: Path) -> None:
    write(
        tmp_path,
        [header(), assistant("a1", None, 1, tool_call("c1", "mcp__github__get_issue", id=7))],
    )
    (call,) = _calls(_one(tmp_path))
    assert (call.mcp_server, call.tool_name, call.tool_class) == ("github", "get_issue", "mcp")
    assert call.canonical_arguments == {}


def test_two_configured_names_that_both_fit_leave_the_call_unsplit(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "mcp.json").write_text(
        json.dumps({"mcpServers": {"a": {"command": "x"}, "a__b": {"command": "y"}}})
    )
    write(
        tmp_path / "sessions",
        [header(), assistant("a1", None, 1, tool_call("c1", "mcp__a__b__c"))],
    )
    (call,) = _calls(_one(tmp_path / "sessions", agent_dir))
    assert (call.mcp_server, call.tool_name, call.tool_class) == (None, "mcp__a__b__c", None)


def test_a_project_server_name_is_read_from_the_projects_own_file(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / ".pi").mkdir(parents=True)
    (project / ".pi" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"my.db": {"command": "x"}}})
    )
    write(
        tmp_path / "sessions",
        [header(cwd=str(project)), assistant("a1", None, 1, tool_call("c1", "mcp__my_db__query"))],
        cwd=str(project),
    )
    (call,) = _calls(_one(tmp_path / "sessions"))
    assert (call.mcp_server, call.tool_name) == ("my.db", "query")


def test_an_unreadable_configuration_is_reported_and_names_stay_written(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "mcp.json").write_text("{not json")
    write(tmp_path / "s", [header(), assistant("a1", None, 1, tool_call("c1", "mcp__gh__x"))])
    sessions, failures = _read(tmp_path / "s", agent_dir)
    assert _calls(sessions[0])[0].mcp_server == "gh"
    assert len(failures) == 1 and "mcp.json: not read" in failures[0]


def test_usage_is_one_response_per_assistant_message(tmp_path: Path) -> None:
    usage = {
        "input": 100,
        "output": 50,
        "reasoning": 20,
        "cacheRead": 900,
        "cacheWrite": 300,
        "cacheWrite1h": 100,
        "totalTokens": 1350,
        "cost": {"total": 0},
    }
    write(
        tmp_path,
        [
            header(),
            assistant(
                "a1",
                None,
                1,
                "hi",
                usage=usage,
                responseId="msg_9",
                responseModel="claude-sonnet-4-5-20251001",
                stop="stop",
            ),
            {
                "type": "usage",
                "id": "w1",
                "parentId": "a1",
                "timestamp": iso(2),
                "kind": "cache_warm",
                "provider": "anthropic",
                "model": "claude-sonnet-4-5",
                "usage": {
                    "input": 0,
                    "output": 0,
                    "cacheRead": 5000,
                    "cacheWrite": 0,
                    "totalTokens": 5000,
                },
            },
            {
                "type": "compaction",
                "id": "k1",
                "parentId": "w1",
                "timestamp": iso(3),
                "summary": "s",
                "firstKeptEntryId": "a1",
                "tokensBefore": 150000,
                "usage": {
                    "input": 9000,
                    "output": 400,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "totalTokens": 9400,
                },
            },
        ],
    )
    session = _one(tmp_path)
    first, warm, summary = session.responses
    assert (first.response_id, first.model) == ("msg_9", "claude-sonnet-4-5-20251001")
    assert session.turns[0].response_id == "msg_9"
    assert first.usage.input_tokens == 100 and first.usage.output_tokens == 50
    assert first.usage.reasoning_tokens == 20
    assert first.usage.cache_creation_input_tokens == 300
    assert (
        first.usage.cache_creation_5m_input_tokens,
        first.usage.cache_creation_1h_input_tokens,
    ) == (200, 100)
    assert (warm.response_id, warm.usage.cache_read_input_tokens) == ("w1", 5000)
    assert (summary.response_id, summary.model) == ("k1", None)
    (compaction,) = session.compactions
    assert (compaction.trigger, compaction.pre_tokens) == ("unknown", 150000)


def test_a_provider_error_is_an_error_turn(tmp_path: Path) -> None:
    write(
        tmp_path,
        [
            header(),
            user("u1", None, 1, "go"),
            assistant("a1", "u1", 2, stop="error", errorMessage="429 rate_limit_error: slow down"),
        ],
    )
    session = _one(tmp_path)
    assert [(t.key, t.role) for t in session.turns][-1] == ("a1.error", "error")
    assert session.turns[-1].text == "429 rate_limit_error: slow down"


def test_material_from_elsewhere_is_a_context_item(tmp_path: Path) -> None:
    write(
        tmp_path,
        [
            header(),
            message(
                "s1",
                None,
                0,
                {
                    "role": "system",
                    "content": "",
                    "sections": {
                        "preamble": "You are Pi.",
                        "skills": "<skills>pdf</skills>",
                        "mcp_servers": "<mcp_servers>docs: Search the docs</mcp_servers>",
                    },
                },
            ),
            {
                "type": "custom_message",
                "id": "x1",
                "parentId": "s1",
                "timestamp": iso(1),
                "customType": "my-ext",
                "content": "Remember to deploy",
                "display": True,
            },
            message(
                "b1",
                "x1",
                2,
                {
                    "role": "bashExecution",
                    "command": "cat .env",
                    "output": "K=1",
                    "exitCode": 0,
                    "cancelled": False,
                    "truncated": False,
                },
            ),
        ],
    )
    session = _one(tmp_path)
    assert [(c.source, c.name) for c in session.context_items] == [
        ("skill_listing", "skills"),
        ("mcp_instructions", "mcp_servers"),
        ("injected", "my-ext"),
    ]
    assert len({c.span for c in session.context_items}) == 3
    (turn,) = session.turns
    assert (turn.role, turn.text) == ("user", "cat .env\nK=1")


def test_an_entry_from_the_first_file_version_is_keyed_by_its_line(tmp_path: Path) -> None:
    write(
        tmp_path,
        [
            {"type": "session", "id": SESSION, "timestamp": iso(0), "cwd": CWD},
            user(None, None, 1, "hello"),
        ],
    )
    (turn,) = _one(tmp_path).turns
    assert turn.key == "L2"


def test_a_growing_file_keeps_every_span_and_reads_only_complete_lines(tmp_path: Path) -> None:
    path = write(tmp_path, _basic()[:4])
    reader = PiReader(root=tmp_path, agent_dir=tmp_path / "none")
    (before,), failures = reader.collect_file(path)
    assert failures == []
    append(path, *_basic()[4:])
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "message", "id": "half')
    (after,), _ = reader.collect_file(path)
    first = {c.span: c for c in _calls(before)}
    assert {c.span: c for c in _calls(after)}.keys() >= first.keys()
    assert len(_calls(after)) == 4
    # Nothing changed: nothing returned.
    assert reader.collect_file(path) == ([], [])


def test_a_rewritten_file_is_read_again_from_the_start(tmp_path: Path) -> None:
    path = write(tmp_path, _basic())
    reader = PiReader(root=tmp_path, agent_dir=tmp_path / "none")
    reader.collect_file(path)
    path.write_text(
        json.dumps(header(version=4)) + "\n" + json.dumps(user("u9", None, 1, "new")) + "\n"
    )
    (session,), _ = reader.collect_file(path)
    assert [t.key for t in session.turns] == ["u9"]


def test_a_broken_line_is_reported_and_the_rest_is_read(tmp_path: Path) -> None:
    path = write(tmp_path, _basic())
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{oops\n")
    sessions, failures = _read(tmp_path)
    assert len(sessions) == 1 and len(failures) == 1
    assert "line 9 is not JSON" in failures[0]


def test_a_file_with_no_header_is_reported(tmp_path: Path) -> None:
    write(tmp_path, [user("u1", None, 1, "hi")])
    sessions, failures = _read(tmp_path)
    assert sessions == [] and "no session header" in failures[0]


def test_the_window_skips_a_file_untouched_since(tmp_path: Path) -> None:
    path = write(tmp_path, _basic())
    old = T0.timestamp()
    os.utime(path, (old, old))
    reader = PiReader(root=tmp_path, agent_dir=tmp_path / "none")
    assert reader.collect(Window(since=T0.replace(year=2027)))[0] == []
    assert len(reader.collect(ALL)[0]) == 1


def test_no_session_directory_is_no_sessions_and_no_failure(tmp_path: Path) -> None:
    assert _read(tmp_path / "missing") == ([], [])


def test_the_default_collection_reads_pi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(tmp_path / "pi"))
    assert default_root() == tmp_path / "pi"
    write(tmp_path / "pi", _basic())
    readers = default_readers(
        root=tmp_path / "c",
        devin_root=tmp_path / "d",
        codex_root=tmp_path / "x",
        cursor_root=tmp_path / "cu",
        opencode_root=tmp_path / "oc",
    )
    collection = collect(parse_kind_filter(["pi"]), ALL, readers)
    assert [s.session_id for s in collection.sessions] == [ID]
