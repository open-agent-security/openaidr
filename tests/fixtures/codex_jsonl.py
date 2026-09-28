"""Synthetic Codex rollout records; never captured conversation material."""

from __future__ import annotations

import json
from pathlib import Path


def record(kind: str, payload: dict[str, object], second: int = 0) -> dict[str, object]:
    return {"timestamp": f"2026-09-01T10:00:{second:02d}Z", "type": kind, "payload": payload}


def meta(sid: str = "s1", **extra: object) -> dict[str, object]:
    return record(
        "session_meta",
        {"id": sid, "cwd": "/synthetic/project", "cli_version": "0.1", "source": "cli", **extra},
    )


def message(text: str = "synthetic prompt", role: str = "user") -> dict[str, object]:
    return record(
        "response_item",
        {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]},
        1,
    )


def call(
    cid: str = "call1", *, custom: bool = False, name: str = "exec_command"
) -> dict[str, object]:
    return record(
        "response_item",
        {
            "type": "custom_tool_call" if custom else "function_call",
            "call_id": cid,
            "name": name,
            **(
                {"input": "synthetic input"}
                if custom
                else {"arguments": '{"cmd":"synthetic command"}'}
            ),
        },
        2,
    )


def output(
    cid: str = "call1", body: object = "synthetic result", *, custom: bool = False
) -> dict[str, object]:
    return record(
        "response_item",
        {
            "type": "custom_tool_call_output" if custom else "function_call_output",
            "call_id": cid,
            "output": body,
        },
        3,
    )


def write_rollout(root: Path, records: list[dict[str, object]], name: str = "s1") -> Path:
    path = root / "2026" / "09" / "01" / f"rollout-2026-09-01T10-00-00-{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path
