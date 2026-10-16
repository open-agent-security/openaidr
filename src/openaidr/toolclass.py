"""What a tool *is*, and its arguments under one key name — per kind, from tables.

Every kind names its tools its own way: Claude Code's shell is `Bash`, Devin
CLI's is `exec`. A consumer asking *did this session run a command* should not
have to learn every kind's vocabulary, and holding that knowledge is what this
package is for (ADR-0021). The tables below come first. A name a table does not
list is `other` where its agent declared an Agent Client Protocol kind for the
call (`class_of_acp_kind`, ADR-0025): known to be the agent's own, with no
capability claimed. Otherwise it gets no class, never a guessed one. A key a kind does not
record is absent from the canonical arguments, never filled from a neighbour.

`tool_name` and `arguments` are left exactly as recorded beside both.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Mapping

#: The closed vocabulary. `mcp` is any call with an MCP server; `other` is a
#: built-in the table knows and that fits no class above it.
TOOL_CLASSES = frozenset(
    {
        "shell",
        "shell_control",
        "file_read",
        "file_search",
        "file_write",
        "file_edit",
        "web_fetch",
        "web_search",
        "delegate",
        "skill",
        "mcp",
        "other",
    }
)

#: The canonical argument keys, by the class that uses them.
CANONICAL_KEYS = frozenset(
    {"command", "path", "content", "new_text", "url", "agent", "skill", "edits"}
)

_CLASS_BY_NAME: dict[str, dict[str, str]] = {
    "claude-code": {
        "Bash": "shell",
        "BashOutput": "shell_control",
        "KillShell": "shell_control",
        "KillBash": "shell_control",
        "Read": "file_read",
        "NotebookRead": "file_read",
        "Grep": "file_search",
        "Glob": "file_search",
        "LS": "file_search",
        "Write": "file_write",
        "Edit": "file_edit",
        "MultiEdit": "file_edit",
        "NotebookEdit": "file_edit",
        "WebFetch": "web_fetch",
        "WebSearch": "web_search",
        "Agent": "delegate",
        "Task": "delegate",
        "Skill": "skill",
        "TodoWrite": "other",
        "ExitPlanMode": "other",
        "AskUserQuestion": "other",
        "SlashCommand": "other",
        "ListMcpResourcesTool": "other",
        "ReadMcpResourceTool": "other",
        # Each seen in a real transcript. `Monitor` runs its `command` as a
        # shell script and streams the output. `TaskOutput` and `TaskStop`
        # reach a background agent as well as a shell, so they are not
        # `shell_control`.
        "Monitor": "shell",
        "TaskOutput": "other",
        "TaskStop": "other",
        "TaskCreate": "other",
        "ToolSearch": "other",
        "EnterPlanMode": "other",
        "SendMessage": "other",
        "ListAgents": "other",
        "SubagentHandback": "other",
        "Artifact": "other",
        "ReportFindings": "other",
        "SendUserFile": "other",
    },
    # Devin CLI 3000.11.3's built-in tool names, as its binary declares them.
    "devin": {
        "exec": "shell",
        "get_output": "shell_control",
        "write_to_process": "shell_control",
        "kill_shell": "shell_control",
        "read": "file_read",
        "notebook_read": "file_read",
        "grep": "file_search",
        "glob": "file_search",
        # Not in the 3000.11.3 schemas this table was first read from, and
        # called in real sessions on the `swe-1-6` models. The binary's own
        # hook matcher groups `find_file_by_name` with `read` and `grep`, and
        # Devin declares both as ACP `search`.
        "find_file_by_name": "file_search",
        "code_search": "file_search",
        "web_search": "web_search",
        "write": "file_write",
        "edit": "file_edit",
        "apply_patch": "file_edit",
        "notebook_edit": "file_edit",
        "webfetch": "web_fetch",
        "run_subagent": "delegate",
        "skill": "skill",
        "read_subagent": "other",
        "todo_write": "other",
        "exit_plan_mode": "other",
        "request_scope": "other",
        "mcp_list_servers": "other",
        "mcp_list_tools": "other",
        "mcp_read_resource": "other",
    },
    # Codex CLI 0.163.0's built-in tools, as the request it sends the model
    # declares them, and the older builds' `shell`, `shell_command` and
    # `local_shell`. Codex has no tool to read, search, write or fetch: it does
    # all four through the shell, so those calls are `shell`.
    "codex": {
        "exec_command": "shell",
        "shell": "shell",
        "shell_command": "shell",
        "local_shell": "shell",
        "write_stdin": "shell_control",
        "view_image": "file_read",
        "apply_patch": "file_edit",
        "web_search": "web_search",
        "spawn_agent": "delegate",
        # These reach an agent that already exists, as Claude Code's
        # `SendMessage` and Devin's `read_subagent` do.
        "send_input": "other",
        "resume_agent": "other",
        "wait_agent": "other",
        "close_agent": "other",
        "tool_search": "other",
        "update_plan": "other",
        "request_user_input": "other",
        "list_mcp_resources": "other",
        "list_mcp_resource_templates": "other",
        "read_mcp_resource": "other",
        "get_goal": "other",
        "create_goal": "other",
        "update_goal": "other",
        "image_generation": "other",
    },
}

#: `(canonical key, recorded key)` pairs per tool, in preference order. Only
#: keys the kind is known to record: Claude Code's from its transcripts, Devin
#: CLI's from the input schemas its binary declares. A tool absent here has no
#: canonical arguments yet, which is the honest answer for a schema not seen.
_ARGUMENT_KEYS: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {
    "claude-code": {
        "Bash": (("command", "command"),),
        "Monitor": (("command", "command"),),
        "Read": (("path", "file_path"),),
        "NotebookRead": (("path", "notebook_path"),),
        "Grep": (("path", "path"),),
        "Glob": (("path", "path"),),
        "Write": (("path", "file_path"), ("content", "content")),
        "Edit": (("path", "file_path"), ("new_text", "new_string")),
        "MultiEdit": (("path", "file_path"),),
        "NotebookEdit": (("path", "notebook_path"), ("new_text", "new_source")),
        "WebFetch": (("url", "url"),),
        "Agent": (("agent", "subagent_type"),),
        "Task": (("agent", "subagent_type"),),
        "Skill": (("skill", "skill"),),
    },
    "devin": {
        "exec": (("command", "command"),),
        "read": (("path", "file_path"),),
        "notebook_read": (("path", "notebook_path"),),
        "notebook_edit": (("path", "notebook_path"),),
        "write": (("path", "file_path"), ("content", "content")),
        "edit": (("path", "file_path"), ("new_text", "new_string")),
        "webfetch": (("url", "url"),),
        # The profile a sub-agent runs with, as a recorded 3000.11.3 call holds
        # it: `{"title", "task", "profile", "is_background"}`.
        "run_subagent": (("agent", "profile"),),
        # The skill tool's one input is the skill's name; the binary does not
        # say which of these two spellings it serialises, and under this tool
        # either one can only hold that name.
        "skill": (("skill", "skill"), ("skill", "name")),
    },
    "codex": {
        # The script as the model wrote it. Codex runs it as `bash -lc <cmd>`,
        # and the wrapper is Codex's, not the command.
        "exec_command": (("command", "cmd"),),
        "shell_command": (("command", "command"),),
        "view_image": (("path", "path"),),
        "spawn_agent": (("agent", "agent_type"),),
    },
}

#: Shells whose `-c` script is the command a person would read, not the shell.
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish"})

#: A file section of Codex's `apply_patch` grammar.
_PATCH_FILE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$")
_PATCH_MOVE = re.compile(r"^\*\*\* Move to: (.+?)\s*$")


def _argv_command(arguments: Mapping[str, object]) -> dict[str, object]:
    """`command` for a tool that records an argv: the script of `bash -lc
    <script>` and the like, else the argv joined as a shell would read it."""
    argv = arguments.get("command")
    if isinstance(argv, str):
        return {"command": argv}
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return {}
    flag = argv[1] if len(argv) == 3 else ""
    if (
        argv[0].rsplit("/", 1)[-1] in _SHELLS
        and flag.startswith("-")
        and not flag.startswith("--")
        and "c" in flag
    ):
        return {"command": argv[2]}
    return {"command": shlex.join(argv)}


def _patch_edits(arguments: Mapping[str, object]) -> dict[str, object]:
    """`edits`, one `{path, kind, new_text}` per file a patch touches, with
    `new_text` its added lines. A patch to one file also gets `path` and
    `new_text`, the keys a single-file edit has everywhere else.

    From the patch's own grammar when the call recorded it (`input`), else from
    the per-file changes Codex recorded when it applied one (`changes`).
    """
    patch = arguments.get("input")
    changes = arguments.get("changes")
    if isinstance(patch, str):
        edits = _parse_patch(patch)
    elif isinstance(changes, Mapping):
        edits = _changed_files(changes)
    else:
        return {}
    if not edits:
        return {}
    found: dict[str, object] = {"edits": edits}
    if len(edits) == 1:
        found["path"] = edits[0]["path"]
        found["new_text"] = edits[0]["new_text"]
    return found


def _parse_patch(text: str) -> list[dict[str, str]]:
    files: list[tuple[list[str], list[str]]] = []
    current: tuple[list[str], list[str]] | None = None
    for line in text.splitlines():
        section = _PATCH_FILE.match(line)
        if section is not None:
            current = ([section.group(2), section.group(1).lower()], [])
            files.append(current)
            continue
        moved = _PATCH_MOVE.match(line)
        if moved is not None and current is not None:
            # The file ends up at the new path; that is where the text lands.
            current[0][0] = moved.group(1)
            continue
        if current is not None and line.startswith("+"):
            current[1].append(line[1:])
    return [
        {"path": head[0], "kind": head[1], "new_text": "\n".join(lines)} for head, lines in files
    ]


def _changed_files(changes: Mapping[object, object]) -> list[dict[str, str]]:
    edits: list[dict[str, str]] = []
    for path, change in changes.items():
        if not isinstance(path, str) or not isinstance(change, Mapping):
            continue
        kind = change.get("type")
        if kind not in ("add", "update", "delete"):
            continue
        if kind == "add":
            content = change.get("content")
            new_text = content if isinstance(content, str) else ""
        else:
            diff = change.get("unified_diff")
            new_text = (
                "\n".join(
                    line[1:]
                    for line in diff.splitlines()
                    if line.startswith("+") and not line.startswith("+++")
                )
                if isinstance(diff, str)
                else ""
            )
        moved = change.get("move_path")
        edits.append(
            {
                "path": moved if isinstance(moved, str) and moved else path,
                "kind": kind,
                "new_text": new_text,
            }
        )
    return edits


def _default_role(arguments: Mapping[str, object]) -> dict[str, object]:
    """Codex runs a sub-agent with no `agent_type` as its `default` role
    (`DEFAULT_ROLE_NAME`), so that is the agent a bare spawn names."""
    role = arguments.get("agent_type")
    return {} if isinstance(role, str) and role.strip() else {"agent": "default"}


#: Canonical arguments a table of keys cannot express, per kind and tool. Each
#: reads only the call's recorded arguments.
_DERIVED: dict[str, dict[str, Callable[[Mapping[str, object]], dict[str, object]]]] = {
    "codex": {
        "shell": _argv_command,
        "local_shell": _argv_command,
        "apply_patch": _patch_edits,
        "spawn_agent": _default_role,
    },
}


def tool_class(agent_kind: str | None, tool_name: str, mcp_server: str | None) -> str | None:
    """The class of one call, or None where the kind's table does not know it."""
    if mcp_server is not None:
        return "mcp"
    table = _CLASS_BY_NAME.get(agent_kind or "")
    if table is None:
        return None
    return table.get(tool_name)


#: The Agent Client Protocol's tool kinds (`ToolKind`, schema v1), one of which
#: an agent speaking ACP declares for each call. A call the table above does not
#: name falls back to its declared kind (ADR-0025), and the kind says one thing:
#: the call is the agent's own built-in. ACP defines kinds so a client can pick
#: an icon, and its definitions span "files or data", "commands or code" and
#: "external data", so none says what a tool touched. A capability comes only
#: from the table.
ACP_TOOL_KINDS = frozenset(
    {
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
)


def class_of_acp_kind(kind: object) -> str | None:
    """`other` for any kind ACP defines, None for anything else."""
    return "other" if isinstance(kind, str) and kind in ACP_TOOL_KINDS else None


def canonical_arguments(
    agent_kind: str | None,
    tool_name: str,
    mcp_server: str | None,
    arguments: Mapping[str, object],
) -> dict[str, object]:
    """The call's arguments under canonical keys, holding only what was recorded."""
    if mcp_server is not None:
        return {}
    keys = _ARGUMENT_KEYS.get(agent_kind or "", {}).get(tool_name, ())
    found: dict[str, object] = {}
    for canonical, recorded in keys:
        if canonical not in found and recorded in arguments:
            found[canonical] = arguments[recorded]
    derive = _DERIVED.get(agent_kind or "", {}).get(tool_name)
    if derive is not None:
        for canonical, value in derive(arguments).items():
            found.setdefault(canonical, value)
    return found
