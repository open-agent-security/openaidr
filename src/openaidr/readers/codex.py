"""Reads Codex sessions from its rollout files.

Codex CLI, `codex exec`, and the IDE extension and Codex app (which drive the
same agent through `codex app-server`) share one home, `$CODEX_HOME` or
`~/.codex`. Every thread, a sub-agent's included, is one rollout under
`sessions/YYYY/MM/DD/`: append-only JSONL, one `{timestamp, ordinal, type,
payload}` record per line. `adr-sensor`'s Codex parser drops most calls and
reads every returned call as successful, so this reader is OpenAIDR's own, on
the same contract (`docs/specs/codex.md`).

**A record is a turn.** Each message and each call record is one turn, keyed
by the record's own ordinal; outputs and completion items update the call they
name and never add a turn, so appending records cannot move a span (ADR-0001).

**An outcome is Codex's, never the command's.** A call's status comes from the
completion item Codex wrote, the header Codex puts on a call's output, or the
envelope Codex writes when it refuses a call. What a command printed is never
read as a success, a failure or a refusal.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import socket
import stat
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from openaidr.kinds import map_source
from openaidr.model import (
    Compaction,
    ContextItem,
    ContextPart,
    ModelResponse,
    ProviderRefusal,
    Session,
    Status,
    TokenUsage,
    ToolCall,
    Turn,
    span_id,
)
from openaidr.readers.base import ReaderFailure, Window
from openaidr.readers.codex_log import CodexLogs, Placement
from openaidr.toolclass import canonical_arguments, tool_class
from openaidr.toolnames import split_tool_name

SOURCE = "codex"
SESSIONS = "sessions"
ARCHIVED = "archived_sessions"
INDEX = "session_index.jsonl"

#: The same bounds as the Claude Code reader's incremental projections (ADR-0029).
_MAX_INCREMENTAL_FILES = 128
_MAX_INCREMENTAL_BYTES = 16 * 1024 * 1024

#: Response items that are a call, and the tool name each implies when the
#: record carries none of its own.
_CALLS = {
    "function_call": None,
    "custom_tool_call": None,
    "tool_search_call": "tool_search",
    "web_search_call": "web_search",
    "local_shell_call": "local_shell",
    "image_generation_call": "image_generation",
}
#: Response items that answer a call, by the call family they answer.
_OUTPUTS = {
    "function_call_output": ("function_call", "local_shell_call"),
    "custom_tool_call_output": ("custom_tool_call",),
    "tool_search_output": ("tool_search_call",),
}


@dataclass(frozen=True)
class _ItemKind:
    """How one completion item records a call (ADR-0033).

    `tool` names the built-in it stands for, None when the item names its own
    tool (`tool`). `arguments` maps the item's fields to the call's argument
    keys, an empty key spreading that field's object; with none, the item's
    own `arguments` object is the call's. `result` names the one field the
    item records its result in: present, even empty, it is the result; absent,
    there is none (ADR-0034). `opaque` keeps that result whole as JSON, never
    read as text blocks.
    """

    tool: str | None
    arguments: tuple[tuple[str, str], ...] = ()
    result: str | None = None
    opaque: bool = False
    #: The item has no status: its completion is the call succeeding.
    complete: bool = False


#: Every completion item Codex 0.163.0 writes for a call, keyed by `type`, and
#: by `kind` for an `Extension` item: `TurnItem` and `ExtensionItem` are both
#: closed enums (`protocol/src/items.rs`, `ext/items/src/lib.rs`). An item here
#: joins the call whose `call_id` is its `id`; with no such call it is its own
#: (an execution nested inside another tool).
_CALL_ITEMS: dict[str, _ItemKind] = {
    "CommandExecution": _ItemKind("shell", (("command", "command"),), result="aggregated_output"),
    "FileChange": _ItemKind("apply_patch", (("changes", "changes"),), result="stdout"),
    "McpToolCall": _ItemKind(None, result="result"),
    "DynamicToolCall": _ItemKind(None, result="content_items"),
    "CollabAgentToolCall": _ItemKind(None),
    "WebSearch": _ItemKind(
        "web_search", (("action", ""),), result="results", opaque=True, complete=True
    ),
    "ImageView": _ItemKind("view_image", (("path", "path"),), complete=True),
    "ImageGeneration": _ItemKind(
        "image_generation", (("revised_prompt", "revised_prompt"),), result="result"
    ),
    "image_gen.generation": _ItemKind(
        "image_generation", (("revisedPrompt", "revised_prompt"),), result="result"
    ),
    "clock.sleep": _ItemKind("sleep", (("durationMs", "duration_ms"),), complete=True),
    "web.search": _ItemKind(
        "web_search", (("action", ""),), result="results", opaque=True, complete=True
    ),
}
#: The rest of `TurnItem`: none is an invocation, so none is a call.
#: `FunctionCallOutput` is an output the client supplied as turn input; the
#: `function_call_output` response item beside it is the one read.
_NOT_CALL_ITEMS = frozenset(
    {
        "UserMessage",
        "FunctionCallOutput",
        "HookPrompt",
        "AgentMessage",
        "Plan",
        "Reasoning",
        "SubAgentActivity",
        "EnteredReviewMode",
        "ExitedReviewMode",
        "ContextCompaction",
    }
)
#: `CollabAgentTool` as written, where it differs from the tool the model
#: called: `wait` is the `wait_agent` tool.
_COLLAB_TOOLS = {"wait": "wait_agent"}
#: Codex 0.163.0's own namespaced tools, `(namespace, name)` as the model
#: calls them, to the built-in each is (`core/src/tools/handlers/sleep.rs`,
#: `ext/web-search/src/tool.rs`, `core/src/tools/spec_plan.rs`). Read when no
#: item names the call, as for a refused or a still-running one.
_NAMESPACED_BUILTINS = {
    ("clock", "sleep"): "sleep",
    ("web", "run"): "web_search",
    ("image_gen", "imagegen"): "image_generation",
}
#: Items whose own name beats the namespace their call was made under.
_NAMED_BY_ITEM = frozenset(
    {"CollabAgentToolCall", "image_gen.generation", "clock.sleep", "web.search"}
)
#: Multi-Agent V1's namespace, and V2's default (`multi_agent_v2.tool_namespace`
#: can name another, so a V2 call elsewhere is recognised by its item alone).
_COLLAB_NAMESPACES = frozenset({"multi_agent_v1", "collaboration"})
#: Item statuses that end a call, by what they mean. `declined` is a person's
#: refusal; `interrupted` a person's stop.
_ITEM_STATES: dict[str, Status] = {
    "completed": "ok",
    "failed": "error",
    "declined": "rejected",
    "interrupted": "interrupted",
}
#: A user message is a person's when every part Codex says it holds is typed
#: text. Codex marks what it injected (`environments.environment_context`,
#: `generic.turn_aborted`, `multi_agent.subagent_notification`, ...).
_TYPED = "user."
#: Before Codex marked its parts, what it injected opened with one of these.
_INJECTED_OPENERS = (
    "<environment_context>",
    "<user_instructions>",
    "<turn_aborted>",
    "<subagent_notification>",
    "# AGENTS.md instructions",
)
#: `codex_error_info` codes that are the provider declining the request.
_REFUSALS = frozenset(
    {"cyber_policy", "bio_policy", "misalignment_policy_violation", "invalid_prompt"}
)

#: The envelope Codex writes as a call's output when it refuses to run the
#: call, at the start of the output, where a command's own output never sits.
_REJECTED = re.compile(r'^exec_command failed: CreateProcess \{ message: "Rejected\(')
#: Who refused, by Codex's own words inside or instead of that envelope
#: (`core/src/tools/approvals.rs`, `core/src/exec_policy.rs`). First match wins.
_DENIALS: tuple[tuple[re.Pattern[str], str | None], ...] = (
    (re.compile(r"rejected by user"), "user-rejected"),
    (re.compile(r"automatic approval review denied the action"), "automode-blocked"),
    (re.compile(r"rejected by configuration"), "permission-rule"),
    (re.compile(r"forbidden by policy|policy forbids commands starting with"), "permission-rule"),
    (re.compile(r"rm -f style commands are not permitted"), "permission-rule"),
)
#: Refusals Codex writes without the envelope.
_BARE_DENIALS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^approval policy is Never; reject command"), "permission-rule"),
    (re.compile(r"^patch rejected by user"), "user-rejected"),
)
#: Codex's output when a person stopped a running call.
_ABORTED = re.compile(r"^(?:Wall time: [^\n]*\n)?aborted by user\s*$")
#: The header Codex writes above a call's output, before its `Output:` line.
_EXIT = re.compile(r"^(?:Process exited with code|Exit code:) (-?\d+)\s*$", re.MULTILINE)
_HEADER_END = re.compile(r"^Output:\s*$", re.MULTILINE)
#: Codex's own marks where it cut a call's output (`utils/string/src/truncate.rs`).
_TRUNCATED = re.compile(r"…\d+ (?:tokens|chars) truncated…")
#: The status line a Code Mode cell opens its output with
#: (`core/src/tools/code_mode/mod.rs`, `format_script_status`), and what each
#: says of the cell. A cell still running yields `Script running with cell ID`.
_SCRIPT_STATES: dict[str, Status] = {
    "Script completed": "ok",
    "Script failed": "error",
    "Script terminated": "interrupted",
}
#: The deepest a record or a call's JSON arguments may nest. Codex writes with
#: serde_json's default limit of 128, so nothing Codex wrote is deeper; this
#: stays far below Python's own recursion limit (ADR-0033).
_MAX_DEPTH = 256


def default_root() -> Path:
    home = os.environ.get("CODEX_HOME")
    return Path(home) if home else Path(os.path.expanduser("~")) / ".codex"


@dataclass
class _Call:
    family: str
    name: str
    namespace: str | None
    call_id: str | None
    arguments: dict[str, object]
    #: The directory the turn ran in when the call was made.
    cwd: str | None
    turn: str | None
    status_hint: str | None = None
    #: A result a self-contained response item carries inline, with no
    #: separate output or completion record to join it from.
    result: str | None = None


@dataclass
class _Record:
    """A turn in the order Codex wrote it: a message, a client error, or a call."""

    key: str
    role: str
    text: str
    at: datetime | None
    turn: str | None
    permission_mode: str | None
    call: _Call | None = None
    response_id: str | None = None


@dataclass
class _Output:
    body: str | None
    status: str | None


@dataclass
class _Projection:
    """Everything one rollout has recorded so far, as facts; sessions are built from it."""

    meta: dict[str, object] | None = None
    boundary: int | None = None
    #: The highest ordinal turned into a key so far, past the boundary.
    max_ordinal: int | None = None
    records: list[_Record] = field(default_factory=list)
    #: Per call id, the index of its record; None once the id is used twice.
    calls: dict[str, int | None] = field(default_factory=dict)
    outputs: dict[str, list[_Output]] = field(default_factory=dict)
    items: dict[str, dict[str, object]] = field(default_factory=dict)
    #: Per Codex turn: its `turn_context` model, its context window, whether it ended.
    models: dict[str, str] = field(default_factory=dict)
    windows: dict[str, int] = field(default_factory=dict)
    ended: set[str] = field(default_factory=set)
    #: Per Codex turn, the records waiting for the response that produced them.
    awaiting: dict[str, list[int]] = field(default_factory=dict)
    responses: dict[str, ModelResponse] = field(default_factory=dict)
    response_turns: dict[str, str | None] = field(default_factory=dict)
    context: list[ContextItem] = field(default_factory=list)
    compactions: list[Compaction] = field(default_factory=list)
    refusals: list[ProviderRefusal] = field(default_factory=list)
    initial_prompt: str | None = None
    started_at: datetime | None = None
    last_activity_at: datetime | None = None
    model: str | None = None
    cwd: str | None = None
    turn: str | None = None
    permission_mode: str | None = None
    warnings: set[str] = field(default_factory=set)
    #: The physical line of the record being projected. A context item's span
    #: is its line: stable as the file grows (ADR-0011), and an integer.
    line: int = 0

    # --- identity --------------------------------------------------------

    @property
    def thread_id(self) -> str:
        assert self.meta is not None
        return str(self.meta["id"])

    @property
    def session_id(self) -> str:
        assert self.meta is not None
        root = _string(self.meta.get("session_id"))
        if self.sidechain:
            parent = (
                root
                if root and root != self.thread_id
                else _string(self.meta.get("parent_thread_id"))
            )
            if parent:
                return f"{SOURCE}:{parent}:{self.thread_id}"
        return f"{SOURCE}:{self.thread_id}"

    @property
    def sidechain(self) -> bool:
        assert self.meta is not None
        source = self.meta.get("source")
        return (
            (isinstance(source, Mapping) and "subagent" in source)
            or bool(_string(self.meta.get("parent_thread_id")))
            or self.meta.get("thread_source") == "subagent"
        )

    # --- records ---------------------------------------------------------

    def project(self, record: dict[str, object], line: int) -> None:
        kind = record.get("type")
        payload = record.get("payload")
        if not isinstance(kind, str) or not isinstance(payload, dict):
            raise TypeError("record has no type or no object payload")
        at = _instant(record.get("timestamp"))
        self.line = line
        if self.meta is None:
            if kind != "session_meta" or not _string(payload.get("id")):
                raise ValueError("first record is not a session_meta with an id")
            self._meta(payload, line, at)
            return
        if kind == "session_meta":
            # A copy inside history the thread inherited; its first is its own.
            return
        ordinal = record.get("ordinal")
        if self.boundary is not None:
            if type(ordinal) is not int:
                self.warnings.add(
                    "a record without an ordinal follows an inherited-history boundary; not read"
                )
                return
            if ordinal < self.boundary:
                return
        if type(ordinal) is int and ordinal >= 0:
            if self.max_ordinal is not None and ordinal <= self.max_ordinal:
                # Codex's own ordinal is monotonic per file (Source); a repeat
                # or a regression would key two turns alike, colliding their
                # spans, so the session is withheld rather than half-read.
                raise ValueError(f"ordinal {ordinal} does not advance past {self.max_ordinal}")
            self.max_ordinal = ordinal
            key = f"o{ordinal}"
        else:
            key = f"l{line}"
        self._seen(at)
        if kind == "turn_context":
            self._turn_context(payload)
        elif kind == "event_msg":
            self._event(payload, key, at)
        elif kind == "response_item":
            self._response_item(payload, key, at)
        elif kind == "token_usage_record":
            self._usage(payload, at)
        elif kind == "compacted":
            self._compacted(payload, at)

    def _meta(self, payload: dict[str, object], line: int, at: datetime | None) -> None:
        self.meta = payload
        self.cwd = _string(payload.get("cwd"))
        self._seen(_instant(payload.get("timestamp")) or at)
        bounds = [
            value
            for value in (
                payload.get("subagent_history_start_ordinal"),
                payload.get("forked_from_ordinal_exclusive"),
            )
            if type(value) is int and value >= 0
        ]
        self.boundary = max(bounds) if bounds else None
        instructions = payload.get("base_instructions")
        text = (
            _string(_mapping(instructions).get("text")) if isinstance(instructions, dict) else None
        )
        if text:
            self._context("system", text, at)

    def _seen(self, at: datetime | None) -> None:
        if at is None:
            return
        self.started_at = at if self.started_at is None else min(self.started_at, at)
        self.last_activity_at = (
            at if self.last_activity_at is None else max(self.last_activity_at, at)
        )

    def _turn_context(self, payload: Mapping[str, object]) -> None:
        turn = _string(payload.get("turn_id"))
        model = _string(payload.get("model"))
        if turn is not None:
            self.turn = turn
            if model is not None:
                self.models[turn] = model
        self.model = model or self.model
        self.cwd = _string(payload.get("cwd")) or self.cwd
        approval = _string(payload.get("approval_policy"))
        sandbox = _string(_mapping(payload.get("sandbox_policy")).get("type"))
        modes = [mode for mode in (approval, sandbox) if mode]
        self.permission_mode = "; ".join(modes) or self.permission_mode

    def _event(self, payload: Mapping[str, object], key: str, at: datetime | None) -> None:
        kind = payload.get("type")
        if kind == "task_started":
            turn = _string(payload.get("turn_id")) or _string(
                _mapping(payload.get("turn_attribution")).get("turn_id")
            )
            if turn is not None:
                self.turn = turn
                window = payload.get("model_context_window")
                if type(window) is int and window > 0:
                    self.windows[turn] = window
        elif kind in ("task_complete", "turn_aborted"):
            turn = _string(payload.get("turn_id"))
            if turn is not None:
                self.ended.add(turn)
            error = _mapping(payload.get("error")) if kind == "task_complete" else {}
            message = _string(error.get("message"))
            if message is not None:
                self._error(key, message, _string(error.get("codex_error_info")), at, turn)
        elif kind == "item_completed":
            thread = payload.get("thread_id")
            if thread is not None and thread != self.thread_id:
                return
            item = payload.get("item")
            if isinstance(item, dict):
                self._item(dict(item), payload, key, at)
        elif kind == "user_message":
            if self.initial_prompt is None:
                self.initial_prompt = _string(payload.get("message"))
        elif kind == "patch_apply_end":
            call_id = _string(payload.get("call_id"))
            if call_id is not None:
                success = payload.get("success")
                self._item(
                    {
                        "type": "FileChange",
                        "id": call_id,
                        "status": "completed"
                        if success is True
                        else "failed"
                        if success is False
                        else None,
                        "changes": payload.get("changes"),
                        "stdout": payload.get("stdout"),
                        "stderr": payload.get("stderr"),
                    },
                    payload,
                    key,
                    at,
                )
        elif kind == "mcp_tool_call_end":
            call_id = _string(payload.get("call_id"))
            invocation = _mapping(payload.get("invocation"))
            result = _mapping(payload.get("result"))
            if call_id is not None and invocation:
                failed = "Err" in result
                ok = _mapping(result.get("Ok"))
                self._item(
                    {
                        "type": "McpToolCall",
                        "id": call_id,
                        "server": invocation.get("server"),
                        "tool": invocation.get("tool"),
                        "arguments": invocation.get("arguments"),
                        "status": "failed" if failed else "completed",
                        "result": ok or None,
                        "error": result.get("Err") if failed else None,
                        "duration": payload.get("duration"),
                    },
                    payload,
                    key,
                    at,
                )
        elif kind == "exec_command_end":
            call_id = _string(payload.get("call_id"))
            if call_id is not None:
                exit_code = payload.get("exit_code")
                self._item(
                    {
                        "type": "CommandExecution",
                        "id": call_id,
                        "command": payload.get("command"),
                        "cwd": payload.get("cwd"),
                        "exit_code": exit_code,
                        "status": "completed" if exit_code == 0 else "failed",
                        "aggregated_output": payload.get("aggregated_output"),
                        "duration": payload.get("duration"),
                    },
                    payload,
                    key,
                    at,
                )

    def _item(
        self,
        item: dict[str, object],
        event: Mapping[str, object],
        key: str,
        at: datetime | None,
    ) -> None:
        kind = item.get("type")
        if kind == "UserMessage":
            if self.initial_prompt is None:
                self.initial_prompt = _text(item.get("content")) or None
            return
        if kind in _NOT_CALL_ITEMS:
            return
        key_name = item.get("kind") if kind == "Extension" else kind
        if not isinstance(key_name, str) or key_name not in _CALL_ITEMS:
            # Outside Codex 0.163.0's closed vocabulary (ADR-0033): reported,
            # never dropped silently and never guessed at.
            self.warnings.add(
                f"completion item {key_name!r} is not read; tool coverage is incomplete"
            )
            return
        item["_kind"] = key_name
        call_id = _string(item.get("id"))
        if call_id is None:
            return
        started, completed = event.get("started_at_ms"), event.get("completed_at_ms")
        if type(started) is int and type(completed) is int and completed >= started:
            item.setdefault("_elapsed_ms", completed - started)
        if call_id not in self.calls:
            # An execution no call record names: its own turn, never attached to
            # another call by position.
            # The turn the event names, so a late completion keeps its own
            # turn's end and response (ADR-0035); else the turn in force.
            turn = _string(event.get("turn_id")) or self.turn
            self._call_record(key, at, _item_call(item, call_id, self.cwd, turn))
        # A later snapshot of the same execution replaces the earlier one.
        self.items[call_id] = item

    def _response_item(self, payload: dict[str, object], key: str, at: datetime | None) -> None:
        kind = payload.get("type")
        if not isinstance(kind, str):
            raise TypeError("response item has no type")
        turn = (
            _string(
                _mapping(payload.get("internal_chat_message_metadata_passthrough")).get("turn_id")
            )
            or self.turn
        )
        if kind == "message":
            self._message(payload, key, at, turn)
        elif kind in _CALLS:
            self._call_record(key, at, _response_call(kind, payload, self.cwd, turn))
        elif kind in _OUTPUTS:
            self._output(kind, payload, key, at)
        elif kind.endswith(("_call", "_output")):
            self.warnings.add(f"response item {kind!r} is not read; tool coverage is incomplete")

    def _message(
        self, payload: Mapping[str, object], key: str, at: datetime | None, turn: str | None
    ) -> None:
        role = payload.get("role")
        text = _text(payload.get("content"))
        if role in ("developer", "system"):
            self._context("system", text, at)
            return
        if role == "user" and _injected(payload, text):
            self._context("injected", text, at)
            return
        if role not in ("user", "assistant"):
            return
        self._append(
            _Record(key, str(role), text, at, turn, self.permission_mode),
            awaits=role == "assistant",
        )

    def _call_record(self, key: str, at: datetime | None, call: _Call) -> None:
        index = len(self.records)
        if call.call_id is not None:
            known = self.calls.get(call.call_id)
            if known is not None and self.records[known].call is not None:
                held = self.records[known].call
                if held is not None and held.family == "item" and call.family != "item":
                    # Codex wrote the outcome before the call: the call takes
                    # the turn its outcome opened, so no span moves.
                    self.records[known].call = call
                    return
            # A call id used twice: neither use is joined to an outcome, since
            # either could own it, and no span moves.
            self.calls[call.call_id] = None if call.call_id in self.calls else index
        self._append(
            _Record(key, "assistant", "", at, call.turn, self.permission_mode, call=call),
            awaits=True,
        )

    def _append(self, record: _Record, *, awaits: bool) -> None:
        index = len(self.records)
        self.records.append(record)
        if awaits and record.turn is not None:
            self.awaiting.setdefault(record.turn, []).append(index)

    def _output(
        self, kind: str, payload: Mapping[str, object], key: str, at: datetime | None
    ) -> None:
        call_id = _string(payload.get("call_id"))
        if call_id is None:
            if kind == "tool_search_output":
                self._search_context(payload, key, at)
                return
            raise ValueError("call output has no call_id")
        if kind == "tool_search_output":
            self._search_context(payload, key, at)
            body: object = payload.get("tools", [])
        else:
            if "output" not in payload:
                raise ValueError("call output has no output")
            body = payload.get("output")
        self.outputs.setdefault(call_id, []).append(
            _Output(_body(body), _string(payload.get("status")))
        )

    def _search_context(self, payload: Mapping[str, object], key: str, at: datetime | None) -> None:
        """A tool search names each MCP server it found and that server's own instructions."""
        tools = payload.get("tools")
        if not isinstance(tools, list):
            return
        parts: list[ContextPart] = []
        for entry in tools:
            entry = _mapping(entry)
            name = _string(entry.get("name"))
            description = _string(entry.get("description"))
            if (
                entry.get("type") == "namespace"
                and name
                and name.startswith("mcp__")
                and description
            ):
                parts.append(
                    ContextPart(
                        name=name.removeprefix("mcp__").removesuffix("__"), text=description
                    )
                )
        if parts:
            text = "\n\n".join(part.text for part in parts)
            self.context.append(
                ContextItem(
                    span=span_id(self.session_id, "context", self.line),
                    source="mcp_instructions",
                    name=None,
                    text=text,
                    parts=tuple(parts),
                    occurred_at=at,
                )
            )

    def _usage(self, payload: Mapping[str, object], at: datetime | None) -> None:
        response_id = _string(payload.get("response_id"))
        usage = _mapping(payload.get("usage"))
        if response_id is None or not usage or response_id in self.responses:
            return
        turn = _string(payload.get("turn_id"))
        cached = _count(usage.get("cached_input_tokens"))
        recorded_input = _count(usage.get("input_tokens"))
        written = _count(usage.get("cache_write_input_tokens"))
        self.responses[response_id] = ModelResponse(
            response_id=response_id,
            request_id=None,
            model=self.models.get(turn, self.model) if turn is not None else self.model,
            occurred_at=at,
            final=True,
            is_sidechain=self.sidechain,
            usage=TokenUsage(
                # Codex's input includes the tokens it read from cache; this
                # model's input is the part that was not (`docs/specs/codex.md`).
                input_tokens=max(recorded_input - (cached or 0), 0)
                if recorded_input is not None
                else None,
                cache_read_input_tokens=cached,
                # Codex writes 0 where the provider reported nothing, so a 0
                # is not a measurement.
                cache_creation_input_tokens=written if written else None,
                output_tokens=_count(usage.get("output_tokens")),
                reasoning_tokens=_count(usage.get("reasoning_output_tokens")),
            ),
            context_window=self.windows.get(turn) if turn is not None else None,
        )
        self.response_turns[response_id] = turn
        for index in self.awaiting.pop(turn, []) if turn is not None else []:
            self.records[index].response_id = response_id

    def _compacted(self, payload: Mapping[str, object], at: datetime | None) -> None:
        latest = _mapping(_mapping(payload.get("latest_token_usage_record")).get("usage"))
        self.compactions.append(
            Compaction(
                trigger="unknown",
                pre_tokens=_count(latest.get("input_tokens")),
                dropped_tokens=None,
                occurred_at=at,
            )
        )

    def _error(
        self, key: str, message: str, code: str | None, at: datetime | None, turn: str | None
    ) -> None:
        self._append(_Record(key, "error", message, at, turn, self.permission_mode), awaits=False)
        if code in _REFUSALS or "reason: content_filter" in message:
            self.refusals.append(
                ProviderRefusal(
                    category=code if code in _REFUSALS else "content_filter",
                    original_model=self.models.get(turn, self.model) if turn else self.model,
                    fallback_model=None,
                )
            )

    def _context(self, source: str, text: str, at: datetime | None) -> None:
        if text and self.meta is not None:
            self.context.append(
                ContextItem(
                    span=span_id(self.session_id, "context", self.line),
                    source=source,
                    name=None,
                    text=text,
                    occurred_at=at,
                )
            )

    # --- the session -----------------------------------------------------

    def session(self, placement: Placement, title: str | None) -> Session | None:
        if self.meta is None:
            return None
        session_id = self.session_id
        sidechain = self.sidechain
        transports = {c.server: c.transport for c in placement.connections}
        turns: list[Turn] = []
        for record in self.records:
            calls: tuple[ToolCall, ...] = ()
            if record.call is not None:
                calls = (self._tool_call(session_id, record, transports),)
            turns.append(
                Turn(
                    position=len(turns),
                    key=record.key,
                    role=record.role,
                    text=record.text,
                    is_sidechain=sidechain,
                    tool_calls=calls,
                    permission_mode=record.permission_mode,
                    occurred_at=record.at,
                    response_id=record.response_id,
                )
            )
        git = _mapping(self.meta.get("git"))
        return Session(
            session_id=session_id,
            agent_kind=map_source(SOURCE),
            source=SOURCE,
            started_at=self.started_at,
            model=self.model,
            working_directory=_string(self.meta.get("cwd")),
            machine=socket.gethostname() or None,
            user=_user(),
            turns=tuple(turns),
            agent_version=_string(self.meta.get("cli_version")),
            entrypoint=_entrypoint(self.meta),
            git_branch=_string(git.get("branch")),
            initial_prompt=self.initial_prompt,
            context_items=tuple(self.context),
            compactions=tuple(self.compactions),
            mcp_connections=placement.connections,
            mcp_log_state=placement.state,
            provider_refusals=tuple(self.refusals),
            last_activity_at=self.last_activity_at,
            title=title,
            responses=tuple(self.responses.values()),
            compactions_recorded=True,
            subagents_recorded=True,
        )

    def _tool_call(
        self, session_id: str, record: _Record, transports: Mapping[str, str | None]
    ) -> ToolCall:
        call = record.call
        assert call is not None
        position = self.calls.get(call.call_id) if call.call_id is not None else None
        joined = (
            call.call_id is not None and position is not None and self.records[position] is record
        )
        item = self.items.get(call.call_id, {}) if joined and call.call_id else {}
        outputs = self.outputs.get(call.call_id, []) if joined and call.call_id else []
        output = outputs[0] if len(outputs) == 1 else None
        if len(outputs) > 1:
            item = {}
        server, tool = _names(call, item, transports)
        status, denial, exit_code = _outcome(
            call, item, output, call.turn in self.ended if call.turn else True
        )
        if not joined and call.call_id is not None:
            status, denial, exit_code = "unknown", None, None
        body = output.body if output is not None else _item_body(item)
        if body is None:
            body = call.result
        arguments = call.arguments
        if not arguments and item.get("type") == "McpToolCall":
            arguments = _mapping(item.get("arguments"))
        return ToolCall(
            span=span_id(session_id, record.key, 0),
            tool_name=tool,
            mcp_server=server,
            status=status,
            arguments=arguments,
            result=body,
            result_size=len(body) if body is not None else None,
            error_text=_error_text(item, body) if status == "error" else None,
            truncated=bool(body and _TRUNCATED.search(body)),
            provider_call_id=call.call_id,
            duration_ms=_duration(item),
            denial_kind=denial,
            working_directory=_working_directory(item, call),
            attributed_plugin=_string(item.get("plugin_id")) or _string(item.get("pluginId")),
            transport=transports.get(server) if server is not None else None,
            tool_class=tool_class(SOURCE, tool, server),
            canonical_arguments=canonical_arguments(SOURCE, tool, server, arguments),
            exit_code=exit_code,
        )


def _response_call(
    kind: str, payload: Mapping[str, object], cwd: str | None, turn: str | None
) -> _Call:
    implied = _CALLS[kind]
    name = implied or _string(payload.get("name"))
    if name is None:
        raise ValueError("tool call has no name")
    if kind == "custom_tool_call":
        arguments: dict[str, object] = {"input": payload.get("input")}
    elif kind == "web_search_call":
        arguments = _mapping(payload.get("action"))
    elif kind == "local_shell_call":
        action = _mapping(payload.get("action"))
        arguments = {"command": action.get("command"), "workdir": action.get("working_directory")}
    elif kind == "image_generation_call":
        arguments = {"revised_prompt": payload.get("revised_prompt")}
    else:
        arguments = _arguments(payload.get("arguments"))
    return _Call(
        family=kind,
        name=name,
        namespace=_string(payload.get("namespace")),
        call_id=_string(payload.get("call_id"))
        or (
            _string(payload.get("id"))
            if kind in ("web_search_call", "image_generation_call")
            else None
        ),
        arguments=arguments,
        cwd=cwd,
        turn=turn,
        status_hint=_string(payload.get("status"))
        if kind in ("web_search_call", "tool_search_call", "image_generation_call")
        else None,
        result=_string(payload.get("result")) if kind == "image_generation_call" else None,
    )


def _item_call(
    item: Mapping[str, object], call_id: str, cwd: str | None, turn: str | None
) -> _Call:
    shape = _CALL_ITEMS[str(item["_kind"])]
    arguments: dict[str, object] = {}
    for field_name, key in shape.arguments:
        value = item.get(field_name)
        if key:
            arguments[key] = value
        else:
            arguments.update(_mapping(value))
    if not shape.arguments:
        arguments = _mapping(item.get("arguments"))
    return _Call(
        family="item",
        name=_item_tool(item) or str(item["_kind"]),
        namespace=None,
        call_id=call_id,
        arguments=arguments,
        cwd=cwd,
        turn=turn,
    )


def _item_tool(item: Mapping[str, object]) -> str | None:
    """The built-in an item says the call was, from Codex's own closed
    vocabulary; None when the item names no built-in (an MCP or dynamic tool)."""
    kind = item.get("_kind")
    if not isinstance(kind, str):
        return None
    if kind == "CollabAgentToolCall":
        tool = _string(item.get("tool"))
        return _COLLAB_TOOLS.get(tool, tool) if tool else None
    if kind == "DynamicToolCall":
        return _string(item.get("tool"))
    return _CALL_ITEMS[kind].tool


def _sanitize_server(name: str) -> str:
    """The namespace Codex would call `name` under, `mcp__<this>__<tool>`."""
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _names(
    call: _Call, item: Mapping[str, object], transports: Mapping[str, str | None]
) -> tuple[str | None, str]:
    """`(server, tool)`, by the first rule that applies (`docs/specs/codex.md#tool-names`):
    the item's own names, as the server is configured or as Codex's closed
    vocabulary says; then the namespace Codex called it under; then a flat
    legacy name."""
    if item.get("type") == "McpToolCall":
        server, tool = _string(item.get("server")), _string(item.get("tool"))
        if server and tool:
            return server, tool
    if call.namespace is not None and item.get("_kind") in _NAMED_BY_ITEM:
        # A namespace Codex lets configuration choose, or one wrapping a tool
        # the item names exactly: the item says which built-in it was.
        tool = _item_tool(item)
        if tool:
            return None, tool
    namespace = call.namespace
    if namespace is not None:
        if namespace.startswith("mcp__") and len(namespace) > len("mcp__"):
            # Codex sanitises a server's name into its namespace, collapsing any
            # punctuation to `_`, so the namespace alone cannot tell `my-server`
            # from `my_server`. A connection the log database names is ground
            # truth; prefer it when exactly one configured server matches.
            sanitized = namespace.removeprefix("mcp__").removesuffix("__")
            configured = [name for name in transports if _sanitize_server(name) == sanitized]
            if len(configured) == 1:
                return configured[0], call.name
            return sanitized, call.name
        builtin = _NAMESPACED_BUILTINS.get((namespace, call.name))
        if builtin is not None:
            return None, builtin
        if namespace in _COLLAB_NAMESPACES:
            return None, call.name
        return None, f"{namespace}.{call.name}"
    return split_tool_name(SOURCE, call.name)


def _outcome(
    call: _Call, item: Mapping[str, object], output: _Output | None, turn_ended: bool
) -> tuple[Status, str | None, int | None]:
    """`(status, denial_kind, exit_code)`, from Codex's records alone."""
    body = output.body if output is not None else None
    if body is not None:
        if _ABORTED.match(body):
            return "interrupted", None, None
        refused = _refusal(body)
        if refused is not None:
            return "rejected", refused[1], None
        if call.family == "custom_tool_call" and call.name == "exec":
            # A Code Mode cell (ADR-0036): its own status line, before anything
            # the script printed, and never the outcome of a tool it ran, which
            # is that tool's own call.
            state = _SCRIPT_STATES.get(body.split("\n", 1)[0].strip())
            if state is not None:
                return state, None, None
            if body.startswith("Script running with cell ID"):
                return "pending", None, None
    exit_code = item.get("exit_code")
    exit_code = exit_code if type(exit_code) is int else None
    if exit_code is None and body is not None:
        exit_code = _header_exit(body)
    kind = item.get("_kind")
    if isinstance(kind, str):
        state = _ITEM_STATES.get(str(item.get("status")))
        if state is None and _CALL_ITEMS[kind].complete:
            state = "ok"
        if state == "rejected":
            return "rejected", "user-rejected", exit_code
        if state == "interrupted":
            return "interrupted", None, exit_code
        if kind == "McpToolCall":
            failed = state == "error" or _mapping(item.get("result")).get("isError") is True
            return ("error" if failed else state or "unknown"), None, None
        if state == "error" or (exit_code is not None and exit_code != 0):
            return "error", None, exit_code
        if state == "ok":
            return "ok", None, exit_code
    if exit_code is not None:
        return ("ok" if exit_code == 0 else "error"), None, exit_code
    hint = (output.status if output is not None else None) or call.status_hint
    if call.family in (
        "tool_search_call",
        "web_search_call",
        "image_generation_call",
    ) and hint in ("completed", "failed"):
        return ("ok" if hint == "completed" else "error"), None, None
    if output is None and not item:
        return ("unknown" if turn_ended else "pending"), None, None
    return "unknown", None, None


def _refusal(body: str) -> tuple[str, str | None] | None:
    """`("rejected", denial_kind)` when the output is Codex refusing the call."""
    if _REJECTED.match(body):
        for pattern, denial in _DENIALS:
            if pattern.search(body):
                return "rejected", denial
        return "rejected", None
    for pattern, denial in _BARE_DENIALS:
        if pattern.match(body):
            return "rejected", denial
    return None


def _header_exit(body: str) -> int | None:
    """The exit status in Codex's header above a call's output, never below it."""
    end = _HEADER_END.search(body)
    if end is None:
        return None
    found = _EXIT.search(body, 0, end.start())
    return int(found.group(1)) if found else None


def _item_body(item: Mapping[str, object]) -> str | None:
    """The result the item recorded, from its own field (ADR-0034): `""` and
    `[]` are what the call returned, a missing field or `null` is none."""
    kind = item.get("_kind")
    shape = _CALL_ITEMS[kind] if isinstance(kind, str) else None
    value = item.get(shape.result) if shape is not None and shape.result is not None else None
    if shape is not None and shape.opaque and value is not None:
        # Codex keeps it opaque so new fields pass through; so does this.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, Mapping):
        # An MCP `CallToolResult`: its content is what the tool returned.
        value = value.get("content", value)
    return _body(value)


def _error_text(item: Mapping[str, object], body: str | None) -> str | None:
    error = item.get("error")
    if isinstance(error, Mapping):
        error = error.get("message")
    for value in (error, item.get("stderr"), item.get("aggregated_output")):
        if isinstance(value, str) and value:
            return value
    return body


def _duration(item: Mapping[str, object]) -> int | None:
    duration = _mapping(item.get("duration"))
    seconds, nanos = duration.get("secs"), duration.get("nanos")
    if type(seconds) is int and type(nanos) is int and seconds >= 0 and nanos >= 0:
        return seconds * 1000 + nanos // 1_000_000
    elapsed = item.get("_elapsed_ms")
    return elapsed if type(elapsed) is int else None


def _working_directory(item: Mapping[str, object], call: _Call) -> str | None:
    cwd = _string(item.get("cwd"))
    if cwd is not None:
        return _path_of(cwd)
    workdir = _string(call.arguments.get("workdir"))
    if workdir is not None:
        if os.path.isabs(workdir) or call.cwd is None:
            return workdir
        return os.path.normpath(os.path.join(call.cwd, workdir))
    return call.cwd


def _path_of(value: str) -> str:
    if value.startswith("file://"):
        return unquote(urlparse(value).path)
    return value


def _entrypoint(meta: Mapping[str, object]) -> str | None:
    """`session_meta.source`: `cli`, `exec`, `vscode` or `mcp`. A sub-agent's
    source is an object, so its entrypoint is read off the `originator` it
    shares with the thread that spawned it."""
    source = meta.get("source")
    if isinstance(source, str) and source:
        return source
    originator = _string(meta.get("originator"))
    if originator is None:
        return None
    return {"codex_exec": "exec", "codex_cli_rs": "cli"}.get(originator, "vscode")


def _injected(payload: Mapping[str, object], text: str) -> bool:
    kinds = _mapping(payload.get("internal_chat_message_metadata_passthrough")).get(
        "content_item_kinds"
    )
    if isinstance(kinds, list) and kinds:
        return not all(isinstance(kind, str) and kind.startswith(_TYPED) for kind in kinds)
    return text.lstrip().startswith(_INJECTED_OPENERS)


def _arguments(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, RecursionError):
            return {"raw": value}
        if _deeper_than(decoded, _MAX_DEPTH):
            # Decodable but nested past the bound: kept as written, and the
            # session read.
            return {"raw": value}
        return decoded if isinstance(decoded, dict) else {"raw": decoded}
    return {} if value is None else {"raw": value}


def _deeper_than(value: object, limit: int) -> bool:
    """Whether a decoded JSON value nests more than `limit` containers deep.
    Iterative, so a hostile value cannot exhaust the stack measuring itself."""
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        node, depth = pending.pop()
        if isinstance(node, dict):
            children: list[object] = list(node.values())
        elif isinstance(node, list):
            children = node
        else:
            continue
        if depth > limit:
            return True
        pending.extend((child, depth + 1) for child in children)
    return False


def _body(value: object) -> str | None:
    """A call's output as text: a string as written; a list of text blocks
    joined; anything else kept whole as JSON, never elided."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if (
        isinstance(value, list)
        and value
        and all(isinstance(part, dict) and isinstance(part.get("text"), str) for part in value)
    ):
        return "\n".join(str(part["text"]) for part in value)
    return json.dumps(value, ensure_ascii=False)


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def _instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
        return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    except (ValueError, OverflowError):
        # A boundary offset, e.g. "0001-01-01T00:00:00+23:59", parses fine but
        # overflows converting to UTC; as unrepresentable as an instant it is.
        return None


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _user() -> str | None:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return None


# --- reading files ------------------------------------------------------------


@dataclass
class _Cursor:
    """One rollout's committed prefix, its line count and its projection (ADR-0011)."""

    identity: tuple[int, int] | None = None
    offset: int = 0
    line: int = 0
    projection: _Projection = field(default_factory=_Projection)
    error: str | None = None

    def read(self, path: Path) -> None:
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            identity = (info.st_dev, info.st_ino)
            if self.identity != identity or info.st_size < self.offset:
                self.identity = identity
                self.offset = self.line = 0
                self.projection = _Projection()
                self.error = None
            if self.error is not None:
                return
            stream.seek(self.offset)
            for raw in stream:
                # A record still being written waits for its newline, and is
                # read again from its first byte next time.
                if not raw.endswith(b"\n"):
                    break
                self.offset += len(raw)
                self.line += 1
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                    if not isinstance(record, dict):
                        raise TypeError("record is not an object")
                    if _deeper_than(record, _MAX_DEPTH):
                        # Decodable, yet too deep for anything that walks it
                        # again (`json.dumps`, a consumer's own recursion), so
                        # it would fail outside this catch (ADR-0033).
                        raise ValueError("record is nested past the bound")
                    self.projection.project(record, self.line)
                except (ValueError, TypeError, UnicodeError, RecursionError):
                    # Never the record's content in a failure: a malformed
                    # record can also hide a second claim on a call id, so the
                    # session is withheld rather than half-read.
                    self.error = f"line {self.line}: malformed rollout record; session withheld"
                    return


class CodexReader:
    """Reads Codex rollouts under `sessions/` and `archived_sessions/`, and
    places each thread's MCP connections from Codex's log database."""

    agent_kind = "codex"

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root if root is not None else default_root()).absolute()
        self._cursors: OrderedDict[Path, _Cursor] = OrderedDict()

    def locations(self) -> tuple[Path, ...]:
        """Where rollouts live. The log database and the title index are read
        again whenever a rollout is, as the Claude Code reader reads its
        connection logs, so a change to them alone surfaces on that thread's
        next read rather than through the watcher."""
        return (self._root / SESSIONS, self._root / ARCHIVED)

    def collect(self, window: Window) -> tuple[list[Session], list[ReaderFailure]]:
        try:
            root_is_dir = stat.S_ISDIR(self._root.stat().st_mode)
        except FileNotFoundError:
            return [], []
        except OSError as error:
            return [], [self._failure(f"{self._root}: {error}")]
        if not root_is_dir:
            return [], [self._failure(f"{self._root}: not a directory")]
        paths, failures = self._discover()
        titles = self._titles(failures)
        logs = CodexLogs.open(self._root)
        if logs.failure:
            failures.append(self._failure(logs.failure))
        claims: OrderedDict[str, list[Path]] = OrderedDict()
        read: dict[Path, Session] = {}
        try:
            for path in paths:
                if window.since is not None:
                    try:
                        modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
                    except (OSError, OverflowError, ValueError) as error:
                        failures.append(self._failure(f"{path}: {error}"))
                        continue
                    if modified < window.since:
                        continue
                session = self._read(path, _Cursor(), logs, titles, failures)
                if session is None:
                    continue
                if window.since is not None and (
                    session.last_activity_at is None or session.last_activity_at < window.since
                ):
                    continue
                read[path] = session
                claims.setdefault(session.session_id, []).append(path)
        finally:
            logs.close()
        sessions: list[Session] = []
        for session_id, claimants in claims.items():
            if len(claimants) > 1:
                # A rollback keeps the thread id but starts a new rollout file
                # (docs/specs/codex.md#the-rollout), so more than one file can
                # claim the same session; which one is current is not on disk.
                names = ", ".join(str(path) for path in claimants)
                failures.append(
                    self._failure(
                        f"{session_id}: claimed by {len(claimants)} rollouts ({names}); not read"
                    )
                )
                continue
            sessions.append(read[claimants[0]])
        return sessions, failures

    def collect_file(self, path: Path) -> tuple[list[Session], list[ReaderFailure]]:
        """Read one growing rollout, projecting only complete appended records,
        as the Claude Code reader reads one transcript (ADR-0011)."""
        path = path.absolute()
        if not _is_rollout(path) or not any(
            path.is_relative_to(location) for location in self.locations()
        ):
            return [], []
        cursor = self._cursors.setdefault(path, _Cursor())
        self._cursors.move_to_end(path)
        failures: list[ReaderFailure] = []
        titles = self._titles(failures)
        logs = CodexLogs.open(self._root)
        if logs.failure:
            failures.append(self._failure(logs.failure))
        try:
            session = self._read(path, cursor, logs, titles, failures)
        finally:
            logs.close()
            self._bound()
        return ([session] if session is not None else []), failures

    def _read(
        self,
        path: Path,
        cursor: _Cursor,
        logs: CodexLogs,
        titles: Mapping[str, str],
        failures: list[ReaderFailure],
    ) -> Session | None:
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                failures.append(self._failure(f"{path}: not a regular file"))
                return None
            cursor.read(path)
        except FileNotFoundError:
            self._cursors.pop(path, None)
            return None
        except OSError as error:
            failures.append(self._failure(f"{path}: {error}"))
            return None
        if cursor.error is not None:
            failures.append(self._failure(f"{path}: {cursor.error}"))
            return None
        projection = cursor.projection
        failures.extend(
            self._failure(f"{path}: {warning}") for warning in sorted(projection.warnings)
        )
        if projection.meta is None:
            return None
        placement, failure = logs.place(projection.thread_id)
        if failure is not None:
            failures.append(self._failure(failure))
        return projection.session(placement, titles.get(projection.thread_id))

    def _discover(self) -> tuple[list[Path], list[ReaderFailure]]:
        """Every rollout under the two locations, and every directory that
        could not be listed (`os.walk(onerror=...)`, never `glob()`)."""
        paths: list[Path] = []
        failures: list[ReaderFailure] = []
        for location in self.locations():
            errors: list[OSError] = []
            for dirpath, _dirnames, filenames in os.walk(location, onerror=errors.append):
                for name in filenames:
                    if not name.startswith("rollout-"):
                        continue
                    if name.endswith(".jsonl.zst"):
                        failures.append(
                            self._failure(f"{Path(dirpath) / name}: compressed rollout is not read")
                        )
                    elif name.endswith(".jsonl"):
                        paths.append(Path(dirpath) / name)
            for error in errors:
                if isinstance(error, FileNotFoundError) and Path(str(error.filename)) == location:
                    continue
                failures.append(self._failure(f"{error.filename}: {error.strerror or error}"))
        return sorted(paths), failures

    def _titles(self, failures: list[ReaderFailure]) -> dict[str, str]:
        """The newest name a person gave each thread (`session_index.jsonl`)."""
        path = self._root / INDEX
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError) as error:
            failures.append(self._failure(f"{path}: {error}; thread names are not read"))
            return {}
        titles: dict[str, str] = {}
        for line in raw.splitlines():
            try:
                entry = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(entry, dict):
                thread, name = _string(entry.get("id")), _string(entry.get("thread_name"))
                if thread and name:
                    titles[thread] = name
        return titles

    def _bound(self) -> None:
        """Evict the least recently read projections over budget, never the
        file just read (ADR-0029)."""
        while len(self._cursors) > 1 and (
            len(self._cursors) > _MAX_INCREMENTAL_FILES
            or sum(cursor.offset for cursor in self._cursors.values()) > _MAX_INCREMENTAL_BYTES
        ):
            self._cursors.popitem(last=False)

    def _failure(self, message: str) -> ReaderFailure:
        return ReaderFailure(agent_kind=self.agent_kind, message=message)


def _is_rollout(path: Path) -> bool:
    return path.name.startswith("rollout-") and path.name.endswith(".jsonl")
