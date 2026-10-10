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

from collections.abc import Mapping

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
CANONICAL_KEYS = frozenset({"command", "path", "content", "new_text", "url", "agent", "skill"})

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
        # The skill tool's one input is the skill's name; the binary does not
        # say which of these two spellings it serialises, and under this tool
        # either one can only hold that name.
        "skill": (("skill", "skill"), ("skill", "name")),
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
    return found
