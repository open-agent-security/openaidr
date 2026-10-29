"""Synthetic Codex rollouts, in the record shapes Codex CLI 0.163.0 writes.

Every value here is made up. The shapes follow `docs/specs/codex.md`; real
rollouts are never committed.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

_START = datetime(2026, 10, 16, 2, 0, 0, tzinfo=UTC)


class Rollout:
    """One thread's rollout, built record by record."""

    def __init__(
        self,
        thread_id: str,
        *,
        root: str | None = None,
        parent: str | None = None,
        source: object = "exec",
        originator: str = "codex_exec",
        cwd: str = "/work/project",
        branch: str | None = "main",
        meta: dict[str, object] | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.records: list[dict[str, object]] = []
        self._ordinal = 0
        self._clock = _START
        self.turn_id: str | None = None
        payload: dict[str, object] = {
            "session_id": root or thread_id,
            "id": thread_id,
            "timestamp": _iso(self._clock),
            "cwd": cwd,
            "originator": originator,
            "cli_version": "0.163.0",
            "source": source,
            "thread_source": "subagent" if parent else "user",
            "model_provider": "openai",
            "base_instructions": {"text": "You are Codex."},
            "history_mode": "paginated",
        }
        if parent:
            payload["parent_thread_id"] = parent
        if branch:
            payload["git"] = {"commit_hash": "0" * 40, "branch": branch}
        payload.update(meta or {})
        self.add("session_meta", payload)

    # --- raw ------------------------------------------------------------

    def add(self, kind: str, payload: dict[str, object]) -> None:
        self._clock += timedelta(seconds=1)
        record: dict[str, object] = {
            "timestamp": _iso(self._clock),
            "type": kind,
            "payload": payload,
            "ordinal": self._ordinal,
        }
        self._ordinal += 1
        self.records.append(record)

    def event(self, payload: dict[str, object]) -> None:
        self.add("event_msg", payload)

    def item(self, item: dict[str, object]) -> None:
        self.event(
            {
                "type": "item_completed",
                "thread_id": self.thread_id,
                "turn_id": self.turn_id,
                "item": item,
            }
        )

    def response(self, payload: dict[str, object]) -> None:
        if self.turn_id is not None:
            payload = {
                **payload,
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": self.turn_id,
                    **payload.get("internal_chat_message_metadata_passthrough", {}),  # type: ignore[dict-item]
                },
            }
        self.add("response_item", payload)

    # --- a turn ---------------------------------------------------------

    def turn(
        self,
        turn_id: str,
        *,
        approval: str = "never",
        sandbox: str = "workspace-write",
        window: int | None = 258400,
        cwd: str = "/work/project",
    ) -> None:
        self.turn_id = turn_id
        started: dict[str, object] = {"type": "task_started", "turn_id": turn_id}
        if window is not None:
            started["model_context_window"] = window
        self.event(started)
        self.add(
            "turn_context",
            {
                "turn_id": turn_id,
                "cwd": cwd,
                "model": "gpt-5.5",
                "approval_policy": approval,
                "sandbox_policy": {"type": sandbox},
            },
        )

    def user(self, text: str) -> None:
        self.response(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
                "internal_chat_message_metadata_passthrough": {"content_item_kinds": ["user.text"]},
            }
        )
        self.item(
            {
                "type": "UserMessage",
                "id": f"um-{len(self.records)}",
                "content": [{"type": "text", "text": text}],
            }
        )

    def injected(self, text: str) -> None:
        self.response(
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
                "internal_chat_message_metadata_passthrough": {
                    "content_item_kinds": ["environments.environment_context"]
                },
            }
        )

    def developer(self, text: str) -> None:
        self.response(
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": text}],
            }
        )

    def assistant(self, text: str) -> None:
        self.response(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        )

    def call(
        self, call_id: str, name: str, arguments: dict[str, object], *, namespace: str | None = None
    ) -> None:
        payload: dict[str, object] = {
            "type": "function_call",
            "name": name,
            "arguments": json.dumps(arguments),
            "call_id": call_id,
        }
        if namespace is not None:
            payload["namespace"] = namespace
        self.response(payload)

    def output(self, call_id: str, output: object, *, kind: str = "function_call_output") -> None:
        self.response({"type": kind, "call_id": call_id, "output": output})

    def exec(
        self,
        call_id: str,
        cmd: str,
        *,
        exit_code: int | None = 0,
        output: str | None = "done\n",
        cwd: str = "/work/project",
        item: bool = True,
    ) -> None:
        """A command as Codex records one: the call, the execution item, then the output."""
        self.call(call_id, "exec_command", {"cmd": cmd, "yield_time_ms": 3000})
        if item and exit_code is not None:
            self.item(
                {
                    "type": "CommandExecution",
                    "id": call_id,
                    "command": ["/bin/bash", "-lc", cmd],
                    "cwd": f"file://{cwd}",
                    "status": "completed" if exit_code == 0 else "failed",
                    "aggregated_output": output or "",
                    "exit_code": exit_code,
                    "duration": {"secs": 1, "nanos": 500_000_000},
                }
            )
        if output is not None:
            header = (
                f"Chunk ID: abc123\nWall time: 0.1000 seconds\nProcess exited with code {exit_code}\n"
                "Original token count: 3\nOutput:\n"
                if exit_code is not None
                else ""
            )
            self.output(call_id, header + output)

    def patch(self, call_id: str, patch: str, *, status: str = "completed") -> None:
        self.response(
            {"type": "custom_tool_call", "name": "apply_patch", "input": patch, "call_id": call_id}
        )
        self.item({"type": "FileChange", "id": call_id, "changes": {}, "status": status})
        text = (
            "Exit code: 0\nWall time: 0 seconds\nOutput:\nSuccess.\n"
            if status == "completed"
            else "patch rejected by user"
        )
        self.output(call_id, text, kind="custom_tool_call_output")

    def mcp(
        self,
        call_id: str,
        server: str,
        tool: str,
        arguments: dict[str, object],
        *,
        is_error: bool = False,
    ) -> None:
        self.call(call_id, tool, arguments, namespace=f"mcp__{server}")
        self.item(
            {
                "type": "McpToolCall",
                "id": call_id,
                "server": server,
                "tool": tool,
                "arguments": arguments,
                "status": "failed" if is_error else "completed",
                "result": {
                    "content": [{"type": "text", "text": "401 Unauthorized" if is_error else "ok"}],
                    "isError": is_error,
                },
                "duration": {"secs": 0, "nanos": 2_000_000},
            }
        )
        self.output(
            call_id,
            [
                {"type": "input_text", "text": "Wall time: 0.0 seconds\nOutput:"},
                {"type": "input_text", "text": "401 Unauthorized" if is_error else "ok"},
            ],
        )

    def usage(
        self,
        response_id: str,
        *,
        input_tokens: int = 1300,
        cached: int = 650,
        output_tokens: int = 50,
        reasoning: int = 10,
        cache_write: int = 0,
    ) -> None:
        usage = {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached,
            "cache_write_input_tokens": cache_write,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning,
            "total_tokens": input_tokens + output_tokens,
        }
        self.add(
            "token_usage_record",
            {
                "thread_id": self.thread_id,
                "turn_id": self.turn_id,
                "response_id": response_id,
                "usage": usage,
                "thread_token_usage": usage,
            },
        )
        # Codex also reports the running total; a reader must never sum it.
        self.event(
            {"type": "token_count", "info": {"total_token_usage": usage, "last_token_usage": usage}}
        )

    def complete(self, *, error: str | None = None, code: str | None = None) -> None:
        payload: dict[str, object] = {"type": "task_complete", "turn_id": self.turn_id}
        if error is not None:
            payload["error"] = {"message": error, "codex_error_info": code}
        self.event(payload)

    def aborted(self) -> None:
        self.event({"type": "turn_aborted", "turn_id": self.turn_id, "reason": "interrupted"})

    # --- on disk --------------------------------------------------------

    def text(self) -> str:
        return "".join(json.dumps(record) + "\n" for record in self.records)

    def write(self, home: Path, *, archived: bool = False) -> Path:
        directory = home / ("archived_sessions" if archived else "sessions/2026/10/16")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"rollout-2026-10-16T02-00-00-{self.thread_id}.jsonl"
        path.write_text(self.text(), encoding="utf-8")
        return path


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


# --- the log database -------------------------------------------------------------

_SCHEMA = """
CREATE TABLE logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    ts_nanos INTEGER NOT NULL,
    level TEXT NOT NULL,
    target TEXT NOT NULL,
    feedback_log_body TEXT,
    module_path TEXT,
    file TEXT,
    line INTEGER,
    thread_id TEXT,
    process_uuid TEXT,
    estimated_bytes INTEGER NOT NULL DEFAULT 0
);
"""


def request_span(request_id: int) -> str:
    return (
        f'app_server.request{{otel.kind="server" otel.name="thread/start" rpc.system="jsonrpc" '
        f'rpc.method="thread/start" rpc.transport="stdio" rpc.request_id={request_id} '
        f'app_server.connection_id=0 app_server.api_version="v2"}}'
    )


def started_line(server: str, *, name: str = "srv", version: str = "1.0.0", span: str = "") -> str:
    return (
        f"{span}:session_init:session_init.mcp_manager_init:mcp.runtime.refresh:new{{server_name={server}}}"
        f":start_server_task{{server_name={server}}}:serve_inner: Service initialized as client "
        f'peer_info=Some(ServerPeerInfo {{ server_info: Some(Implementation {{ name: "{name}", '
        f'title: None, version: "{version}", description: None }}) }})'
    )


def failed_line(server: str, error: str, *, span: str = "") -> str:
    return (
        f"{span}:session_init:session_init.mcp_manager_init:mcp.runtime.refresh:new{{server_name={server}}}: "
        f'MCP server startup failed server_name="{server}" error={error}'
    )


def write_logs(
    home: Path, rows: list[tuple[str, str, str | None, str]], *, name: str = "logs_2.sqlite"
) -> Path:
    """Rows of `(target, body, thread_id, process_uuid)`, one second apart."""
    home.mkdir(parents=True, exist_ok=True)
    path = home / name
    db = sqlite3.connect(path)
    db.executescript(_SCHEMA)
    for second, (target, body, thread, process) in enumerate(rows):
        db.execute(
            "INSERT INTO logs (ts, ts_nanos, level, target, feedback_log_body, thread_id, process_uuid)"
            " VALUES (?, 0, 'INFO', ?, ?, ?, ?)",
            (1_792_116_738 + second, target, body, thread, process),
        )
    db.commit()
    db.close()
    return path
