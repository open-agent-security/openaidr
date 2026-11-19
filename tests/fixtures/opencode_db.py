"""Builders for OpenCode's session database, in the shape OpenCode 1.19.5 writes.

The tables are those its migrations create (`packages/core/src/database/
migration/`), in the shape `packages/core/src/session/sql.ts` declares. The
`data` column of a message and a part is the message's or part's own JSON, with
its ids left out, as `SessionProjector` writes it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

T0 = datetime(2026, 11, 10, 9, 0, 0, tzinfo=UTC)
ROOT = "ses_7f3a9c0b2e1dQx8Lm4Np6Rt2Vw"
CHILD = "ses_7f3a9c0b2e1cAa1Bb2Cc3Dd4Ee"
DIRECTORY = "/home/dev/shop"

SCHEMA = """
CREATE TABLE project (
  id text PRIMARY KEY, worktree text NOT NULL, vcs text, name text, icon_url text,
  icon_url_override text, icon_color text, time_created integer NOT NULL,
  time_updated integer NOT NULL, time_initialized integer, sandboxes text NOT NULL,
  commands text
);
CREATE TABLE session (
  id text PRIMARY KEY, project_id text NOT NULL, workspace_id text, parent_id text,
  slug text NOT NULL, directory text NOT NULL, path text, title text NOT NULL,
  version text NOT NULL, share_url text, summary_additions integer,
  summary_deletions integer, summary_files integer, summary_diffs text, metadata text,
  cost real DEFAULT 0 NOT NULL, tokens_input integer DEFAULT 0 NOT NULL,
  tokens_output integer DEFAULT 0 NOT NULL, tokens_reasoning integer DEFAULT 0 NOT NULL,
  tokens_cache_read integer DEFAULT 0 NOT NULL, tokens_cache_write integer DEFAULT 0 NOT NULL,
  revert text, permission text, agent text, model text, time_created integer NOT NULL,
  time_updated integer NOT NULL, time_compacting integer, time_archived integer
);
CREATE TABLE message (
  id text PRIMARY KEY, session_id text NOT NULL, time_created integer NOT NULL,
  time_updated integer NOT NULL, data text NOT NULL
);
CREATE TABLE part (
  id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
  time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL
);
CREATE INDEX part_message_id_id_idx ON part (message_id, id);
CREATE INDEX part_session_idx ON part (session_id);
"""


def ms(seconds: float) -> int:
    return int((T0 + timedelta(seconds=seconds)).timestamp() * 1000)


def create(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO project VALUES ('prj_1', ?, 'git', NULL, NULL, NULL, NULL, ?, ?, NULL, '[]', NULL)",
        (DIRECTORY, ms(0), ms(0)),
    )
    connection.commit()
    return connection


def session(
    db: sqlite3.Connection,
    session_id: str = ROOT,
    *,
    parent: str | None = None,
    title: str = "Fix the checkout bug",
    updated: float = 600,
) -> None:
    db.execute(
        "INSERT INTO session (id, project_id, parent_id, slug, directory, title, version, "
        "model, time_created, time_updated) VALUES (?, 'prj_1', ?, 'calm-otter', ?, ?, "
        "'1.19.5', ?, ?, ?)",
        (session_id, parent, DIRECTORY, title, None, ms(0), ms(updated)),
    )
    db.commit()


def user(db: sqlite3.Connection, message_id: str, at: float, session_id: str = ROOT) -> None:
    data = {
        "role": "user",
        "time": {"created": ms(at)},
        "agent": "build",
        "model": {"providerID": "anthropic", "modelID": "claude-sonnet-4-5"},
    }
    _message(db, message_id, session_id, at, data)


def assistant(
    db: sqlite3.Connection,
    message_id: str,
    at: float,
    parent: str,
    session_id: str = ROOT,
    **fields: object,
) -> None:
    data = {
        "role": "assistant",
        "time": {"created": ms(at), "completed": ms(at + 5)},
        "parentID": parent,
        "modelID": "claude-sonnet-4-5",
        "providerID": "anthropic",
        "mode": "build",
        "agent": "build",
        "path": {"cwd": DIRECTORY, "root": DIRECTORY},
        "cost": 0,
        "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        **fields,
    }
    _message(db, message_id, session_id, at, data)


def _message(
    db: sqlite3.Connection, message_id: str, session_id: str, at: float, data: dict[str, object]
) -> None:
    db.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?, ?)",
        (message_id, session_id, ms(at), ms(at), json.dumps(data)),
    )
    db.commit()


def part(
    db: sqlite3.Connection,
    part_id: str,
    message_id: str,
    at: float,
    data: dict[str, object],
    session_id: str = ROOT,
) -> None:
    db.execute(
        "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
        "data = excluded.data, time_updated = excluded.time_updated",
        (part_id, message_id, session_id, ms(at), ms(at), json.dumps(data)),
    )
    db.commit()


def text(value: str, **fields: object) -> dict[str, object]:
    return {"type": "text", "text": value, **fields}


def tool(
    call_id: str,
    tool_name: str,
    status: str,
    /,
    *,
    output: str = "",
    error: str = "",
    metadata: dict[str, object] | None = None,
    **arguments: object,
) -> dict[str, object]:
    state: dict[str, object] = {"status": status, "input": arguments}
    if status == "completed":
        state |= {
            "output": output,
            "title": tool_name,
            "metadata": metadata or {},
            "time": {"start": ms(1), "end": ms(1.25)},
        }
    elif status == "error":
        state |= {"error": error, "time": {"start": ms(1), "end": ms(1.25)}}
        if metadata is not None:
            state["metadata"] = metadata
    elif status == "running":
        state |= {"time": {"start": ms(1)}}
    else:
        state["raw"] = ""
    return {"type": "tool", "callID": call_id, "tool": tool_name, "state": state}


def step_finish(
    input_tokens: int = 1200,
    output: int = 60,
    reasoning: int = 20,
    read: int = 4000,
    write: int = 300,
) -> dict[str, object]:
    return {
        "type": "step-finish",
        "reason": "tool-calls",
        "cost": 0,
        "tokens": {
            "input": input_tokens,
            "output": output,
            "reasoning": reasoning,
            "cache": {"read": read, "write": write},
        },
    }
