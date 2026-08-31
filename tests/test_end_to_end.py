"""One realistic session, end to end, through the installed console script."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.fixtures.claude_jsonl import assistant_tool_use, tool_result, user_text, write_session


def test_a_realistic_session_renders_the_statuses_this_wrapper_can_emit(tmp_path: Path) -> None:
    """`interrupted` is a legal `Status` for which this reader has no trustworthy
    signal and never emits; the other four are covered. `error` here is *read*
    from the result's own `is_error`, not inferred from its wording."""
    denied = tool_result(
        "s1",
        "u6",
        "2026-08-01T10:00:05.000Z",
        "toolu_3",
        "The user doesn't want to proceed with this tool use.",
        is_error=True,
    )
    denied["toolDenialKind"] = "user-rejected"
    write_session(
        tmp_path,
        "-work-project",
        [
            user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "look at the repo"),
            assistant_tool_use(
                "s1", "u2", "2026-08-01T10:00:01.000Z", "toolu_1", "mcp__github__get_issue"
            ),
            tool_result("s1", "u3", "2026-08-01T10:00:01.500Z", "toolu_1", "issue body"),
            assistant_tool_use("s1", "u4", "2026-08-01T10:00:02.000Z", "toolu_2", "Bash"),
            tool_result(
                "s1", "u5", "2026-08-01T10:00:03.000Z", "toolu_2", "Exit code 1", is_error=True
            ),
            assistant_tool_use("s1", "u6b", "2026-08-01T10:00:04.000Z", "toolu_3", "Bash"),
            denied,
            assistant_tool_use("s1", "u7", "2026-08-01T10:00:06.000Z", "toolu_4", "Read"),
        ],
    )

    result = subprocess.run(
        [sys.executable, "-m", "openaidr", "sessions", "--since", "36500d", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
        # The subprocess must still be able to import `openaidr` from this
        # environment's installed venv, so the override adds to the current
        # environment rather than replacing it outright.
        env={**os.environ, "OPENAIDR_CLAUDE_ROOT": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr

    document = json.loads(result.stdout)
    calls = [c for t in document["sessions"][0]["turns"] for c in t["tool_calls"]]
    assert [c["status"] for c in calls] == ["unknown", "error", "rejected", "pending"]
    assert calls[0]["mcp_server"] == "github"
    assert calls[3]["result_size"] is None


def test_the_json_document_carries_no_conversation_text(tmp_path: Path) -> None:
    """Rendered JSON is an account of activity, not a transcript dump."""
    write_session(
        tmp_path, "-p", [user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "my secret prompt")]
    )
    result = subprocess.run(
        [sys.executable, "-m", "openaidr", "sessions", "--since", "36500d", "--format", "json"],
        capture_output=True,
        text=True,
        check=False,
        # The subprocess must still be able to import `openaidr` from this
        # environment's installed venv, so the override adds to the current
        # environment rather than replacing it outright.
        env={**os.environ, "OPENAIDR_CLAUDE_ROOT": str(tmp_path)},
    )
    assert "my secret prompt" not in result.stdout
