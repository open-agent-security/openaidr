"""Codex parity: project facts from durable records, not output wording."""

from pathlib import Path

from openaidr.readers.base import Window
from openaidr.readers.codex import CodexReader
from tests.fixtures.codex_jsonl import call, message, meta, output, record, write_rollout


def completed(item: dict[str, object]) -> dict[str, object]:
    return record(
        "event_msg",
        {"type": "item_completed", "thread_id": "s1", "turn_id": "turn1", "item": item},
        4,
    )


def test_structured_outcomes_enrich_calls_without_duplicating_them(tmp_path: Path) -> None:
    write_rollout(
        tmp_path,
        [
            meta(),
            record("turn_context", {"turn_id": "turn1"}),
            call("ok"),
            completed(
                {
                    "type": "CommandExecution",
                    "id": "ok",
                    "status": "completed",
                    "exit_code": 0,
                    "cwd": "/run",
                    "plugin_id": "synthetic-plugin",
                }
            ),
            output("ok"),
            call("error"),
            output("error"),
            completed(
                {
                    "type": "CommandExecution",
                    "id": "error",
                    "status": "failed",
                    "exit_code": 1,
                    "stderr": "synthetic failure",
                }
            ),
            call("denied", custom=True),
            completed({"type": "FileChange", "id": "denied", "status": "declined"}),
            output("denied", custom=True),
        ],
    )
    sessions, failures = CodexReader(tmp_path).collect(Window(None))
    assert not failures
    tools = [t.tool_calls[0] for t in sessions[0].turns]
    assert [t.status for t in tools] == ["ok", "error", "rejected"]
    assert tools[0].working_directory == "/run"
    assert tools[0].attributed_plugin == "synthetic-plugin"
    assert tools[1].error_text == "synthetic failure"
    assert tools[2].denial_kind == "declined"


def test_nested_execution_is_its_own_call_and_mcp_has_recorded_identity(tmp_path: Path) -> None:
    write_rollout(
        tmp_path,
        [
            meta(),
            call("outer", custom=True, name="exec"),
            completed(
                {
                    "type": "CommandExecution",
                    "id": "exec-inner",
                    "command": ["echo", "synthetic"],
                    "status": "completed",
                    "exit_code": 0,
                    "aggregated_output": "synthetic",
                }
            ),
            call("mcp", name="namespace.alias"),
            completed(
                {
                    "type": "McpToolCall",
                    "id": "mcp",
                    "server": "real_server",
                    "tool": "lookup",
                    "status": "completed",
                    "result": {"content": [], "isError": True},
                }
            ),
        ],
    )
    sessions, failures = CodexReader(tmp_path).collect(Window(None))
    assert not failures
    tools = [t.tool_calls[0] for t in sessions[0].turns]
    assert [t.status for t in tools] == ["pending", "ok", "error"]
    assert tools[1].provider_call_id == "exec-inner"
    assert tools[1].arguments == {"command": ["echo", "synthetic"]}
    assert (tools[2].mcp_server, tools[2].tool_name) == ("real_server", "lookup")
    assert tools[2].transport is None


def test_legacy_mcp_and_patch_events(tmp_path: Path) -> None:
    write_rollout(
        tmp_path,
        [
            meta(),
            call("mcp"),
            output("mcp"),
            record(
                "event_msg",
                {
                    "type": "mcp_tool_call_end",
                    "call_id": "mcp",
                    "invocation": {"server": "synthetic", "tool": "lookup", "arguments": {}},
                    "result": {"Ok": {"content": [], "isError": False}},
                },
            ),
            call("patch", custom=True),
            record(
                "event_msg",
                {
                    "type": "patch_apply_end",
                    "call_id": "patch",
                    "success": False,
                    "status": "failed",
                    "stderr": "synthetic patch failure",
                },
            ),
        ],
    )
    session = CodexReader(tmp_path).collect(Window(None))[0][0]
    assert [t.tool_calls[0].status for t in session.turns] == ["ok", "error"]
    assert session.turns[0].tool_calls[0].mcp_server == "synthetic"


def test_permission_context_prompt_and_compaction_are_preserved_locally(tmp_path: Path) -> None:
    write_rollout(
        tmp_path,
        [
            meta(base_instructions={"text": "base instructions"}),
            record(
                "turn_context",
                {
                    "turn_id": "turn1",
                    "approval_policy": "on-request",
                    "sandbox_policy": {"type": "workspace-write"},
                },
            ),
            message("developer instructions", "developer"),
            message("user prompt"),
            record("event_msg", {"type": "user_message", "message": "user prompt"}),
            call(),
            output(),
            record(
                "compacted",
                {"message": "synthetic summary", "replacement_history": [call()["payload"]]},
            ),
            record(
                "turn_context",
                {
                    "turn_id": "turn2",
                    "approval_policy": "never",
                    "sandbox_policy": {"type": "read-only"},
                },
            ),
            message("assistant response", "assistant"),
        ],
    )
    session = CodexReader(tmp_path).collect(Window(None))[0][0]
    assert len(session.turns) == 3
    assert session.initial_prompt == "user prompt"
    assert "on-request" in (session.turns[0].permission_mode or "")
    assert "read-only" in (session.turns[-1].permission_mode or "")
    assert [c.text for c in session.context_items] == [
        "base instructions",
        "developer instructions",
        "synthetic summary",
    ]
    assert len({c.span for c in session.context_items}) == 3
    assert len(session.compactions) == 1
    assert session.compactions[0].trigger == "unknown"
    assert "initial_prompt" not in session.public_descriptor()


def test_native_tool_search_and_web_search(tmp_path: Path) -> None:
    write_rollout(
        tmp_path,
        [
            meta(),
            record(
                "response_item",
                {
                    "type": "tool_search_call",
                    "call_id": "search",
                    "arguments": {"query": "synthetic"},
                    "execution": "client",
                },
            ),
            record(
                "response_item",
                {
                    "type": "tool_search_output",
                    "call_id": "search",
                    "status": "completed",
                    "execution": "client",
                    "tools": [],
                },
            ),
            record(
                "response_item",
                {
                    "type": "web_search_call",
                    "id": "web",
                    "status": "completed",
                    "action": {"type": "search", "query": "synthetic"},
                },
            ),
        ],
    )
    sessions, failures = CodexReader(tmp_path).collect(Window(None))
    assert not failures
    tools = [t.tool_calls[0] for t in sessions[0].turns]
    assert [t.tool_name for t in tools] == ["tool_search", "web_search"]
    assert [t.status for t in tools] == ["ok", "ok"]


def test_enrichment_keeps_incremental_snapshots_and_duplicate_withholding(tmp_path: Path) -> None:
    import json

    path = write_rollout(tmp_path, [meta(), call()])
    reader = CodexReader(tmp_path)
    before, _ = reader.collect_file(path)
    event = completed(
        {"type": "CommandExecution", "id": "call1", "status": "failed", "exit_code": 2}
    )
    with path.open("a") as stream:
        stream.write(json.dumps(event) + "\n")
    after, failures = reader.collect_file(path)
    assert not failures
    assert before[0].turns[0].tool_calls[0].status == "pending"
    assert after[0].turns[0].tool_calls[0].status == "error"
    assert after == CodexReader(tmp_path).collect(Window(None))[0]
    with path.open("a") as stream:
        stream.write(json.dumps(call()) + "\n")
        stream.write(json.dumps(event) + "\n")
    ambiguous, _ = reader.collect_file(path)
    assert len(ambiguous[0].turns) == 2
    assert all(t.tool_calls[0].status == "pending" for t in ambiguous[0].turns)


def test_paginated_prompt_and_tool_search_without_call_id(tmp_path: Path) -> None:
    write_rollout(
        tmp_path,
        [
            meta(),
            completed(
                {
                    "type": "UserMessage",
                    "id": "user1",
                    "content": [{"type": "text", "text": "actual prompt"}],
                }
            ),
            record(
                "response_item",
                {
                    "type": "tool_search_call",
                    "arguments": {"query": "synthetic"},
                    "execution": "server",
                },
            ),
            record(
                "response_item",
                {
                    "type": "tool_search_output",
                    "status": "completed",
                    "tools": [],
                    "execution": "server",
                },
            ),
            record(
                "response_item",
                {
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {"type": "search", "query": "synthetic"},
                },
            ),
        ],
    )
    sessions, failures = CodexReader(tmp_path).collect(Window(None))
    assert not failures
    assert sessions[0].initial_prompt == "actual prompt"
    # Without a call id, tool-search output cannot resolve the issued call.
    assert [t.tool_calls[0].status for t in sessions[0].turns] == ["pending", "ok"]
