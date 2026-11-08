"""The per-kind tool tables: a closed vocabulary, and nothing guessed."""

from __future__ import annotations

import pytest

from openaidr.toolclass import (
    _ARGUMENT_KEYS,
    _CLASS_BY_NAME,
    ACP_TOOL_KINDS,
    CANONICAL_KEYS,
    TOOL_CLASSES,
    canonical_arguments,
    class_of_acp_kind,
    tool_class,
)

#: Every Agent Client Protocol `ToolKind` (agentclientprotocol/agent-client-protocol,
#: `schema/v1/schema.json`). The schema says kinds exist so clients can "choose
#: appropriate icons": a display hint, not a statement of what a tool touches.
_ACP_KINDS = {
    "read",
    "edit",
    "delete",
    "move",
    "search",
    "execute",
    "think",
    "fetch",
    "switch_mode",
    "other",
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


def test_the_fallback_knows_every_acp_tool_kind() -> None:
    assert ACP_TOOL_KINDS == _ACP_KINDS


@pytest.mark.parametrize("kind", sorted(_ACP_KINDS))
def test_an_acp_kind_says_the_call_is_the_agents_own_and_claims_no_capability(
    kind: str,
) -> None:
    """A capability comes only from the name table (ADR-0025)."""
    assert class_of_acp_kind(kind) == "other"


def test_anything_acp_does_not_define_has_no_class() -> None:
    assert class_of_acp_kind("browse") is None
    assert class_of_acp_kind(None) is None
    assert class_of_acp_kind(3) is None


@pytest.mark.parametrize("name", ["find_file_by_name", "code_search"])
def test_devin_file_searches_seen_in_sessions_are_classed(name: str) -> None:
    assert tool_class("devin", name, None) == "file_search"


def test_codex_names_a_shell_by_the_script_never_its_wrapper() -> None:
    for argv in (["bash", "-lc", "curl -s https://x"], ["/bin/zsh", "-c", "curl -s https://x"]):
        assert canonical_arguments("codex", "shell", None, {"command": argv}) == {
            "command": "curl -s https://x"
        }
    assert canonical_arguments("codex", "shell", None, {"command": ["git", "status", "-s"]}) == {
        "command": "git status -s"
    }
    assert canonical_arguments("codex", "exec_command", None, {"cmd": "rm -rf x"}) == {
        "command": "rm -rf x"
    }


def test_codex_patch_edits_land_where_the_patch_puts_them() -> None:
    patch = "*** Begin Patch\n*** Update File: a.py\n*** Move to: b.py\n@@\n-x\n+y\n*** End Patch\n"
    assert canonical_arguments("codex", "apply_patch", None, {"input": patch}) == {
        "edits": [{"path": "b.py", "kind": "update", "old_text": "x", "new_text": "y"}],
        "path": "b.py",
        "new_text": "y",
    }


def test_codex_patch_edits_read_the_changes_codex_recorded() -> None:
    changes = {
        "/w/.env": {"type": "add", "content": "KEY=1\n"},
        "/w/app.py": {"type": "update", "unified_diff": "--- a\n+++ b\n@@\n-x\n+y\n"},
    }
    assert canonical_arguments("codex", "apply_patch", None, {"changes": changes})["edits"] == [
        {"path": "/w/.env", "kind": "add", "old_text": "", "new_text": "KEY=1\n"},
        {"path": "/w/app.py", "kind": "update", "old_text": "x", "new_text": "y"},
    ]


def test_an_added_line_starting_with_plus_plus_is_not_mistaken_for_the_header() -> None:
    """Only the diff's own "--- a" / "+++ b" header, by position, is excluded;
    an added line that happens to start with "++" (e.g. "++counter;") encodes
    as a diff line starting with "+++" too, but it is content, not a second
    header (P2 Codex review finding on PR #76)."""
    changes = {
        "/w/app.py": {
            "type": "update",
            "unified_diff": "--- a\n+++ b\n@@\n-counter;\n+++counter;\n",
        }
    }
    assert canonical_arguments("codex", "apply_patch", None, {"changes": changes})["edits"] == [
        {"path": "/w/app.py", "kind": "update", "old_text": "counter;", "new_text": "++counter;"},
    ]


# --- canonical edits: what each file edit removed and added ------------------------

#: One recorded argument shape per `file_edit` tool, from the source each table
#: row cites. A tool absent here must be in `_NO_EDITS`, with its reason.
_EDIT_SAMPLES: dict[tuple[str, str], dict[str, object]] = {
    ("claude-code", "Edit"): {"file_path": "/a", "old_string": "o", "new_string": "n"},
    (
        "claude-code",
        "MultiEdit",
    ): {"file_path": "/a", "edits": [{"old_string": "o", "new_string": "n"}]},
    ("claude-code", "NotebookEdit"): {"notebook_path": "/n.ipynb", "new_source": "x = 1"},
    ("codex", "apply_patch"): {"input": "*** Begin Patch\n*** Delete File: a\n*** End Patch\n"},
    ("cursor", "StrReplace"): {"path": "/a", "old_string": "o", "new_string": "n"},
    ("cursor", "ApplyPatch"): {"patch": "*** Begin Patch\n*** Delete File: a\n*** End Patch\n"},
    ("devin", "edit"): {"file_path": "/a", "old_string": "o", "new_string": "n"},
}

#: File edit tools whose recorded arguments are not known well enough to fill
#: `edits` without guessing. Each is stated in its kind's spec.
_NO_EDITS = {
    # Its path and new text keys are in the table; its removed-text key and
    # whether it can create a notebook are not confirmed.
    ("cursor", "EditNotebook"),
    # Its argument keys are unconfirmed (devin-cli.md, M3).
    ("devin", "apply_patch"),
    # Only its path key is known.
    ("devin", "notebook_edit"),
}


def test_every_file_edit_tool_gives_edits_or_is_a_stated_gap() -> None:
    """Asked of every `file_edit` row, not the tools that prompted it, so a file
    edit tool added later cannot silently arrive without `edits`."""
    edit_tools = {
        (kind, name)
        for kind, table in _CLASS_BY_NAME.items()
        for name, cls in table.items()
        if cls == "file_edit"
    }
    assert edit_tools == set(_EDIT_SAMPLES) | _NO_EDITS
    for (kind, name), arguments in _EDIT_SAMPLES.items():
        edits = canonical_arguments(kind, name, None, arguments).get("edits")
        assert isinstance(edits, list) and edits, (kind, name)
        for edit in edits:
            assert {"path", "kind"} <= set(edit) <= {"path", "kind", "old_text", "new_text"}
            assert edit["kind"] in ("add", "update", "delete")


def test_a_claude_code_edit_carries_the_text_it_removed_and_added() -> None:
    found = canonical_arguments(
        "claude-code",
        "Edit",
        None,
        {"file_path": "/a", "old_string": "o", "new_string": "n", "replace_all": False},
    )
    assert found == {
        "path": "/a",
        "new_text": "n",
        "edits": [{"path": "/a", "kind": "update", "old_text": "o", "new_text": "n"}],
    }


def test_an_empty_old_string_is_the_edit_that_creates_the_file() -> None:
    """Claude Code itself counts an `Edit` with an empty `old_string` as a new
    file (`isNewFile`), and its `MultiEdit` names one "Create"."""
    found = canonical_arguments(
        "claude-code", "Edit", None, {"file_path": "/a", "old_string": "", "new_string": "n"}
    )
    assert found["edits"] == [{"path": "/a", "kind": "add", "old_text": "", "new_text": "n"}]


def test_a_multi_edit_gives_one_entry_per_hunk_and_its_added_text_joined() -> None:
    found = canonical_arguments(
        "claude-code",
        "MultiEdit",
        None,
        {
            "file_path": "/a",
            "edits": [
                {"old_string": "a", "new_string": "b"},
                {"old_string": "c", "new_string": "d", "replace_all": True},
            ],
        },
    )
    assert found == {
        "path": "/a",
        "new_text": "b\nd",
        "edits": [
            {"path": "/a", "kind": "update", "old_text": "a", "new_text": "b"},
            {"path": "/a", "kind": "update", "old_text": "c", "new_text": "d"},
        ],
    }


def test_a_notebook_edit_records_no_removed_text() -> None:
    """`NotebookEdit`'s input has no old source; only its result does, and
    canonical arguments read the call's arguments alone. A cell deletion adds
    nothing: Claude Code ignores `new_source` for it."""
    replace = canonical_arguments(
        "claude-code", "NotebookEdit", None, {"notebook_path": "/n.ipynb", "new_source": "x = 1"}
    )
    assert replace["edits"] == [{"path": "/n.ipynb", "kind": "update", "new_text": "x = 1"}]
    delete = canonical_arguments(
        "claude-code",
        "NotebookEdit",
        None,
        {"notebook_path": "/n.ipynb", "new_source": "x = 1", "edit_mode": "delete"},
    )
    assert delete["edits"] == [{"path": "/n.ipynb", "kind": "update", "new_text": ""}]


def test_string_replacements_read_alike_across_kinds() -> None:
    arguments = {"old_string": "o", "new_string": "n"}
    claude = canonical_arguments("claude-code", "Edit", None, {"file_path": "/a", **arguments})
    cursor = canonical_arguments("cursor", "StrReplace", None, {"path": "/a", **arguments})
    devin = canonical_arguments("devin", "edit", None, {"file_path": "/a", **arguments})
    assert claude["edits"] == cursor["edits"] == devin["edits"]


def test_a_removed_text_the_call_did_not_record_is_absent() -> None:
    found = canonical_arguments("cursor", "StrReplace", None, {"path": "/a", "new_string": "n"})
    assert found["edits"] == [{"path": "/a", "kind": "update", "new_text": "n"}]


def test_a_patch_gives_removed_lines_and_says_nothing_it_does_not_hold() -> None:
    """An added file removed nothing. A deleted file's section holds no lines
    in the patch grammar, so what it removed is not in the call and is absent.
    Context lines are neither removed nor added."""
    patch = (
        "*** Begin Patch\n"
        "*** Add File: new.py\n+a\n"
        "*** Update File: app.py\n@@ def f():\n keep\n-old one\n-old two\n+new\n"
        "*** Delete File: gone.py\n"
        "*** End Patch\n"
    )
    assert canonical_arguments("codex", "apply_patch", None, {"input": patch})["edits"] == [
        {"path": "new.py", "kind": "add", "old_text": "", "new_text": "a"},
        {"path": "app.py", "kind": "update", "old_text": "old one\nold two", "new_text": "new"},
        {"path": "gone.py", "kind": "delete", "new_text": ""},
    ]
    assert (
        canonical_arguments("cursor", "ApplyPatch", None, {"patch": patch})["edits"]
        == (canonical_arguments("codex", "apply_patch", None, {"input": patch})["edits"])
    )


def test_a_deletion_codex_recorded_carries_the_file_it_removed() -> None:
    """Codex's applied change for a deleted file holds its content
    (`FileChange::Delete { content }`), so there the removed text is known."""
    changes = {"/w/gone.py": {"type": "delete", "content": "print(1)\n"}}
    assert canonical_arguments("codex", "apply_patch", None, {"changes": changes})["edits"] == [
        {"path": "/w/gone.py", "kind": "delete", "old_text": "print(1)\n", "new_text": ""},
    ]


def test_a_removed_line_starting_with_minus_minus_is_not_mistaken_for_the_header() -> None:
    changes = {
        "/w/app.py": {"type": "update", "unified_diff": "--- a\n+++ b\n@@\n---x;\n+x;\n"},
    }
    assert canonical_arguments("codex", "apply_patch", None, {"changes": changes})["edits"] == [
        {"path": "/w/app.py", "kind": "update", "old_text": "--x;", "new_text": "x;"},
    ]
