"""The per-kind tool tables: a closed vocabulary, and nothing guessed."""

from __future__ import annotations

import pytest

from openaidr.toolclass import (
    _ARGUMENT_KEYS,
    _CLASS_BY_NAME,
    ACP_KIND_CLASSES,
    CANONICAL_KEYS,
    TOOL_CLASSES,
    canonical_arguments,
    class_of_acp_kind,
    tool_class,
)

#: Every Agent Client Protocol `ToolKind` (agentclientprotocol/agent-client-protocol,
#: `schema/v1/schema.json`), its definition there, and the class it may carry. A
#: kind carries a capability only where its definition names one thing: "files
#: or data" claims nothing narrower than `other`.
_ACP_KINDS = {
    "read": ("Reading files or data.", "other"),
    "edit": ("Modifying files or content.", "other"),
    "delete": ("Removing files or data.", "other"),
    "move": ("Moving or renaming files.", "file_write"),
    "search": ("Searching for information.", "other"),
    "execute": ("Running commands or code.", "shell"),
    "think": ("Internal reasoning or planning.", "other"),
    "fetch": ("Retrieving external data.", "web_fetch"),
    "switch_mode": ("Switching the current session mode.", "other"),
    "other": ("Other tool types (default).", "other"),
}


@pytest.mark.parametrize("kind", sorted(_CLASS_BY_NAME))
def test_every_class_a_table_assigns_is_in_the_vocabulary(kind: str) -> None:
    assert set(_CLASS_BY_NAME[kind].values()) <= TOOL_CLASSES - {"mcp"}


@pytest.mark.parametrize("kind", sorted(_ARGUMENT_KEYS))
def test_every_canonical_key_is_in_the_vocabulary_and_its_tool_is_classed(kind: str) -> None:
    for tool, pairs in _ARGUMENT_KEYS[kind].items():
        assert tool in _CLASS_BY_NAME[kind], tool
        assert {canonical for canonical, _ in pairs} <= CANONICAL_KEYS, tool


def test_both_kinds_class_their_shell_alike() -> None:
    assert tool_class("claude-code", "Bash", None) == tool_class("devin", "exec", None) == "shell"
    assert canonical_arguments("claude-code", "Bash", None, {"command": "ls"}) == {"command": "ls"}
    assert canonical_arguments("devin", "exec", None, {"command": "ls"}) == {"command": "ls"}


def test_a_file_write_carries_path_and_content_for_both_kinds() -> None:
    claude = canonical_arguments("claude-code", "Write", None, {"file_path": "/a", "content": "x"})
    devin = canonical_arguments("devin", "write", None, {"file_path": "/a", "content": "x"})
    assert claude == devin == {"path": "/a", "content": "x"}


def test_any_call_with_a_server_is_mcp_and_has_no_canonical_arguments() -> None:
    assert tool_class("claude-code", "Bash", "srv") == "mcp"
    assert canonical_arguments("claude-code", "Bash", "srv", {"command": "ls"}) == {}


def test_an_unknown_name_or_kind_is_unclassed_never_other() -> None:
    assert tool_class("claude-code", "BrandNewTool", None) is None
    assert tool_class("devin", "Bash", None) is None
    assert tool_class(None, "Bash", None) is None


def test_a_key_the_call_did_not_record_is_absent() -> None:
    assert canonical_arguments("devin", "edit", None, {"file_path": "/a"}) == {"path": "/a"}


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


def test_every_acp_tool_kind_has_a_class_in_the_vocabulary() -> None:
    assert set(ACP_KIND_CLASSES) == set(_ACP_KINDS)
    assert set(ACP_KIND_CLASSES.values()) <= TOOL_CLASSES - {"mcp"}


@pytest.mark.parametrize("kind", sorted(_ACP_KINDS))
def test_each_acp_kind_claims_only_what_its_definition_names(kind: str) -> None:
    definition, expected = _ACP_KINDS[kind]
    assert class_of_acp_kind(kind) == expected, definition


def test_anything_acp_does_not_define_has_no_class() -> None:
    assert class_of_acp_kind("browse") is None
    assert class_of_acp_kind(None) is None
    assert class_of_acp_kind(3) is None


@pytest.mark.parametrize("name", ["find_file_by_name", "code_search"])
def test_devin_file_searches_seen_in_sessions_are_classed(name: str) -> None:
    assert tool_class("devin", name, None) == "file_search"
