"""Builders for Claude Code JSONL records, shaped like the real files.

Field names here mirror what Claude Code writes: `sessionId`, `uuid`,
`parentUuid`, `timestamp`, `isSidechain`, `cwd`, `message`, `toolUseResult`,
`toolDenialKind`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def user_text(session_id: str, uuid: str, timestamp: str, text: str, **extra: Any) -> dict:
    return {
        "type": "user",
        "sessionId": session_id,
        "uuid": uuid,
        "timestamp": timestamp,
        "isSidechain": False,
        "cwd": "/work/project",
        "version": "2.1.247",
        "message": {"role": "user", "content": text},
        **extra,
    }


def assistant_tool_use(
    session_id: str,
    uuid: str,
    timestamp: str,
    call_id: str,
    name: str,
    arguments: dict | None = None,
    **extra: Any,
) -> dict:
    return {
        "type": "assistant",
        "sessionId": session_id,
        "uuid": uuid,
        "timestamp": timestamp,
        "isSidechain": False,
        "cwd": "/work/project",
        "message": {
            "role": "assistant",
            "model": "claude-opus-5",
            "content": [
                {"type": "tool_use", "id": call_id, "name": name, "input": arguments or {}}
            ],
        },
        **extra,
    }


def tool_result(
    session_id: str,
    uuid: str,
    timestamp: str,
    call_id: str,
    content: str = "ok",
    is_error: bool = False,
    **extra: Any,
) -> dict:
    return {
        "type": "user",
        "sessionId": session_id,
        "uuid": uuid,
        "timestamp": timestamp,
        "isSidechain": False,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": call_id,
                    "content": content,
                    **({"is_error": True} if is_error else {}),
                }
            ],
        },
        **extra,
    }


def write_session(root: Path, project: str, records: list[dict]) -> Path:
    """Write records to `<root>/<project>/<sessionId>.jsonl`, as Claude Code does."""
    session_id = records[0]["sessionId"]
    directory = root / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def write_subagent_session(
    root: Path, project: str, parent_session_id: str, agent_id: str, records: list[dict]
) -> Path:
    """Write a subagent transcript to `<root>/<project>/<parentSessionId>/subagents/agent-<id>.jsonl`.

    This is the nested layout Claude Code uses for a dispatched subagent's own
    transcript, one level below the parent's top-level `<sessionId>.jsonl`.
    """
    directory = root / project / parent_session_id / "subagents"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"agent-{agent_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def attachment(session_id: str, uuid: str, timestamp: str, body: dict, **extra: Any) -> dict:
    """An attachment record: material injected outside the turn structure."""
    return {
        "type": "attachment",
        "sessionId": session_id,
        "uuid": uuid,
        "timestamp": timestamp,
        "isSidechain": False,
        "cwd": "/work/project",
        "attachment": body,
        **extra,
    }


def system_event(session_id: str, uuid: str, timestamp: str, subtype: str, **extra: Any) -> dict:
    """A `system` record — a compaction boundary or a provider refusal."""
    return {
        "type": "system",
        "sessionId": session_id,
        "uuid": uuid,
        "timestamp": timestamp,
        "isSidechain": False,
        "subtype": subtype,
        **extra,
    }


def last_prompt(session_id: str, prompt: str) -> dict:
    """The initiating request, recorded apart from the turns."""
    return {"type": "last-prompt", "sessionId": session_id, "lastPrompt": prompt}


def custom_title(session_id: str, title: str) -> dict:
    """The name the client shows for a session, set by the user or by the agent."""
    return {"type": "custom-title", "sessionId": session_id, "customTitle": title}
