"""The Codex reader against Codex 0.163.0's own closed vocabularies (ADR-0033).

Each list here is copied from a Codex enum at `rust-v0.163.0`, not from the
reader, so a member the reader misses fails by name. When Codex adds a member,
it is added here first, and the reader follows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openaidr.model import Session, ToolCall
from openaidr.readers.base import Window
from openaidr.readers.codex import CodexReader
from openaidr.toolclass import TOOL_CLASSES, tool_class
from tests.fixtures.codex_rollout import Rollout

_ALL = Window(since=None)

#: `codex_protocol::items::TurnItem` (`protocol/src/items.rs`): the item an
#: `item_completed` event carries.
_TURN_ITEMS = {
    "UserMessage",
    "FunctionCallOutput",
    "HookPrompt",
    "AgentMessage",
    "Plan",
    "Reasoning",
    "CommandExecution",
    "DynamicToolCall",
    "CollabAgentToolCall",
    "SubAgentActivity",
    "WebSearch",
    "ImageView",
    "Extension",
    "ImageGeneration",
    "EnteredReviewMode",
    "ExitedReviewMode",
    "FileChange",
    "McpToolCall",
    "ContextCompaction",
}
#: `codex_extension_items::ExtensionItem` (`ext/items/src/lib.rs`), by `kind`.
_EXTENSION_KINDS = {"image_gen.generation", "clock.sleep", "web.search"}
#: `CollabAgentTool` (`protocol/src/items.rs`), as written.
_COLLAB_TOOLS = {
    "spawn_agent",
    "send_input",
    "resume_agent",
    "wait",
    "close_agent",
    "send_message",
    "followup_task",
    "interrupt_agent",
    "list_agents",
}
#: The call each item that records a call stands for, as the reader names it.
#: An item outside this table and `_NOT_CALLS` is a coverage gap Codex added.
_CALL_ITEMS = {
    "CommandExecution": {"type": "CommandExecution", "command": ["ls"], "status": "completed"},
    "DynamicToolCall": {
        "type": "DynamicToolCall",
        "tool": "widget",
        "arguments": {},
        "status": "completed",
    },
    "CollabAgentToolCall": {
        "type": "CollabAgentToolCall",
        "tool": "list_agents",
        "status": "completed",
        "sender_thread_id": "t",
    },
    "WebSearch": {"type": "WebSearch", "query": "q", "action": {"type": "search"}},
    "ImageView": {"type": "ImageView", "path": "file:///w/a.png"},
    "ImageGeneration": {"type": "ImageGeneration", "status": "completed", "result": "x"},
    "FileChange": {"type": "FileChange", "changes": {}, "status": "completed"},
    "McpToolCall": {
        "type": "McpToolCall",
        "server": "s",
        "tool": "t",
        "arguments": {},
        "status": "completed",
    },
}
_EXTENSION_ITEMS = {
    "image_gen.generation": {"status": "completed", "result": "x", "revisedPrompt": "a cat"},
    "clock.sleep": {"durationMs": 10},
    "web.search": {"query": "q", "action": {"type": "search", "query": "q"}},
}
#: Items that are not a call, and why each is not read as one.
_NOT_CALLS = {
    # Read as the session's initial prompt.
    "UserMessage",
    # An output the client supplied as turn input (`TurnInput::FunctionCallOutput`);
    # the `function_call_output` response item beside it is the one read.
    "FunctionCallOutput",
    # Text a hook added, an agent's message, a plan, reasoning, a sub-agent's
    # lifecycle, review mode and a compaction marker: none is an invocation.
    "HookPrompt",
    "AgentMessage",
    "Plan",
    "Reasoning",
    "SubAgentActivity",
    "EnteredReviewMode",
    "ExitedReviewMode",
    "ContextCompaction",
}


def _session(rollout: Rollout, home: Path) -> tuple[Session, list[str]]:
    rollout.complete()
    rollout.write(home)
    sessions, failures = CodexReader(home).collect(_ALL)
    assert len(sessions) == 1, failures
    return sessions[0], [failure.message for failure in failures]


def _calls(session: Session) -> dict[str, ToolCall]:
    return {call.provider_call_id or "": call for turn in session.turns for call in turn.tool_calls}


def test_every_turn_item_is_a_call_or_deliberately_not_one() -> None:
    assert set(_CALL_ITEMS) | {"Extension"} | _NOT_CALLS == _TURN_ITEMS
    assert not set(_CALL_ITEMS) & _NOT_CALLS


@pytest.mark.parametrize("kind", sorted(_CALL_ITEMS))
def test_every_call_item_is_a_classed_call_with_a_status(kind: str, tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.item({**_CALL_ITEMS[kind], "id": "c1"})
    session, failures = _session(rollout, tmp_path)

    call = _calls(session)["c1"]

    assert failures == []
    if kind == "DynamicToolCall":
        # A tool the app-server client defined, not Codex's: unclassed, never
        # a guessed class (ADR-0021).
        assert call.tool_class is None
    else:
        assert call.tool_class in TOOL_CLASSES, call.tool_name
    assert call.status in ("ok", "error", "rejected", "interrupted"), call.status


def test_every_extension_kind_is_read() -> None:
    assert set(_EXTENSION_ITEMS) == _EXTENSION_KINDS


@pytest.mark.parametrize("kind", sorted(_EXTENSION_KINDS))
def test_every_extension_item_is_a_classed_call_with_a_status(kind: str, tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.item({"type": "Extension", "kind": kind, "id": "c1", **_EXTENSION_ITEMS[kind]})
    session, failures = _session(rollout, tmp_path)

    call = _calls(session)["c1"]

    assert failures == []
    assert call.tool_class in TOOL_CLASSES, call.tool_name
    assert call.status in ("ok", "error"), call.status


@pytest.mark.parametrize("kind", sorted(_NOT_CALLS))
def test_an_item_that_is_not_a_call_adds_no_turn_and_no_warning(kind: str, tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.item({"type": kind, "id": "x1", "content": []})
    session, failures = _session(rollout, tmp_path)

    assert failures == []
    assert _calls(session) == {}


def test_an_item_codex_added_since_0_163_is_reported_not_dropped(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.item({"type": "BrandNewCall", "id": "n1"})
    rollout.item({"type": "Extension", "kind": "clock.alarm", "id": "n2"})
    _, failures = _session(rollout, tmp_path)

    assert any("'BrandNewCall'" in failure for failure in failures), failures
    assert any("'clock.alarm'" in failure for failure in failures), failures


@pytest.mark.parametrize("tool", sorted(_COLLAB_TOOLS))
@pytest.mark.parametrize("namespace", ["multi_agent_v1", "collaboration", "team"])
def test_a_collaboration_call_is_named_by_its_item_in_any_namespace(
    tool: str, namespace: str, tmp_path: Path
) -> None:
    """V1 puts its tools under `multi_agent_v1`; V2 under `collaboration` by
    default, or any namespace configured (`multi_agent_v2.tool_namespace`).
    The typed item names the built-in either way."""
    name = "wait_agent" if tool == "wait" else tool
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.call("c1", name, {"target": "x"}, namespace=namespace)
    rollout.item(
        {
            "type": "CollabAgentToolCall",
            "id": "c1",
            "tool": tool,
            "status": "completed",
            "sender_thread_id": "t",
        }
    )
    session, failures = _session(rollout, tmp_path)

    call = _calls(session)["c1"]

    assert failures == []
    assert (call.mcp_server, call.tool_name) == (None, name)
    assert tool_class("codex", call.tool_name, None) in TOOL_CLASSES - {"mcp"}


def test_a_standalone_image_generation_is_named_by_its_extension_item(tmp_path: Path) -> None:
    """Codex calls it `image_gen.imagegen`, a function call, and completes it
    with an `image_gen.generation` extension item of the same id
    (`ext/image-generation/src/tool.rs`)."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.call("ig1", "imagegen", {"prompt": "a cat"}, namespace="image_gen")
    rollout.item(
        {
            "type": "Extension",
            "kind": "image_gen.generation",
            "id": "ig1",
            "status": "completed",
            "revisedPrompt": "a cat in a hat",
            "result": "<base64>",
        }
    )
    session, failures = _session(rollout, tmp_path)

    call = _calls(session)["ig1"]

    assert failures == []
    assert (call.mcp_server, call.tool_name, call.tool_class) == (
        None,
        "image_generation",
        "other",
    )
    assert (call.status, call.result) == ("ok", "<base64>")


def test_a_failed_standalone_image_generation_is_an_error(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.item(
        {
            "type": "Extension",
            "kind": "image_gen.generation",
            "id": "ig1",
            "status": "failed",
            "result": "",
            "failure": {"type": "usageLimitExceeded", "limitId": "image_gen", "resetsAt": None},
        }
    )
    session, _ = _session(rollout, tmp_path)

    assert _calls(session)["ig1"].status == "error"


def test_a_hosted_image_generation_item_joins_its_call(tmp_path: Path) -> None:
    """`event_mapping.rs` gives the `ImageGeneration` item the id of its
    `image_generation_call`, so the two are one call."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.response(
        {"type": "image_generation_call", "id": "ig1", "status": "completed", "result": "x"}
    )
    rollout.item({"type": "ImageGeneration", "id": "ig1", "status": "completed", "result": "x"})
    session, failures = _session(rollout, tmp_path)

    assert failures == []
    assert list(_calls(session)) == ["ig1"]


@pytest.mark.parametrize("results", [[], [{"title": "a"}]])
@pytest.mark.parametrize("kind", ["WebSearch", "Extension"])
def test_a_web_searchs_results_are_its_result_even_when_empty(
    kind: str, results: list[object], tmp_path: Path
) -> None:
    """`results: []` is a search that found nothing, not one that recorded
    nothing; Codex's own tests write it (`exec/tests/event_processor_with_json_output.rs`)."""
    item: dict[str, object] = {
        "type": kind,
        "id": "w1",
        "query": "q",
        "action": {"type": "search"},
        "results": results,
    }
    if kind == "Extension":
        item["kind"] = "web.search"
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.item(item)
    session, _ = _session(rollout, tmp_path)

    call = _calls(session)["w1"]

    assert call.result == json.dumps(results)
    assert call.result_size == len(json.dumps(results))


def test_a_web_search_with_no_results_recorded_has_no_result(tmp_path: Path) -> None:
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.item({"type": "WebSearch", "id": "w1", "query": "q", "action": {"type": "search"}})
    session, _ = _session(rollout, tmp_path)

    assert _calls(session)["w1"].result is None


#: The field each call item records its result in, and an empty value of that
#: field's type (`protocol/src/items.rs`, `ext/items/src/*.rs`).
_RESULT_FIELDS: dict[str, tuple[str, object]] = {
    "CommandExecution": ("aggregated_output", ""),
    "FileChange": ("stdout", ""),
    "McpToolCall": ("result", {"content": []}),
    "DynamicToolCall": ("content_items", []),
    "WebSearch": ("results", []),
    "ImageGeneration": ("result", ""),
    "image_gen.generation": ("result", ""),
    "web.search": ("results", []),
}
#: Items that record no result.
_NO_RESULT = {"CollabAgentToolCall", "ImageView", "clock.sleep"}


def _item_of(kind: str) -> dict[str, object]:
    if kind in _EXTENSION_KINDS:
        return {"type": "Extension", "kind": kind, **_EXTENSION_ITEMS[kind]}
    return dict(_CALL_ITEMS[kind])


def test_every_call_item_says_where_its_result_is() -> None:
    assert set(_RESULT_FIELDS) | _NO_RESULT == set(_CALL_ITEMS) | _EXTENSION_KINDS


@pytest.mark.parametrize("kind", sorted(_RESULT_FIELDS))
def test_an_empty_recorded_result_is_a_result_and_an_absent_one_is_none(
    kind: str, tmp_path: Path
) -> None:
    """Present but empty is what the call returned; absent is nothing recorded.
    Each item's result is read from its own field, never by trying fields in
    turn, where an empty one falls through to the next (ADR-0034)."""
    field, empty = _RESULT_FIELDS[kind]
    recorded = {**_item_of(kind), "id": "full", field: empty}
    absent = {key: value for key, value in _item_of(kind).items() if key != field}
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.item(recorded)
    rollout.item({**absent, "id": "none"})
    session, _ = _session(rollout, tmp_path)

    calls = _calls(session)

    assert calls["full"].result is not None
    assert calls["full"].result_size == len(calls["full"].result)
    if kind not in ("ImageGeneration", "image_gen.generation"):
        # Codex always writes an image's `result`; the others are optional.
        assert calls["none"].result is None


#: Every built-in name the reader can emit for a call: each 0.163 tool the
#: model is offered, each name an item implies, and each older build's name.
#: All are the agent's own, so each must be classed.
_EMITTED_BUILTINS = {
    "exec_command",
    "shell",
    "shell_command",
    "local_shell",
    "write_stdin",
    "view_image",
    "apply_patch",
    "web_search",
    "tool_search",
    "update_plan",
    "request_user_input",
    "list_mcp_resources",
    "list_mcp_resource_templates",
    "read_mcp_resource",
    "get_goal",
    "create_goal",
    "update_goal",
    "image_generation",
    "sleep",
    *("wait_agent" if tool == "wait" else tool for tool in _COLLAB_TOOLS),
}


@pytest.mark.parametrize("name", sorted(_EMITTED_BUILTINS))
def test_every_built_in_the_reader_emits_is_classed(name: str) -> None:
    assert tool_class("codex", name, None) in TOOL_CLASSES - {"mcp"}


def _nested(depth: int = 100_000) -> str:
    return "[" * depth + "]" * depth


def test_an_arguments_string_nested_past_the_decoders_limit_is_raw_not_fatal(
    tmp_path: Path,
) -> None:
    """A call's `arguments` is JSON text inside the record; nested too deep to
    decode, it is kept as `{"raw": …}` like any other undecodable arguments,
    and the session is read."""
    rollout = Rollout("t")
    rollout.turn("t1")
    rollout.response(
        {"type": "function_call", "name": "exec_command", "arguments": _nested(), "call_id": "c1"}
    )
    session, _ = _session(rollout, tmp_path)

    assert set(_calls(session)["c1"].arguments) == {"raw"}


# --- the input boundary: every decode site, every hostile input -------------------

#: Every place the Codex readers decode bytes they did not write, by
#: `(module, function, call)`. A new site fails `test_every_decode_site_is_known`
#: until it is added here with an input it must survive below.
_DECODE_SITES = {
    ("src/openaidr/readers/codex.py", "read", "json.loads"),
    ("src/openaidr/readers/codex.py", "_arguments", "json.loads"),
    ("src/openaidr/readers/codex.py", "_titles", "json.loads"),
    ("src/openaidr/readers/codex.py", "_instant", "datetime.fromisoformat"),
    ("src/openaidr/readers/codex.py", "collect", "datetime.fromtimestamp"),
    ("src/openaidr/readers/codex_log.py", "_line", "datetime.fromtimestamp"),
    ("src/openaidr/accounts.py", "_codex_record", "tomllib.loads"),
    ("src/openaidr/accounts.py", "_json", "json.loads"),
    ("src/openaidr/accounts.py", "_token_claims", "base64.b64decode"),
    ("src/openaidr/accounts.py", "_token_claims", "json.loads"),
}
_DECODERS = {"loads", "fromisoformat", "fromtimestamp", "b64decode"}


def test_every_decode_site_is_known() -> None:
    import ast

    found = set()
    for module in {site[0] for site in _DECODE_SITES}:
        tree = ast.parse(Path(module).read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef):
                continue
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _DECODERS
                ):
                    found.add((module, function.name, ast.unparse(node.func)))
    assert found == _DECODE_SITES


#: Text nothing should have written, each a way decoding it could raise.
_HOSTILE_TEXT = {
    "deep-array": "[" * 100_000 + "]" * 100_000,
    "deep-object": '{"a":' * 50_000 + "1" + "}" * 50_000,
    "not-json": "SECRET-TOKEN{",
}
#: Timestamps that parse but cannot be an instant in UTC, or cannot parse.
_HOSTILE_INSTANTS = ["0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59", "not-a-time"]


@pytest.mark.parametrize("text", list(_HOSTILE_TEXT.values()), ids=list(_HOSTILE_TEXT))
@pytest.mark.parametrize("where", ["record", "arguments", "title"])
def test_hostile_rollout_text_never_raises_or_echoes(where: str, text: str, tmp_path: Path) -> None:
    """A malformed record withholds its session; malformed arguments are kept
    raw; a malformed title line is skipped. None raises, and none is echoed."""
    rollout = Rollout("t")
    rollout.turn("t1")
    if where == "arguments":
        rollout.response(
            {"type": "function_call", "name": "exec_command", "arguments": text, "call_id": "c1"}
        )
    path = rollout.write(tmp_path)
    if where == "record":
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f'{{"type": "response_item", "payload": {text}}}\n')
    if where == "title":
        (tmp_path / "session_index.jsonl").write_text(text + "\n", encoding="utf-8")

    sessions, failures = CodexReader(tmp_path).collect(_ALL)

    assert all("SECRET-TOKEN" not in failure.message for failure in failures)
    if where == "record":
        assert sessions == [] and len(failures) == 1
    else:
        assert len(sessions) == 1 and failures == []
    if where == "arguments":
        assert _calls(sessions[0])["c1"].arguments == {"raw": text}


@pytest.mark.parametrize("instant", _HOSTILE_INSTANTS)
@pytest.mark.parametrize("where", ["record", "session_meta"])
def test_a_hostile_timestamp_is_no_instant_not_a_failure(
    where: str, instant: str, tmp_path: Path
) -> None:
    meta: dict[str, object] | None = {"timestamp": instant} if where == "session_meta" else None
    rollout = Rollout("t", meta=meta)
    rollout.turn("t1")
    rollout.user("hi")
    if where == "record":
        rollout.records[-2]["timestamp"] = instant
    rollout.write(tmp_path)

    sessions, failures = CodexReader(tmp_path).collect(_ALL)

    assert len(sessions) == 1 and failures == []


def test_a_record_too_deep_to_walk_again_withholds_only_its_own_session(
    tmp_path: Path,
) -> None:
    """A deep object decodes without `RecursionError`, but serialising it again
    raises outside any reader's catch, losing every session in the pass. The
    record is withheld at the bound instead (ADR-0033)."""
    good = Rollout("good")
    good.turn("t1")
    good.user("hi")
    good.complete()
    good.write(tmp_path)
    bad = Rollout("bad")
    bad.turn("t1")
    bad.item(
        {
            "type": "McpToolCall",
            "id": "m1",
            "server": "s",
            "tool": "t",
            "arguments": {},
            "status": "completed",
            "result": {"content": "DEEP"},
        }
    )
    bad.complete()
    path = bad.write(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace('"DEEP"', _HOSTILE_TEXT["deep-object"]),
        encoding="utf-8",
    )

    sessions, failures = CodexReader(tmp_path).collect(_ALL)

    assert [session.session_id for session in sessions] == ["codex:good"]
    assert len(failures) == 1 and "malformed rollout record" in failures[0].message
