from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from openaidr.collector import IncrementalCollector, collect
from openaidr.kinds import parse_kind_filter
from openaidr.readers.base import Window
from openaidr.readers.claude_code import ClaudeCodeReader
from openaidr.readers.claude_code_mcp import MCPLogIndex
from tests.fixtures import mcp_logs as log
from tests.fixtures.claude_jsonl import (
    assistant_tool_use,
    tool_result,
    user_text,
    write_session,
)


def _append(path: Path, *records: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _reader(root: Path) -> ClaudeCodeReader:
    return ClaudeCodeReader(
        root=root,
        mcp_logs=MCPLogIndex(by_session={}, root_found=False),
    )


def _incremental(root: Path) -> IncrementalCollector:
    return IncrementalCollector(readers=[_reader(root)])


def _mcp_calls(collection):
    return [
        call
        for session in collection.sessions
        for turn in session.turns
        for call in turn.tool_calls
        if call.mcp_server
    ]


def test_only_appended_records_are_projected_after_the_cold_read(tmp_path: Path) -> None:
    path = write_session(
        tmp_path,
        "-work-project",
        [user_text("s1", "u1", "2026-09-18T10:00:00Z", "investigate")],
    )
    reader = _reader(tmp_path)
    incremental = IncrementalCollector(readers=[reader])

    with patch.object(
        reader._parser,
        "_extract_message_data",
        wraps=reader._parser._extract_message_data,
    ) as project:
        first = incremental.collect("claude-code", path)
        _append(
            path,
            assistant_tool_use("s1", "u2", "2026-09-18T10:00:01Z", "toolu_1", "Read"),
            tool_result("s1", "u3", "2026-09-18T10:00:02Z", "toolu_1", "file contents"),
        )
        second = incremental.collect("claude-code", path)
        unchanged = incremental.collect("claude-code", path)

    assert project.call_count == 3
    assert first.sessions[0].turn_count == 1
    call = second.sessions[0].turns[1].tool_calls[0]
    assert (call.status, call.result) == ("unknown", "file contents")
    assert unchanged.sessions == second.sessions


def test_an_appended_mcp_log_outcome_updates_an_unchanged_session(
    tmp_path: Path, monkeypatch
) -> None:
    root, cache = tmp_path / "projects", tmp_path / "cache"
    path = write_session(
        root,
        "-work-project",
        [
            user_text("s1", "u1", "2026-09-18T10:00:00Z", "search"),
            assistant_tool_use(
                "s1",
                "u2",
                "2026-09-18T10:00:01Z",
                "toolu_1",
                "mcp__books__search",
            ),
            tool_result("s1", "u3", "2026-09-18T10:00:02Z", "toolu_1", "done"),
        ],
    )
    log_path = log.write_server_log(
        cache,
        "books",
        [log.connected("s1"), log.calling("s1", "search")],
    )
    monkeypatch.setenv("CLAUDE_CLI_CACHE_DIR", str(cache))
    incremental = IncrementalCollector(readers=[ClaudeCodeReader(root=root)])

    before = incremental.collect("claude-code", path)
    _append(log_path, log.completed("s1", "search"))
    original_read_text = Path.read_text

    def reject_whole_log_reread(candidate: Path, *args, **kwargs):
        if candidate == log_path:
            raise AssertionError("incremental collection reread the whole MCP log")
        return original_read_text(candidate, *args, **kwargs)

    with patch.object(Path, "read_text", reject_whole_log_reread):
        after = incremental.collect("claude-code", path)
    cold = collect(
        parse_kind_filter(["claude-code"]),
        Window(since=None),
        readers=[ClaudeCodeReader(root=root)],
    )

    assert before.failures == []
    assert after.failures == []
    assert _mcp_calls(before)[0].status == "unknown"
    assert _mcp_calls(after)[0].status == "ok"
    assert after == cold


def test_a_partial_mcp_log_record_is_held_until_its_newline_arrives(
    tmp_path: Path, monkeypatch
) -> None:
    root, cache = tmp_path / "projects", tmp_path / "cache"
    path = write_session(
        root,
        "-work-project",
        [
            assistant_tool_use(
                "s1",
                "u1",
                "2026-09-18T10:00:01Z",
                "toolu_1",
                "mcp__books__search",
            ),
            tool_result("s1", "u2", "2026-09-18T10:00:02Z", "toolu_1", "done"),
        ],
    )
    log_path = log.write_server_log(cache, "books", [log.calling("s1", "search")])
    monkeypatch.setenv("CLAUDE_CLI_CACHE_DIR", str(cache))
    incremental = IncrementalCollector(readers=[ClaudeCodeReader(root=root)])
    record = json.dumps(log.completed("s1", "search"))

    assert _mcp_calls(incremental.collect("claude-code", path))[0].status == "unknown"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(record)
    assert _mcp_calls(incremental.collect("claude-code", path))[0].status == "unknown"
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert _mcp_calls(incremental.collect("claude-code", path))[0].status == "ok"


def test_a_truncated_mcp_log_restarts_from_a_cold_state(tmp_path: Path, monkeypatch) -> None:
    root, cache = tmp_path / "projects", tmp_path / "cache"
    path = write_session(
        root,
        "-work-project",
        [
            assistant_tool_use(
                "s1",
                "u1",
                "2026-09-18T10:00:01Z",
                "toolu_1",
                "mcp__books__search",
            ),
            tool_result("s1", "u2", "2026-09-18T10:00:02Z", "toolu_1", "done"),
        ],
    )
    log_path = log.write_server_log(
        cache,
        "books",
        [log.calling("s1", "search"), log.failed("s1", "search", ms=1200)],
    )
    monkeypatch.setenv("CLAUDE_CLI_CACHE_DIR", str(cache))
    incremental = IncrementalCollector(readers=[ClaudeCodeReader(root=root)])

    assert _mcp_calls(incremental.collect("claude-code", path))[0].status == "error"
    log_path.write_text(json.dumps(log.completed("s1", "search")) + "\n", encoding="utf-8")
    assert _mcp_calls(incremental.collect("claude-code", path))[0].status == "ok"


def test_a_partial_last_record_is_held_until_its_newline_arrives(tmp_path: Path) -> None:
    path = write_session(
        tmp_path,
        "-work-project",
        [user_text("s1", "u1", "2026-09-18T10:00:00Z", "first message")],
    )
    incremental = _incremental(tmp_path)
    record = json.dumps(user_text("s1", "u2", "2026-09-18T10:00:01Z", "second message"))

    assert incremental.collect("claude-code", path).sessions[0].turn_count == 1
    with path.open("a", encoding="utf-8") as handle:
        handle.write(record)
    assert incremental.collect("claude-code", path).sessions[0].turn_count == 1
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    assert incremental.collect("claude-code", path).sessions[0].turn_count == 2


def test_a_partial_tool_result_does_not_update_its_call_before_the_newline(
    tmp_path: Path,
) -> None:
    path = write_session(
        tmp_path,
        "-work-project",
        [assistant_tool_use("s1", "u1", "2026-09-18T10:00:00Z", "toolu_1", "Read")],
    )
    incremental = _incremental(tmp_path)
    result = json.dumps(tool_result("s1", "u2", "2026-09-18T10:00:01Z", "toolu_1", "file contents"))

    assert incremental.collect("claude-code", path).sessions[0].turns[0].tool_calls[0].status == (
        "pending"
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(result)
    assert incremental.collect("claude-code", path).sessions[0].turns[0].tool_calls[0].status == (
        "pending"
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    call = incremental.collect("claude-code", path).sessions[0].turns[0].tool_calls[0]
    assert (call.status, call.result) == ("unknown", "file contents")


def test_a_truncated_transcript_restarts_from_a_cold_state(tmp_path: Path) -> None:
    path = write_session(
        tmp_path,
        "-work-project",
        [
            user_text("s1", "u1", "2026-09-18T10:00:00Z", "one"),
            user_text("s1", "u2", "2026-09-18T10:00:01Z", "two"),
        ],
    )
    incremental = _incremental(tmp_path)
    assert incremental.collect("claude-code", path).sessions[0].turn_count == 2

    path.write_text(
        json.dumps(user_text("s1", "u9", "2026-09-18T11:00:00Z", "replacement")) + "\n",
        encoding="utf-8",
    )

    session = incremental.collect("claude-code", path).sessions[0]
    assert [turn.text for turn in session.turns] == ["replacement"]


def test_incremental_and_cold_collection_produce_the_same_session(tmp_path: Path) -> None:
    path = write_session(
        tmp_path,
        "-work-project",
        [
            user_text("s1", "u1", "2026-09-18T10:00:00Z", "investigate"),
            assistant_tool_use("s1", "u2", "2026-09-18T10:00:01Z", "toolu_1", "Read"),
            tool_result("s1", "u3", "2026-09-18T10:00:02Z", "toolu_1", "file contents"),
        ],
    )

    incremental = _incremental(tmp_path).collect("claude-code", path)
    cold = collect(
        parse_kind_filter(["claude-code"]),
        Window(since=None),
        readers=[_reader(tmp_path)],
    )

    assert incremental.failures == cold.failures
    assert incremental.sessions == cold.sessions


def test_incremental_matches_cold_for_identical_calls_in_one_turn(tmp_path: Path) -> None:
    first = assistant_tool_use("s1", "u1", "2026-09-18T10:00:00Z", "toolu_1", "Read")
    first["message"]["content"].append(
        {"type": "tool_use", "id": "toolu_2", "name": "Read", "input": {}}
    )
    path = write_session(
        tmp_path,
        "-work-project",
        [
            first,
            tool_result("s1", "u2", "2026-09-18T10:00:01Z", "toolu_1", "first contents"),
            tool_result("s1", "u3", "2026-09-18T10:00:02Z", "toolu_2", "second contents"),
        ],
    )

    incremental = _incremental(tmp_path).collect("claude-code", path)
    cold = collect(
        parse_kind_filter(["claude-code"]),
        Window(since=None),
        readers=[_reader(tmp_path)],
    )

    assert incremental.sessions == cold.sessions


def test_a_differently_spelled_path_is_not_a_second_claimant(tmp_path: Path) -> None:
    path = write_session(
        tmp_path,
        "-work-project",
        [user_text("s1", "u1", "2026-09-18T10:00:00Z", "investigate")],
    )
    reader = _reader(tmp_path)
    relative = Path(os.path.relpath(path, Path.cwd()))

    sessions, failures = reader.collect_file(relative)

    assert failures == []
    assert sessions[0].mcp_log_state == "no_log_root"


def test_collect_file_reports_an_unreadable_mcp_log(tmp_path: Path) -> None:
    path = write_session(
        tmp_path,
        "-work-project",
        [user_text("s1", "u1", "2026-09-18T10:00:00Z", "investigate")],
    )
    reader = ClaudeCodeReader(
        root=tmp_path,
        mcp_logs=MCPLogIndex(by_session={}, root_found=True, unreadable=("bad.jsonl",)),
    )

    sessions, failures = reader.collect_file(path)

    assert len(sessions) == 1
    assert len(failures) == 1
    assert "bad.jsonl" in failures[0].message


def test_an_unknown_agent_kind_is_a_reported_failure(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    path.write_text("", encoding="utf-8")

    collection = IncrementalCollector(root=tmp_path).collect("cursor", path)

    assert collection.sessions == []
    assert len(collection.failures) == 1
    assert "cursor" in collection.failures[0].message


def test_an_incremental_reader_failure_is_reported_not_raised(tmp_path: Path) -> None:
    class ExplodingReader:
        agent_kind = "claude-code"

        def collect_file(self, path: Path):
            raise RuntimeError("transcript changed while reading")

    path = tmp_path / "session.jsonl"
    path.write_text("", encoding="utf-8")

    collection = IncrementalCollector(readers=[ExplodingReader()]).collect("claude-code", path)

    assert collection.sessions == []
    assert len(collection.failures) == 1
    assert "changed while reading" in collection.failures[0].message
