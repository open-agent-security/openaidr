"""Builders for Pi session files, in the shape Pi 1.5.2 writes.

The shapes follow Pi's own `docs/session-format.md` and `docs/message-types.md`,
and the code that writes them: `SessionManager` (`dist/core/session-manager.js`),
the agent loop's tool results (`pi-agent-core/dist/agent-loop.js`) and the
nested-call record (`dist/core/nested-tool-calls.js`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

T0 = datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC)
SESSION = "3f1c2a9e-5b7d-4e21-9c0a-7d2b1e4f6a80"
CWD = "/home/dev/shop"

_USAGE = {
    "input": 1200,
    "output": 80,
    "cacheRead": 4000,
    "cacheWrite": 600,
    "totalTokens": 5880,
    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
}


def iso(seconds: int) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def ms(seconds: int) -> int:
    return int((T0 + timedelta(seconds=seconds)).timestamp() * 1000)


def header(cwd: str = CWD, session: str = SESSION, **fields: object) -> dict[str, object]:
    return {
        "type": "session",
        "version": 3,
        "id": session,
        "timestamp": iso(0),
        "cwd": cwd,
        **fields,
    }


def _entry(
    entry_id: str | None, parent: str | None, at: int, **fields: object
) -> dict[str, object]:
    record: dict[str, object] = {"parentId": parent, "timestamp": iso(at), **fields}
    if entry_id is not None:
        record = {"id": entry_id, **record}
    return record


def message(
    entry_id: str | None, parent: str | None, at: int, body: dict[str, object]
) -> dict[str, object]:
    return _entry(entry_id, parent, at, type="message", message={**body, "timestamp": ms(at)})


def user(entry_id: str | None, parent: str | None, at: int, text: str) -> dict[str, object]:
    return message(entry_id, parent, at, {"role": "user", "content": text})


def tool_call(call_id: str, name: str, **arguments: object) -> dict[str, object]:
    return {"type": "toolCall", "id": call_id, "name": name, "arguments": arguments}


def assistant(
    entry_id: str,
    parent: str | None,
    at: int,
    *content: dict[str, object] | str,
    model: str = "claude-sonnet-4-5",
    usage: dict[str, object] | None = None,
    stop: str = "toolUse",
    **fields: object,
) -> dict[str, object]:
    parts = [{"type": "text", "text": c} if isinstance(c, str) else c for c in content]
    return message(
        entry_id,
        parent,
        at,
        {
            "role": "assistant",
            "content": parts,
            "api": "anthropic-messages",
            "provider": "anthropic",
            "model": model,
            "usage": usage if usage is not None else dict(_USAGE),
            "stopReason": stop,
            **fields,
        },
    )


def tool_result(
    entry_id: str,
    parent: str | None,
    at: int,
    call_id: str,
    name: str,
    text: str,
    *,
    error: bool = False,
    **fields: object,
) -> dict[str, object]:
    return message(
        entry_id,
        parent,
        at,
        {
            "role": "toolResult",
            "toolCallId": call_id,
            "toolName": name,
            "content": [{"type": "text", "text": text}],
            "isError": error,
            **fields,
        },
    )


def write(
    root: Path, entries: list[dict[str, object]], cwd: str = CWD, session: str = SESSION
) -> Path:
    """Write a session where Pi writes it: one directory per working directory."""
    folder = root / ("--" + cwd.lstrip("/").replace("/", "-") + "--")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{iso(0).replace(':', '-').replace('.', '-')}_{session}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


def append(path: Path, *entries: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
