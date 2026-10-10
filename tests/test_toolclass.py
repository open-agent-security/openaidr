"""The per-kind tool tables: a closed vocabulary, and nothing guessed."""

from __future__ import annotations

import pytest

from openaidr.toolclass import (
    _ARGUMENT_KEYS,
    _CLASS_BY_NAME,
    CANONICAL_KEYS,
    TOOL_CLASSES,
    canonical_arguments,
    tool_class,
)


@pytest.mark.parametrize("kind", sorted(_CLASS_BY_NAME))
def test_every_class_a_table_assigns_is_in_the_vocabulary(kind: str) -> None:
    assert set(_CLASS_BY_NAME[kind].values()) <= TOOL_CLASSES - {"mcp"}


@pytest.mark.parametrize("kind", sorted(_ARGUMENT_KEYS))
def test_every_canonical_key_is_in_the_vocabulary_and_its_tool_is_classed(kind: str) -> None:
    for tool, pairs in _ARGUMENT_KEYS[kind].items():
        assert tool in _CLASS_BY_NAME[kind], tool
        assert {canonical for canonical, _ in pairs} <= CANONICAL_KEYS, tool


def test_both_kinds_class_their_shell_alike() -> None:
    assert (
        tool_class("claude-code", "Bash", None) == tool_class("devin-cli", "exec", None) == "shell"
    )
    assert canonical_arguments("claude-code", "Bash", None, {"command": "ls"}) == {"command": "ls"}
    assert canonical_arguments("devin-cli", "exec", None, {"command": "ls"}) == {"command": "ls"}


def test_a_file_write_carries_path_and_content_for_both_kinds() -> None:
    claude = canonical_arguments("claude-code", "Write", None, {"file_path": "/a", "content": "x"})
    devin = canonical_arguments("devin-cli", "write", None, {"file_path": "/a", "content": "x"})
    assert claude == devin == {"path": "/a", "content": "x"}


def test_any_call_with_a_server_is_mcp_and_has_no_canonical_arguments() -> None:
    assert tool_class("claude-code", "Bash", "srv") == "mcp"
    assert canonical_arguments("claude-code", "Bash", "srv", {"command": "ls"}) == {}


def test_an_unknown_name_or_kind_is_unclassed_never_other() -> None:
    assert tool_class("claude-code", "BrandNewTool", None) is None
    assert tool_class("devin-cli", "Bash", None) is None
    assert tool_class(None, "Bash", None) is None


def test_a_key_the_call_did_not_record_is_absent() -> None:
    assert canonical_arguments("devin-cli", "edit", None, {"file_path": "/a"}) == {"path": "/a"}


#: Claude Code built-ins seen in real transcripts after the table was first
#: written. Each must be classed: unclassed, a consumer reads the agent's own
#: call as a tool it cannot place.
_CLAUDE_BUILTINS_SEEN_LATER = (
    "Monitor",
    "TaskOutput",
    "TaskStop",
    "TaskCreate",
    "ToolSearch",
    "EnterPlanMode",
    "SendMessage",
    "ListAgents",
    "SubagentHandback",
    "Artifact",
    "ReportFindings",
    "SendUserFile",
)


@pytest.mark.parametrize("name", _CLAUDE_BUILTINS_SEEN_LATER)
def test_claude_code_built_ins_seen_in_transcripts_are_classed(name: str) -> None:
    assert tool_class("claude-code", name, None) in TOOL_CLASSES - {"mcp"}


def test_claude_codes_monitor_is_a_shell_carrying_its_command() -> None:
    assert tool_class("claude-code", "Monitor", None) == "shell"
    arguments = {"command": "tail -f app.log", "description": "errors", "timeout_ms": 1000}
    assert canonical_arguments("claude-code", "Monitor", None, arguments) == {
        "command": "tail -f app.log"
    }
