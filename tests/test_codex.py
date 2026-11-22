"""The Codex reader (`docs/specs/codex.md`), against synthetic rollouts."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openaidr.collector import collect, default_readers
from openaidr.kinds import parse_kind_filter
from openaidr.model import Session, ToolCall
from openaidr.readers.base import Window
from openaidr.readers.codex import CodexReader
from tests.fixtures.codex_rollout import Rollout, started_line, write_logs

_ALL = Window(since=None)


def _read(home: Path) -> tuple[list[Session], list[str]]:
    sessions, failures = CodexReader(home).collect(_ALL)
    return sorted(sessions, key=lambda s: s.session_id), [f.message for f in failures]


def _only(home: Path) -> Session:
    sessions, failures = _read(home)
    assert failures == []
    assert len(sessions) == 1
    return sessions[0]


def _calls(session: Session) -> dict[str, ToolCall]:
    return {call.provider_call_id or "": call for turn in session.turns for call in turn.tool_calls}


# --- identity -------------------------------------------------------------------


def test_a_thread_is_one_session_named_by_its_id(tmp_path: Path) -> None:
    rollout = Rollout("01a1-main")
    rollout.turn("t1")
    rollout.user("fix the build")
    rollout.assistant("on it")
    rollout.write(tmp_path)

    session = _only(tmp_path)

    assert session.session_id == "codex:01a1-main"
    assert session.agent_kind == "codex"
    assert session.source == "codex"
    assert [t.role for t in session.turns] == ["user", "assistant"]
    assert not any(t.is_sidechain for t in session.turns)


def test_a_sub_agent_is_named_by_its_root_thread_at_any_depth(tmp_path: Path) -> None:
    """`session_id` names the root thread on every descendant, so a run is one
    root and everything under it (ADR-0001's `<kind>:<parent>:<stem>`)."""
    Rollout("root").write(tmp_path)
    child = Rollout(
        "grandchild",
        root="root",
        parent="child",
        source={"subagent": {"thread_spawn": {"parent_thread_id": "child", "depth": 2}}},
    )
    child.turn("t1")
    child.assistant("done")
    child.usage("resp-c")
    child.write(tmp_path)

    sessions, _ = _read(tmp_path)

    assert [s.session_id for s in sessions] == ["codex:root", "codex:root:grandchild"]
    grandchild = sessions[1]
    assert all(t.is_sidechain for t in grandchild.turns)
    assert all(r.is_sidechain for r in grandchild.responses)
    assert grandchild.entrypoint == "exec"


def test_two_rollouts_claiming_the_same_thread_are_reported_not_guessed(tmp_path: Path) -> None:
    """A rollback keeps the thread id but starts a new rollout file
    (docs/specs/codex.md#the-rollout), so `collect()` can find two files for
    one thread. Which is current is not on disk, so neither is guessed at;
    the ambiguity is reported instead of returning two sessions sharing one
    `session_id` (ADR-0001's span identity)."""
    first = Rollout("01a1-main")
    first.turn("t1")
    first.user("fix the build")
    first.write(tmp_path)

    second = Rollout("01a1-main")
    second.turn("t2")
    second.user("fix it differently")
    directory = tmp_path / "sessions/2026/10/16"
    second_path = directory / "rollout-2026-10-16T02-05-00-01a1-main_rb1.jsonl"
    second_path.write_text(second.text(), encoding="utf-8")

    sessions, failures = CodexReader(tmp_path).collect(_ALL)

    assert sessions == []
    assert len(failures) == 1
    assert "codex:01a1-main" in failures[0].message
    assert "2 rollouts" in failures[0].message


def test_a_call_is_its_own_turn_and_its_span_never_moves(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.user("go")
    rollout.exec("call-1", "ls", output=None, item=False)
    path = rollout.write(tmp_path)
    reader = CodexReader(tmp_path)

    first, _ = reader.collect_file(path)
    span = first[0].turns[1].tool_calls[0].span
    assert first[0].turns[1].tool_calls[0].status == "pending"

    rollout.item(
        {
            "type": "CommandExecution",
            "id": "call-1",
            "status": "completed",
            "exit_code": 0,
            "command": ["ls"],
        }
    )
    rollout.output("call-1", "Process exited with code 0\nOutput:\nREADME\n")
    rollout.assistant("listed")
    path.write_text(rollout.text(), encoding="utf-8")
    second, _ = reader.collect_file(path)

    call = second[0].turns[1].tool_calls[0]
    assert call.span == span == "codex:t:o5:0"
    assert call.status == "ok"
    assert len(second[0].turns) == 3


def test_an_outcome_written_before_its_call_joins_the_call(tmp_path: Path) -> None:
    """Codex can write a web search's item before the search record. The call
    takes the turn its item opened, so neither is withheld."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.item(
        {
            "type": "WebSearch",
            "id": "ws_1",
            "query": "x",
            "action": {"type": "search", "query": "x"},
        }
    )
    rollout.response(
        {
            "type": "web_search_call",
            "id": "ws_1",
            "status": "completed",
            "action": {"type": "search", "query": "x"},
        }
    )
    rollout.write(tmp_path)

    calls = [c for t in _only(tmp_path).turns for c in t.tool_calls]

    assert len(calls) == 1
    assert (calls[0].tool_name, calls[0].status, calls[0].tool_class) == (
        "web_search",
        "ok",
        "web_search",
    )


def test_a_call_id_used_twice_joins_neither_use(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.call("dup", "exec_command", {"cmd": "a"})
    rollout.call("dup", "exec_command", {"cmd": "b"})
    rollout.output("dup", "Process exited with code 0\nOutput:\n")
    rollout.complete()
    rollout.write(tmp_path)

    calls = [c for t in _only(tmp_path).turns for c in t.tool_calls]

    assert [(c.status, c.result) for c in calls] == [("unknown", None), ("unknown", None)]


def test_history_a_sub_agent_inherited_is_not_its_own(tmp_path: Path) -> None:
    rollout = Rollout(
        "child", root="root", parent="root", meta={"subagent_history_start_ordinal": 3}
    )
    rollout.turn("parent-turn")  # ordinals 1 and 2: inherited
    rollout.user("the parent's words")  # 3 and 4
    rollout.write(tmp_path)

    session = _only(tmp_path)

    assert [t.text for t in session.turns] == ["the parent's words"]


# --- outcomes -----------------------------------------------------------------


def test_each_outcome_comes_from_codexs_own_records(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.exec("ok", "echo hi")
    rollout.exec("failed", "ls /nope", exit_code=2, output="ls: cannot access\n")
    # A sandbox denial: the command ran, failed, and left no item.
    rollout.exec(
        "sandboxed", "touch /etc/x", exit_code=1, output="Read-only file system\n", item=False
    )
    rollout.call("person", "exec_command", {"cmd": "touch a"})
    rollout.output(
        "person",
        'exec_command failed: CreateProcess { message: "Rejected(\\"rejected by user\\")" }',
    )
    rollout.call("policy", "exec_command", {"cmd": "git push"})
    rollout.output(
        "policy",
        "exec_command failed: CreateProcess { message: \"Rejected(\\\"`/bin/bash -lc 'git push'` "
        'rejected: pushing is forbidden by policy\\")" }',
    )
    rollout.call("builtin", "exec_command", {"cmd": "rm -rf ./x"})
    rollout.output(
        "builtin",
        'exec_command failed: CreateProcess { message: "Rejected(\\"`rm -rf ./x` rejected: rm -f style '
        'commands are not permitted. Use a safer approach\\")" }',
    )
    rollout.call("reviewer", "exec_command", {"cmd": "curl x"})
    rollout.output(
        "reviewer",
        'exec_command failed: CreateProcess { message: "Rejected(\\"automatic approval review denied the action\\")" }',
    )
    rollout.call(
        "never", "exec_command", {"cmd": "touch b", "sandbox_permissions": "require_escalated"}
    )
    rollout.output(
        "never",
        "approval policy is Never; reject command — you cannot ask for escalated permissions",
    )
    rollout.patch(
        "declined", "*** Begin Patch\n*** Add File: x\n+x\n*** End Patch\n", status="declined"
    )
    rollout.call("plain", "write_stdin", {"session_id": 1, "chars": "y"})
    rollout.output("plain", "y")
    rollout.complete()
    rollout.write(tmp_path)

    calls = _calls(_only(tmp_path))

    assert {name: (c.status, c.denial_kind, c.exit_code) for name, c in calls.items()} == {
        "ok": ("ok", None, 0),
        "failed": ("error", None, 2),
        "sandboxed": ("error", None, 1),
        "person": ("rejected", "user-rejected", None),
        "policy": ("rejected", "permission-rule", None),
        "builtin": ("rejected", "permission-rule", None),
        "reviewer": ("rejected", "automode-blocked", None),
        "never": ("rejected", "permission-rule", None),
        "declined": ("rejected", "user-rejected", None),
        "plain": ("unknown", None, None),
    }
    assert calls["failed"].error_text == "ls: cannot access\n"
    assert calls["ok"].duration_ms == 1500


def test_what_a_command_printed_is_never_its_outcome(tmp_path: Path) -> None:
    """A command whose own output reads like Codex's refusal is still the
    command's output: the envelope counts only at the start, where Codex
    writes it."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.exec("echo", "cat log", output="rejected by user\naborted by user\n")
    rollout.complete()
    rollout.write(tmp_path)

    call = _calls(_only(tmp_path))["echo"]

    assert (call.status, call.denial_kind) == ("ok", None)


def test_a_call_stopped_by_a_person_is_interrupted_whatever_comes_after(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.call("sleep", "exec_command", {"cmd": "sleep 20"})
    rollout.output("sleep", "Wall time: 3.0 seconds\naborted by user")
    rollout.aborted()
    # Codex appends the process's own end later; it does not undo the interrupt.
    rollout.item(
        {
            "type": "CommandExecution",
            "id": "sleep",
            "status": "failed",
            "exit_code": -1,
            "command": ["sleep"],
        }
    )
    rollout.write(tmp_path)

    assert _calls(_only(tmp_path))["sleep"].status == "interrupted"


def test_a_call_with_no_outcome_is_pending_only_while_its_turn_runs(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.call("open", "exec_command", {"cmd": "make"})
    rollout.write(tmp_path)
    assert _calls(_only(tmp_path))["open"].status == "pending"

    rollout.complete()
    rollout.write(tmp_path)
    assert _calls(_only(tmp_path))["open"].status == "unknown"


def test_mcp_calls_name_their_configured_server(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.mcp("ok", "github", "get_issue", {"number": 1})
    rollout.mcp("bad", "linear", "create_issue", {"title": "x"}, is_error=True)
    # Denied before it ran: only the namespace names the server.
    rollout.call("denied", "search", {"q": "x"}, namespace="mcp__notion")
    rollout.output(
        "denied",
        'exec_command failed: CreateProcess { message: "Rejected(\\"rejected by user\\")" }',
    )
    rollout.call("legacy", "mcp__jira__get", {"id": 1})
    rollout.complete()
    rollout.write(tmp_path)

    calls = _calls(_only(tmp_path))

    assert {n: (c.mcp_server, c.tool_name, c.status, c.tool_class) for n, c in calls.items()} == {
        "ok": ("github", "get_issue", "ok", "mcp"),
        "bad": ("linear", "create_issue", "error", "mcp"),
        "denied": ("notion", "search", "rejected", "mcp"),
        "legacy": ("jira", "get", "unknown", "mcp"),
    }
    assert calls["ok"].result == "Wall time: 0.0 seconds\nOutput:\nok"
    assert calls["bad"].error_text is not None and "401 Unauthorized" in calls["bad"].error_text


def test_a_pending_calls_namespace_is_corrected_from_the_logged_server(tmp_path: Path) -> None:
    """Codex sanitises a server's punctuation into `_`, so `mcp__my_server` is
    indistinguishable on its own from a server actually named `my_server`. A
    connection the log database names breaks the tie (P1 Codex review finding
    on PR #76)."""
    write_logs(
        tmp_path,
        [
            ("rmcp::service", started_line("my-server"), None, "solo"),
            ("codex_core::shell_snapshot", "turn", "t", "solo"),
        ],
    )
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.call("denied", "search", {"q": "x"}, namespace="mcp__my_server")
    rollout.output(
        "denied",
        'exec_command failed: CreateProcess { message: "Rejected(\\"rejected by user\\")" }',
    )
    rollout.complete()
    rollout.write(tmp_path)

    calls = _calls(_only(tmp_path))

    assert calls["denied"].mcp_server == "my-server"
    assert calls["denied"].tool_name == "search"


def test_each_call_ran_where_codex_says(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.exec("item", "pwd", cwd="/work/project/sub")
    rollout.call("relative", "exec_command", {"cmd": "pwd", "workdir": "pkg"})
    rollout.call("inherit", "view_image", {"path": "a.png"})
    rollout.complete()
    rollout.write(tmp_path)

    calls = _calls(_only(tmp_path))

    assert calls["item"].working_directory == "/work/project/sub"
    assert calls["relative"].working_directory == "/work/project/pkg"
    assert calls["inherit"].working_directory == "/work/project"


def test_codex_marks_where_it_cut_an_output(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.exec("long", "cat big", output="start…1200 tokens truncated…end\n")
    rollout.complete()
    rollout.write(tmp_path)

    assert _calls(_only(tmp_path))["long"].truncated is True


# --- tool class and canonical arguments --------------------------------------------


def test_calls_are_classed_and_their_arguments_named_once(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.exec("sh", "curl -s https://example.test")
    rollout.patch(
        "patch",
        "*** Begin Patch\n*** Add File: .env\n+TOKEN=abc\n*** Update File: src/app.py\n@@\n-old\n+new\n*** End Patch\n",
    )
    rollout.call("spawn", "spawn_agent", {"message": "look"}, namespace="multi_agent_v1")
    rollout.call(
        "explore",
        "spawn_agent",
        {"message": "look", "agent_type": "explorer"},
        namespace="multi_agent_v1",
    )
    rollout.call("wait", "wait_agent", {"targets": ["x"]}, namespace="multi_agent_v1")
    rollout.complete()
    rollout.write(tmp_path)

    calls = _calls(_only(tmp_path))

    assert (calls["sh"].tool_class, calls["sh"].canonical_arguments) == (
        "shell",
        {"command": "curl -s https://example.test"},
    )
    assert calls["patch"].tool_class == "file_edit"
    assert calls["patch"].canonical_arguments == {
        "edits": [
            {"path": ".env", "kind": "add", "old_text": "", "new_text": "TOKEN=abc"},
            {"path": "src/app.py", "kind": "update", "old_text": "old", "new_text": "new"},
        ]
    }
    assert (calls["spawn"].tool_class, calls["spawn"].canonical_arguments) == (
        "delegate",
        {"agent": "default"},
    )
    assert calls["explore"].canonical_arguments == {"agent": "explorer"}
    assert (calls["wait"].tool_name, calls["wait"].tool_class) == ("wait_agent", "other")


def test_a_hosted_image_generation_call_is_not_a_coverage_gap(tmp_path: Path) -> None:
    """Codex persists `image_generation_call` as its own self-contained
    response item (`status` and `result` inline, no separate output record),
    the same way `web_search_call` is read; without a branch for it the call
    fell through to the generic "tool coverage is incomplete" warning and the
    session held no turn for it at all (P1 Codex review finding on PR #76)."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.response(
        {
            "type": "image_generation_call",
            "id": "ig_1",
            "status": "completed",
            "result": "<base64>",
            "revised_prompt": "a cat wearing a hat",
        }
    )
    rollout.complete()
    rollout.write(tmp_path)

    calls = _calls(_only(tmp_path))

    assert (calls["ig_1"].tool_name, calls["ig_1"].tool_class, calls["ig_1"].status) == (
        "image_generation",
        "other",
        "ok",
    )
    assert calls["ig_1"].arguments == {"revised_prompt": "a cat wearing a hat"}
    assert (calls["ig_1"].result, calls["ig_1"].result_size) == ("<base64>", len("<base64>"))


def test_view_image_and_dynamic_tool_completion_items_join_their_call(tmp_path: Path) -> None:
    """`ImageView` (a paginated `view_image` result) and `DynamicToolCall` (an
    app-server dynamic tool invocation) are completion items `_ITEMS` did not
    recognize, so each was silently dropped instead of becoming its own call,
    the way `CommandExecution` or `McpToolCall` already does (P2 Codex review
    finding on PR #76)."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.item({"type": "ImageView", "id": "view-1", "path": "file:///work/screenshot.png"})
    rollout.item(
        {
            "type": "DynamicToolCall",
            "id": "dyn-1",
            "tool": "custom_widget",
            "arguments": {"x": 1},
            "status": "completed",
        }
    )
    rollout.complete()
    rollout.write(tmp_path)

    calls = _calls(_only(tmp_path))

    assert (calls["view-1"].tool_name, calls["view-1"].status, calls["view-1"].arguments) == (
        "view_image",
        "ok",
        {"path": "file:///work/screenshot.png"},
    )
    assert (calls["dyn-1"].tool_name, calls["dyn-1"].status, calls["dyn-1"].arguments) == (
        "custom_widget",
        "ok",
        {"x": 1},
    )


def test_a_sleep_extension_call_is_not_a_coverage_gap(tmp_path: Path) -> None:
    """Codex serializes a standalone sleep completion as
    `{"type": "Extension", "kind": "clock.sleep", ...}` (confirmed against
    Codex's own `sleep_extension_item_preserves_type_and_kind` test at
    `rust-v0.163.0`). `_ITEMS` had no `Extension` case, so `_item()` returned
    with no warning at all: a session that slept held no turn for it
    (P2 Codex review finding on PR #76)."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.item({"type": "Extension", "kind": "clock.sleep", "id": "sleep-1", "durationMs": 1000})
    rollout.complete()
    rollout.write(tmp_path)

    call = _calls(_only(tmp_path))["sleep-1"]

    assert (call.tool_name, call.tool_class, call.status, call.arguments) == (
        "sleep",
        "other",
        "ok",
        {"duration_ms": 1000},
    )


def test_a_web_search_extension_call_is_not_a_coverage_gap(tmp_path: Path) -> None:
    """A standalone web search also completes as an `Extension` item, with
    kind `web.search` (confirmed against `ExtensionItem`'s own
    `#[serde(rename = "web.search")]` at `rust-v0.163.0`), wrapping the same
    `WebSearchItem` shape the core `WebSearch` completion item already uses.
    `_item()`'s `Extension` guard only accepted `clock.sleep`, so a
    standalone web search held no turn at all (P1 Codex review finding on
    PR #76)."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.item(
        {
            "type": "Extension",
            "kind": "web.search",
            "id": "ws-1",
            "query": "weather today",
            "action": {"type": "search", "query": "weather today"},
            "results": [{"text": "Sunny, 72F"}],
        }
    )
    rollout.complete()
    rollout.write(tmp_path)

    call = _calls(_only(tmp_path))["ws-1"]

    assert (call.tool_name, call.tool_class, call.status, call.arguments) == (
        "web_search",
        "web_search",
        "ok",
        {"type": "search", "query": "weather today"},
    )
    # Kept whole: `results` is opaque JSON in Codex, so a `text` field is one
    # field among others, never the result on its own (ADR-0035).
    assert call.result == json.dumps([{"text": "Sunny, 72F"}])


def test_a_dynamic_tool_calls_content_items_are_its_result(tmp_path: Path) -> None:
    """`DynamicToolCallItem.content_items` is where a completed dynamic
    tool's text/image/audio output lands; `_item_body` only checked
    `aggregated_output`, `stdout` and `result`, so the call got its status
    and duration but `result`/`result_size` stayed `None` (P2 Codex review
    finding on PR #76)."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.item(
        {
            "type": "DynamicToolCall",
            "id": "dyn-2",
            "tool": "custom_widget",
            "arguments": {},
            "status": "completed",
            "content_items": [{"type": "inputText", "text": "widget output"}],
        }
    )
    rollout.complete()
    rollout.write(tmp_path)

    call = _calls(_only(tmp_path))["dyn-2"]

    assert (call.result, call.result_size) == ("widget output", len("widget output"))


def test_an_execution_no_call_names_is_its_own_call(tmp_path: Path) -> None:
    """A command nested inside another tool has its own id. It becomes its own
    call, and never attaches to an outer call by position."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.item(
        {
            "type": "CommandExecution",
            "id": "nested",
            "command": ["/bin/bash", "-lc", "rm -rf build"],
            "cwd": "file:///work/project",
            "status": "completed",
            "exit_code": 0,
        }
    )
    rollout.complete()
    rollout.write(tmp_path)

    call = _calls(_only(tmp_path))["nested"]

    assert (call.tool_class, call.canonical_arguments, call.status) == (
        "shell",
        {"command": "rm -rf build"},
        "ok",
    )


def _code_mode_cell(rollout: Rollout, call_id: str, source: str, status_line: str) -> None:
    """A Code Mode `exec` cell as Codex 0.163.0 writes it: the script is a
    custom call, and its output opens with the cell's status line."""
    rollout.response(
        {
            "type": "custom_tool_call",
            "status": "completed",
            "call_id": call_id,
            "name": "exec",
            "input": source,
        }
    )
    rollout.output(
        call_id,
        [
            {"type": "input_text", "text": f"{status_line}\nWall time 0.0 seconds\nOutput:\n"},
            {"type": "input_text", "text": "{}"},
        ],
        kind="custom_tool_call_output",
    )


def test_a_code_mode_script_is_classed_and_its_tools_stay_their_own_calls(
    tmp_path: Path,
) -> None:
    """A Code Mode cell is a script, not the tool it runs. Each tool the script
    calls has its own completion item and stays its own call, with its own
    outcome; the cell's outcome is its own status line (ADR-0036)."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.response(
        {
            "type": "custom_tool_call",
            "status": "completed",
            "call_id": "cell",
            "name": "exec",
            "input": 'text(await tools.mcp__northwind__fetch_record({"record":"northwind"}));',
        }
    )
    rollout.item(
        {
            "type": "McpToolCall",
            "id": "exec-nested",
            "server": "northwind",
            "tool": "fetch_record",
            "arguments": {"record": "northwind"},
            "status": "failed",
            "result": {"content": [{"type": "text", "text": "locked"}], "isError": True},
        }
    )
    rollout.output(
        "cell",
        [{"type": "input_text", "text": "Script completed\nWall time 0.0 seconds\nOutput:\n"}],
        kind="custom_tool_call_output",
    )
    _code_mode_cell(rollout, "broken", "text(await tools.apply_patch(1));", "Script failed")
    _code_mode_cell(rollout, "stopped", "await sleep(9e9);", "Script terminated")
    rollout.complete()
    rollout.write(tmp_path)

    calls = _calls(_only(tmp_path))

    assert (calls["cell"].tool_class, calls["cell"].status) == ("script", "ok")
    assert (calls["broken"].tool_class, calls["broken"].status) == ("script", "error")
    assert (calls["stopped"].tool_class, calls["stopped"].status) == ("script", "interrupted")
    nested = calls["exec-nested"]
    assert (nested.mcp_server, nested.tool_name, nested.tool_class, nested.status) == (
        "northwind",
        "fetch_record",
        "mcp",
        "error",
    )


def test_a_code_mode_cells_status_survives_a_media_item_after_it(tmp_path: Path) -> None:
    """Code Mode's output wrapper keeps the status header at index 0 even
    when a later item is media (`core/src/tools/code_mode/output.rs`); `_body`
    cannot join such a list as text, since not every item has a `text` key,
    so the cell's status must come from the first item alone (P2 Codex review
    finding on PR #78)."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.response(
        {
            "type": "custom_tool_call",
            "status": "completed",
            "call_id": "cell",
            "name": "exec",
            "input": "text(await tools.render());",
        }
    )
    rollout.output(
        "cell",
        [
            {"type": "input_text", "text": "Script completed\nWall time 0.0 seconds\nOutput:\n"},
            {"type": "input_image", "image_url": "data:image/png;base64,iVBOR..."},
        ],
        kind="custom_tool_call_output",
    )
    rollout.complete()
    rollout.write(tmp_path)

    call = _calls(_only(tmp_path))["cell"]

    assert (call.tool_class, call.status) == ("script", "ok")


def test_a_code_mode_status_line_is_read_only_from_a_script(tmp_path: Path) -> None:
    """Only Code Mode's `exec` writes a status line. Another custom tool whose
    output happens to open with the same words keeps the outcome it had."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.response(
        {
            "type": "custom_tool_call",
            "status": "completed",
            "call_id": "patch",
            "name": "apply_patch",
            "input": "*** Begin Patch\n*** End Patch",
        }
    )
    rollout.output("patch", "Script failed\n", kind="custom_tool_call_output")
    rollout.complete()
    rollout.write(tmp_path)

    call = _calls(_only(tmp_path))["patch"]

    assert (call.tool_class, call.status) == ("file_edit", "unknown")


def test_a_namespaced_exec_tool_keeps_its_own_outcome(tmp_path: Path) -> None:
    """Codex's `is_exec_tool_name` requires the default namespace
    (`core/src/tools/code_mode/mod.rs`); a namespaced tool also named `exec`
    is not Code Mode, even if its output opens with a status line's words
    (P2 Codex review finding on PR #78)."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.response(
        {
            "type": "custom_tool_call",
            "status": "completed",
            "call_id": "vendor-exec",
            "name": "exec",
            "namespace": "vendor",
            "input": "{}",
        }
    )
    rollout.output("vendor-exec", "Script failed\nreal output\n", kind="custom_tool_call_output")
    rollout.complete()
    rollout.write(tmp_path)

    call = _calls(_only(tmp_path))["vendor-exec"]

    assert call.status != "error"


def test_an_interrupted_collab_agent_call_is_interrupted_not_unknown(tmp_path: Path) -> None:
    """`CollabAgentToolCallStatus` has a real `interrupted` variant (wire value
    `interrupted`) alongside `completed`/`failed`, which `_outcome` matched
    neither of, falling through to `unknown` instead of the model's own
    `interrupted` status (P2 Codex review finding on PR #76)."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.item(
        {
            "type": "CollabAgentToolCall",
            "id": "collab-1",
            "tool": "wait_agent",
            "arguments": {"targets": ["x"]},
            "status": "interrupted",
        }
    )
    rollout.complete()
    rollout.write(tmp_path)

    assert _calls(_only(tmp_path))["collab-1"].status == "interrupted"


# --- usage --------------------------------------------------------------------------


def test_usage_is_one_response_per_record_with_cached_input_apart(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1", window=400000)
    rollout.user("go")
    rollout.exec("c1", "ls")
    rollout.usage("resp-1", input_tokens=1300, cached=650, output_tokens=51, reasoning=11)
    rollout.assistant("done")
    rollout.usage(
        "resp-2", input_tokens=1400, cached=0, output_tokens=5, reasoning=0, cache_write=120
    )
    rollout.usage("resp-2", input_tokens=9, cached=0, output_tokens=9)  # a repeat counts once
    rollout.complete()
    rollout.write(tmp_path)

    session = _only(tmp_path)

    assert [r.response_id for r in session.responses] == ["resp-1", "resp-2"]
    first, second = session.responses
    assert (first.usage.input_tokens, first.usage.cache_read_input_tokens) == (650, 650)
    assert (first.usage.output_tokens, first.usage.reasoning_tokens) == (51, 11)
    assert first.usage.cache_creation_input_tokens is None
    assert second.usage.cache_creation_input_tokens == 120
    assert (first.model, first.final, first.context_window) == ("gpt-5.5", True, 400000)
    calls = [t for t in session.turns if t.tool_calls]
    assert calls[0].response_id == "resp-1"
    assert session.turns[-1].response_id == "resp-2"


# --- session fields -------------------------------------------------------------


def test_session_fields_come_from_the_records(tmp_path: Path) -> None:
    rollout = Rollout("t", source="vscode", originator="codex_vscode", branch="feature/x")
    rollout.developer("<permissions instructions>")
    rollout.injected("<environment_context>\n  <cwd>/work</cwd>\n</environment_context>")
    rollout.turn("t1", approval="on-request", sandbox="read-only")
    rollout.user("ship it")
    rollout.response(
        {
            "type": "tool_search_output",
            "call_id": "ts",
            "status": "completed",
            "tools": [
                {
                    "type": "namespace",
                    "name": "mcp__github__",
                    "description": "GitHub tools.",
                    "tools": [],
                }
            ],
        }
    )
    rollout.complete()
    rollout.write(tmp_path)
    (tmp_path / "session_index.jsonl").write_text(
        json.dumps({"id": "t", "thread_name": "first", "updated_at": "x"})
        + "\n"
        + json.dumps({"id": "t", "thread_name": "renamed", "updated_at": "y"})
        + "\n",
        encoding="utf-8",
    )

    session = _only(tmp_path)

    assert session.entrypoint == "vscode"
    assert (session.agent_version, session.git_branch, session.model) == (
        "0.163.0",
        "feature/x",
        "gpt-5.5",
    )
    assert session.working_directory == "/work/project"
    assert session.initial_prompt == "ship it"
    assert session.title == "renamed"
    assert session.turns[0].permission_mode == "on-request; read-only"
    assert [(c.source, [p.name for p in c.parts]) for c in session.context_items] == [
        ("system", []),
        ("system", []),
        ("injected", []),
        ("mcp_instructions", ["github"]),
    ]
    assert session.compactions_recorded and session.subagents_recorded
    assert session.started_at == datetime(2026, 10, 16, 2, 0, 0, tzinfo=UTC)


def test_a_title_nested_past_the_decoders_limit_is_ignored_not_fatal(tmp_path: Path) -> None:
    """`_titles()` runs before the per-rollout loop in `collect()`, with no
    `_read()` of its own to withhold just one session; a `RecursionError`
    there must not escape and cost the whole pass its Codex sessions, the
    same way a merely malformed title line is ignored (P2 Codex review
    finding on PR #76)."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.user("fix the build")
    rollout.write(tmp_path)
    depth = 100_000
    nested = ("[" * depth) + ("]" * depth)
    (tmp_path / "session_index.jsonl").write_text(f'{{"id": "t", "thread_name": {nested}}}\n')

    session = _only(tmp_path)

    assert session.title is None


def test_client_errors_are_turns_and_provider_refusals_are_named(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.user("go")
    rollout.complete(
        error="You’ve hit your usage limit. Try again later.", code="usage_limit_exceeded"
    )
    rollout.turn("t2")
    rollout.complete(error="Invalid prompt: your prompt was flagged.", code="invalid_prompt")
    rollout.turn("t3")
    rollout.complete(
        error="stream disconnected before completion: Incomplete response returned, reason: content_filter",
        code="other",
    )
    rollout.write(tmp_path)

    session = _only(tmp_path)

    errors = [t for t in session.turns if t.role == "error"]
    assert [e.text[:30] for e in errors] == [
        "You’ve hit your usage limit. T",
        "Invalid prompt: your prompt wa",
        "stream disconnected before com",
    ]
    assert all(not e.tool_calls for e in errors)
    assert [r.category for r in session.provider_refusals] == ["invalid_prompt", "content_filter"]


def test_a_compaction_says_how_much_it_replaced(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.add(
        "compacted", {"message": "", "latest_token_usage_record": {"usage": {"input_tokens": 2100}}}
    )
    rollout.write(tmp_path)

    compaction = _only(tmp_path).compactions[0]

    assert (compaction.trigger, compaction.pre_tokens) == ("unknown", 2100)
    assert compaction.occurred_at is not None


def _search(call_id: str, *tools: dict[str, object]) -> dict[str, object]:
    """A `tool_search_output`, its entries shaped as Codex 0.163.0 writes a
    `LoadableToolSpec` (`tools/src/responses_api.rs`)."""
    return {
        "type": "tool_search_output",
        "call_id": call_id,
        "status": "completed",
        "execution": "client",
        "tools": list(tools),
    }


def _namespace(name: str, description: str, *tools: dict[str, object]) -> dict[str, object]:
    return {"type": "namespace", "name": name, "description": description, "tools": list(tools)}


def _function(name: object, description: object) -> dict[str, object]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": False,
        "parameters": {"type": "object", "properties": {}},
    }


def _tool_listings(session: Session) -> list[list[tuple[str, str]]]:
    return [
        [(p.name, p.text) for p in item.parts]
        for item in session.context_items
        if item.source == "tool_listing"
    ]


def test_a_tool_search_keeps_each_tools_description_beside_its_servers(tmp_path: Path) -> None:
    """The namespace's description is the server's own instructions. Each tool
    in it has a description of its own, and a consumer judging a call needs
    that one."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.user("file it")
    rollout.response(
        _search(
            "ts",
            _namespace(
                "mcp__linear__",
                "Linear tools.",
                _function("create_issue", "Files an issue."),
                {
                    "type": "custom",
                    "name": "query",
                    "description": "Runs a query.",
                    "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.+/"},
                },
            ),
            _namespace("codex_app", "App tools.", _function("open_page", "Opens a page.")),
            _function("lookup", "Looks a word up."),
        )
    )
    rollout.complete()
    rollout.write(tmp_path)

    session = _only(tmp_path)

    servers, tools = (
        item
        for item in session.context_items
        if item.source in ("mcp_instructions", "tool_listing")
    )
    assert (servers.source, [(p.name, p.text) for p in servers.parts]) == (
        "mcp_instructions",
        [("linear", "Linear tools.")],
    )
    assert (tools.source, tools.name) == ("tool_listing", None)
    assert [(p.name, p.text) for p in tools.parts] == [
        ("mcp__linear__create_issue", "Files an issue."),
        ("mcp__linear__query", "Runs a query."),
        ("codex_app.open_page", "Opens a page."),
        ("lookup", "Looks a word up."),
    ]
    assert tools.text == "Files an issue.\n\nRuns a query.\n\nOpens a page.\n\nLooks a word up."
    # One record gives both items. Each has its own span.
    line = servers.span.rsplit(":", 1)[1]
    assert servers.span == f"codex:t:context:{line}"
    assert tools.span == f"codex:t:context.tools:{line}"


def test_a_tool_search_lists_a_tool_once_and_again_when_its_words_change(
    tmp_path: Path,
) -> None:
    """A later search that finds a tool the session already listed repeats
    it. Only what is new is listed: a tool not seen, or new words for one."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.user("go")
    first = _namespace("mcp__x__", "X.", _function("a", "Does a."), _function("a", "Does a."))
    rollout.response(_search("ts1", first))
    path = rollout.write(tmp_path)
    reader = CodexReader(tmp_path)
    before, _ = reader.collect_file(path)

    rollout.response(_search("ts2", _namespace("mcp__x__", "X.", _function("a", "Does a."))))
    rollout.response(
        _search(
            "ts3",
            _namespace("mcp__x__", "X.", _function("a", "Does a, then b."), _function("c", "C.")),
        )
    )
    path.write_text(rollout.text(), encoding="utf-8")
    after, _ = reader.collect_file(path)

    assert _tool_listings(before[0]) == [[("mcp__x__a", "Does a.")]]
    assert _tool_listings(after[0]) == [
        [("mcp__x__a", "Does a.")],
        [("mcp__x__a", "Does a, then b."), ("mcp__x__c", "C.")],
    ]
    # The listing read first keeps its span on the re-read.
    first_span = next(i.span for i in before[0].context_items if i.source == "tool_listing")
    assert next(i.span for i in after[0].context_items if i.source == "tool_listing") == first_span


def test_every_listed_tool_is_named_as_the_reader_names_a_call_to_it(tmp_path: Path) -> None:
    """A consumer joins a tool's description to a call by name, so a part's
    name is the name a call to that tool gets: across MCP namespaces (one
    whose server name has punctuation that only the log restores), other
    namespaces, Codex's own namespaced tools and tools in no namespace."""
    write_logs(
        tmp_path,
        [
            ("rmcp::service", started_line("my-server"), None, "solo"),
            ("codex_core::shell_snapshot", "turn", "t", "solo"),
        ],
    )
    listed = [
        ("mcp__my_server__", "search"),
        ("mcp__github__", "create_issue"),
        ("codex_app", "open_page"),
        ("multi_agent_v1", "spawn_agent"),
        ("clock", "sleep"),
        (None, "lookup"),
        (None, "mcp__flat__fetch"),
    ]
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.user("go")
    rollout.response(
        _search(
            "ts",
            *(
                _namespace(namespace, f"{namespace} tools.", _function(name, f"Does {name}."))
                if namespace is not None
                else _function(name, f"Does {name}.")
                for namespace, name in listed
            ),
        )
    )
    for index, (namespace, name) in enumerate(listed):
        # Codex calls an MCP tool under the namespace without its trailing `__`.
        called = namespace.removesuffix("__") if namespace is not None else None
        rollout.call(f"c{index}", name, {}, namespace=called)
    rollout.complete()
    rollout.write(tmp_path)

    session = _only(tmp_path)

    calls = _calls(session)
    called_names = [
        f"mcp__{call.mcp_server}__{call.tool_name}" if call.mcp_server else call.tool_name
        for call in (calls[f"c{index}"] for index in range(len(listed)))
    ]
    (parts,) = _tool_listings(session)
    assert [name for name, _ in parts] == called_names
    assert called_names[0] == "mcp__my-server__search"


def _completed_mcp(rollout: Rollout, call_id: str, server: str, tool: str) -> None:
    """A call Codex made under the sanitised namespace, and its completed item,
    which names the server as it is configured."""
    rollout.call(call_id, tool, {}, namespace=f"mcp__{re.sub(r'[^A-Za-z0-9_]', '_', server)}")
    rollout.item(
        {
            "type": "McpToolCall",
            "id": call_id,
            "server": server,
            "tool": tool,
            "arguments": {},
            "status": "completed",
            "result": {"content": [{"type": "text", "text": "ok"}], "isError": False},
        }
    )
    rollout.output(call_id, [{"type": "input_text", "text": "ok"}])


@pytest.mark.parametrize(
    ("logged", "completed", "expected_server"),
    [
        # A completed item names the server, and no log is placed (`codex exec`).
        (None, ["my-server"], "my-server"),
        # The item and the log disagree: Codex's own record of the call wins,
        # for the call and for the listing alike.
        ("my.server", ["my-server"], "my-server"),
        # Only the log names it, and no listed tool is called.
        ("my-server", [], "my-server"),
        # Nothing names it: the written name, for a call and a listing alike.
        (None, [], "my_server"),
        # Two completed items name different servers: no item decides, so the
        # log does. Each call keeps the server its own item names.
        ("my-server", ["my-server", "my.server"], "my-server"),
    ],
)
def test_a_listed_mcp_tool_is_named_from_the_same_evidence_as_its_calls(
    logged: str | None, completed: list[str], expected_server: str, tmp_path: Path
) -> None:
    """A call's server comes from its completed item, then the connection log,
    then the namespace. A listed tool is named from the same evidence, built
    once for the session, so a consumer joins a description to a call by name,
    also for a tool the session never called."""
    if logged is not None:
        write_logs(
            tmp_path,
            [
                ("rmcp::service", started_line(logged), None, "solo"),
                ("codex_core::shell_snapshot", "turn", "t", "solo"),
            ],
        )
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.user("go")
    rollout.response(
        _search(
            "ts",
            _namespace(
                "mcp__my_server__",
                "Tools.",
                _function("search", "Searches."),
                _function("never_called", "Is never called."),
            ),
        )
    )
    for index, server in enumerate(completed):
        _completed_mcp(rollout, f"c{index}", server, "search")
    rollout.complete()
    rollout.write(tmp_path)

    session = _only(tmp_path)

    (parts,) = _tool_listings(session)
    assert [name for name, _ in parts] == [
        f"mcp__{expected_server}__search",
        f"mcp__{expected_server}__never_called",
    ]
    calls = _calls(session)
    assert [calls[f"c{i}"].mcp_server for i in range(len(completed))] == completed
    if len(set(completed)) == 1:
        call = calls["c0"]
        assert parts[0][0] == f"mcp__{call.mcp_server}__{call.tool_name}"


def test_a_searched_tool_without_a_name_or_a_description_is_not_a_part(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.user("go")
    rollout.response(
        _search(
            "ts1",
            _namespace(
                "mcp__x__",
                "X.",
                _function("empty", ""),
                _function("not_text", 7),
                _function(None, "No name."),
                {"type": "function", "name": "no_description"},
                "not a tool",  # type: ignore[arg-type]
                _function("a", "Does a."),
            ),
            {"type": "namespace", "name": "mcp__y__", "description": "Y.", "tools": "x"},
            {"type": "namespace", "name": "mcp__z__", "description": "Z."},
        )
    )
    rollout.response(_search("ts2", _namespace("mcp__x__", "X.", _function("empty", ""))))
    rollout.complete()
    rollout.write(tmp_path)

    session = _only(tmp_path)

    assert _tool_listings(session) == [[("mcp__x__a", "Does a.")]]


# --- files ---------------------------------------------------------------------------


def test_archived_rollouts_are_read_and_other_files_are_not(tmp_path: Path) -> None:
    """Compressed rollouts are read too: `test_codex_compressed.py`."""
    Rollout("live").write(tmp_path)
    Rollout("old").write(tmp_path, archived=True)
    (tmp_path / "sessions" / "notes.txt").write_text("not a rollout", encoding="utf-8")
    (tmp_path / "sessions" / "rollout-compress-1.tmp").write_bytes(b"Codex's own temporary")

    sessions, failures = _read(tmp_path)

    assert [s.session_id for s in sessions] == ["codex:live", "codex:old"]
    assert failures == []


def test_a_malformed_record_withholds_the_session_and_never_echoes_it(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    path = rollout.write(tmp_path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"type": "response_item", "payload": SECRET-TOKEN\n')

    sessions, failures = _read(tmp_path)

    assert sessions == []
    assert len(failures) == 1 and "SECRET-TOKEN" not in failures[0]
    assert "malformed rollout record" in failures[0]


def test_a_record_nested_past_the_decoders_limit_withholds_the_session(tmp_path: Path) -> None:
    """`json.loads` on a pathologically nested record raises `RecursionError`,
    not the `ValueError` a merely malformed record raises; a cold pass must
    withhold only this rollout, not let the exception escape and cost every
    Codex session the pass was collecting (P2 Codex review finding on PR
    #76)."""
    rollout = Rollout("t")
    rollout.turn("t1")
    path = rollout.write(tmp_path)
    depth = 100_000
    nested = ("[" * depth) + ("]" * depth)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f'{{"type": "response_item", "payload": {nested}}}\n')

    sessions, failures = _read(tmp_path)

    assert sessions == []
    assert len(failures) == 1
    assert "malformed rollout record" in failures[0]


def test_a_record_that_does_not_advance_the_ordinal_withholds_the_session(
    tmp_path: Path,
) -> None:
    """Codex's own ordinal is monotonic per file (docs/specs/codex.md#the-rollout);
    a repeat or a regression would key two turns alike, colliding their spans
    (P1 Codex review finding on PR #76)."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.user("first")
    replayed = rollout._ordinal - 1
    path = rollout.write(tmp_path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "timestamp": "2026-10-16T02:00:10.000Z",
                    "type": "event_msg",
                    "ordinal": replayed,
                    "payload": {
                        "type": "item_completed",
                        "thread_id": "t",
                        "turn_id": "t1",
                        "item": {"type": "UserMessage", "id": "replayed", "text": "again"},
                    },
                }
            )
            + "\n"
        )

    sessions, failures = _read(tmp_path)

    assert sessions == []
    assert len(failures) == 1
    assert "malformed rollout record" in failures[0]


def test_a_timestamp_that_overflows_converting_to_utc_is_not_fatal(tmp_path: Path) -> None:
    """A boundary offset like `0001-01-01T00:00:00+23:59` parses fine with
    `datetime.fromisoformat` but raises `OverflowError` converting to UTC,
    not the `ValueError` an unparseable timestamp raises. The record's own
    instant is simply absent, not fatal to the whole session (P2 Codex
    review finding on PR #76)."""
    rollout = Rollout("t")
    rollout.turn("t1")
    next_ordinal = rollout._ordinal
    path = rollout.write(tmp_path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "timestamp": "0001-01-01T00:00:00+23:59",
                    "type": "response_item",
                    "ordinal": next_ordinal,
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hi"}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": "t1"},
                    },
                }
            )
            + "\n"
        )

    session = _only(tmp_path)

    assert session.turns[-1].occurred_at is None


def test_a_record_still_being_written_waits_for_its_newline(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    path = rollout.write(tmp_path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"timestamp": "2026-10-16T02:00:09.000Z", "ordinal": 3, "type": "resp')
    reader = CodexReader(tmp_path)

    sessions, failures = reader.collect_file(path)
    assert failures == [] and sessions[0].turns == ()

    rollout.user("now complete")
    path.write_text(rollout.text(), encoding="utf-8")
    sessions, _ = reader.collect_file(path)
    assert [t.text for t in sessions[0].turns] == ["now complete"]


def test_a_replaced_rollout_is_read_from_the_start(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.user("first")
    path = rollout.write(tmp_path)
    reader = CodexReader(tmp_path)
    reader.collect_file(path)

    replacement = Rollout("t")
    replacement.turn("t1")
    replacement.user("second")
    staged = replacement.write(tmp_path / "staged")
    staged.replace(path)
    sessions, _ = reader.collect_file(path)

    assert [t.text for t in sessions[0].turns] == ["second"]


def test_collect_file_reads_only_rollouts_under_its_locations(tmp_path: Path) -> None:
    reader = CodexReader(tmp_path / "home")
    elsewhere = Rollout("t").write(tmp_path / "other")

    assert reader.collect_file(elsewhere) == ([], [])
    assert reader.collect_file(tmp_path / "home" / "sessions" / "x.txt") == ([], [])
    assert reader.locations() == (
        tmp_path / "home" / "sessions",
        tmp_path / "home" / "archived_sessions",
    )


def test_the_window_keeps_threads_active_inside_it(tmp_path: Path) -> None:
    Rollout("t").write(tmp_path)
    sessions, _ = CodexReader(tmp_path).collect(Window(since=datetime(2026, 10, 17, tzinfo=UTC)))
    assert sessions == []


def test_no_home_is_no_sessions_and_no_failure(tmp_path: Path) -> None:
    assert CodexReader(tmp_path / "absent").collect(_ALL) == ([], [])


def test_the_default_readers_read_codex_from_its_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Rollout("t").write(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))

    collection = collect(parse_kind_filter(["codex"]), _ALL, default_readers(tmp_path / "claude"))

    assert [s.session_id for s in collection.sessions] == ["codex:t"]


def test_the_incremental_cache_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("openaidr.readers.codex._MAX_INCREMENTAL_FILES", 2)
    paths = [Rollout(f"t{i}").write(tmp_path) for i in range(3)]
    reader = CodexReader(tmp_path)
    for path in paths:
        reader.collect_file(path)

    assert list(reader._cursors) == [p.absolute() for p in paths[1:]]
