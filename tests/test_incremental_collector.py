from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from openaidr.collector import IncrementalCollector, collect
from openaidr.kinds import parse_kind_filter
from openaidr.readers.base import Window
from openaidr.readers.claude_code import ClaudeCodeReader
from openaidr.readers.claude_code_mcp import MCPLogIndex
from openaidr.readers.devin_cli import DevinCliReader
from tests.fixtures import devin_store
from tests.fixtures import mcp_logs as log
from tests.fixtures.claude_jsonl import (
    assistant_response,
    assistant_tool_use,
    tool_result,
    user_text,
    write_session,
    write_subagent_session,
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

    collection = IncrementalCollector(root=tmp_path).collect("windsurf", path)

    assert collection.sessions == []
    assert len(collection.failures) == 1
    assert "windsurf" in collection.failures[0].message


def test_an_incremental_reader_failure_is_reported_not_raised(tmp_path: Path) -> None:
    class ExplodingReader:
        agent_kind = "claude-code"

        def collect_file(self, path: Path):
            raise RuntimeError("transcript changed while reading")

        def locations(self) -> tuple[Path, ...]:
            return (tmp_path,)

    path = tmp_path / "session.jsonl"
    path.write_text("", encoding="utf-8")

    collection = IncrementalCollector(readers=[ExplodingReader()]).collect("claude-code", path)

    assert collection.sessions == []
    assert len(collection.failures) == 1
    assert "changed while reading" in collection.failures[0].message


def _folds(monkeypatch) -> list[tuple[str, str]]:
    """Record the `(project, server)` of every log file the index build parses."""
    from openaidr.readers import claude_code_mcp

    folded: list[tuple[str, str]] = []
    original = claude_code_mcp._read_file

    def counting(lines, server, project, by_session):
        folded.append((project, server))
        return original(lines, server, project, by_session)

    monkeypatch.setattr(claude_code_mcp, "_read_file", counting)
    return folded


def test_an_unchanged_mcp_cache_reuses_the_previous_index(tmp_path: Path, monkeypatch) -> None:
    from openaidr.readers.claude_code_mcp import _IncrementalMCPLogReader

    cache = tmp_path / "cache"
    log.write_server_log(cache, "books", [log.calling("s1", "search")])
    log.write_server_log(cache, "files", [log.connected("s1")])
    reader = _IncrementalMCPLogReader(roots=(cache,))
    folded = _folds(monkeypatch)

    first = reader.read()
    parsed_first = len(folded)
    second = reader.read()

    assert parsed_first == 2
    assert second is first
    assert len(folded) == parsed_first


def test_an_mcp_cache_change_rebuilds_the_index(tmp_path: Path, monkeypatch) -> None:
    from openaidr.readers.claude_code_mcp import _IncrementalMCPLogReader

    cache = tmp_path / "cache"
    books = log.write_server_log(cache, "books", [log.calling("s1", "search")])
    reader = _IncrementalMCPLogReader(roots=(cache,))
    first = reader.read()

    _append(books, log.completed("s1", "search"))
    appended = reader.read()
    files = log.write_server_log(cache, "files", [log.connected("s1")])
    added = reader.read()
    files.unlink()
    removed = reader.read()

    assert appended is not first
    assert [
        o.ok for o in appended.by_session[("-work-project", "s1")].outcomes[("books", "search")]
    ] == [True]
    assert added is not appended
    assert "files" in added.by_session[("-work-project", "s1")].connections
    assert removed is not added
    assert "files" not in removed.by_session[("-work-project", "s1")].connections


def test_a_partial_mcp_record_alone_does_not_rebuild_the_index(tmp_path: Path) -> None:
    from openaidr.readers.claude_code_mcp import _IncrementalMCPLogReader

    cache = tmp_path / "cache"
    books = log.write_server_log(cache, "books", [log.calling("s1", "search")])
    reader = _IncrementalMCPLogReader(roots=(cache,))
    first = reader.read()

    with books.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(log.completed("s1", "search")))
    partial = reader.read()
    with books.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    complete = reader.read()

    assert partial is first
    assert complete is not first


def test_an_mcp_log_that_has_not_grown_is_not_reopened(tmp_path: Path) -> None:
    from openaidr.readers.claude_code_mcp import _IncrementalMCPLogReader

    cache = tmp_path / "cache"
    books = log.write_server_log(cache, "books", [log.calling("s1", "search")])
    reader = _IncrementalMCPLogReader(roots=(cache,))
    first = reader.read()
    original_open = Path.open

    def reject_reopen(candidate: Path, *args, **kwargs):
        if candidate == books:
            raise AssertionError("an MCP log with no new bytes was reopened")
        return original_open(candidate, *args, **kwargs)

    with patch.object(Path, "open", reject_reopen):
        second = reader.read()

    assert second is first


def test_a_persistently_unreadable_zero_byte_log_stays_unreadable(tmp_path: Path) -> None:
    from openaidr.readers.claude_code_mcp import _IncrementalMCPLogReader

    cache = tmp_path / "cache"
    books = log.write_server_log(cache, "books", [])
    books.write_bytes(b"")
    reader = _IncrementalMCPLogReader(roots=(cache,))
    original_open = Path.open

    def deny(candidate: Path, *args, **kwargs):
        if candidate == books:
            raise PermissionError("denied")
        return original_open(candidate, *args, **kwargs)

    with patch.object(Path, "open", deny):
        first = reader.read()
        second = reader.read()

    assert str(books) in first.unreadable
    assert ("-work-project", "books") in first.incomplete_project_servers
    assert str(books) in second.unreadable
    assert ("-work-project", "books") in second.incomplete_project_servers


def test_an_undecodable_mcp_log_is_reported_without_rebuilding_each_pass(
    tmp_path: Path, monkeypatch
) -> None:
    from openaidr.readers.claude_code_mcp import _IncrementalMCPLogReader

    cache = tmp_path / "cache"
    log.write_server_log(cache, "books", [log.calling("s1", "search")])
    broken = log.write_server_log(cache, "files", [log.connected("s1")])
    broken.write_bytes(b"\xff\xfe not utf-8\n")
    reader = _IncrementalMCPLogReader(roots=(cache,))
    folded = _folds(monkeypatch)

    first = reader.read()
    parsed_first = len(folded)
    second = reader.read()

    assert str(broken) in first.unreadable
    assert ("-work-project", "files") in first.incomplete_project_servers
    assert second is first
    assert len(folded) == parsed_first


def test_13_a_response_is_revised_as_its_final_record_arrives(tmp_path: Path) -> None:
    """Response identity is the update key: a read reports the newest committed
    record of each response, so a later read revises it, never appends it. A
    partial trailing record contributes nothing until its newline."""
    path = write_session(
        tmp_path,
        "-work-project",
        [
            user_text("s1", "u1", "2026-10-06T10:00:00Z", "go"),
            assistant_response("s1", "a1", "2026-10-06T10:00:01Z", "msg_1", {"output_tokens": 5}),
        ],
    )
    incremental = _incremental(tmp_path)

    def responses():
        collection = incremental.collect("claude-code", path)
        return [
            (r.response_id, r.final, r.usage.output_tokens)
            for r in collection.sessions[0].responses
        ]

    assert responses() == [("msg_1", False, 5)]
    final = json.dumps(
        assistant_response(
            "s1",
            "a2",
            "2026-10-06T10:00:02Z",
            "msg_1",
            {"output_tokens": 80},
            stop_reason="end_turn",
        )
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(final[:40])
    assert responses() == [("msg_1", False, 5)]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(final[40:] + "\n")
    assert responses() == [("msg_1", True, 80)]

    cold = collect(
        parse_kind_filter(["claude-code"]),
        Window(since=None),
        readers=[_reader(tmp_path)],
    )
    assert incremental.collect("claude-code", path).sessions == cold.sessions


def test_a_watcher_given_only_the_locations_finds_every_kinds_sessions(tmp_path: Path) -> None:
    """What `locations` promises: walk them, hand every file to its kind, and
    every session -- a sub-agent's included -- comes back, while a file the
    kind does not read is ignored rather than failed."""
    claude_root = tmp_path / "claude"
    devin_root = tmp_path / "devin"
    write_session(claude_root, "-p", [user_text("s1", "u1", "2026-09-18T10:00:00Z", "investigate")])
    write_subagent_session(
        claude_root, "-p", "s1", "abc", [user_text("s1", "u9", "2026-09-18T10:00:02Z", "delegated")]
    )
    store = devin_store.create_store(devin_root)
    devin_store.write_session(
        store,
        devin_store.StoredSession(
            id="otter",
            nodes=[devin_store.Node(1, None, devin_store.user("x"))],
            main_chain_id=1,
        ),
    )
    # Non-UTF-8 and newline-terminated, so a reader that only filters by
    # `.jsonl` suffix *after* decoding -- rather than before reading at all --
    # would surface this as a reader failure instead of silently ignoring it.
    (claude_root / "-p" / "notes.txt").write_bytes(b"not a transcript \xff\n")
    (devin_root / "settings.json").write_text("{}", encoding="utf-8")
    collector = IncrementalCollector(
        readers=[_reader(claude_root), DevinCliReader(root=devin_root)]
    )

    found: set[str] = set()
    for kind, directories in collector.locations().items():
        for directory in directories:
            for path in sorted(p for p in directory.rglob("*") if p.is_file()):
                collection = collector.collect(kind, path)
                assert collection.failures == [], path
                found |= {s.session_id for s in collection.sessions}

    assert found == {"claude-code:s1", "claude-code:s1:agent-abc", "devin:otter"}


def test_each_kinds_root_reaches_its_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    collector = IncrementalCollector(
        tmp_path / "claude",
        devin_root=tmp_path / "devin",
        codex_root=tmp_path / "codex",
        cursor_root=tmp_path / "cursor",
    )
    assert collector.locations() == {
        "claude-code": (tmp_path / "claude",),
        "devin": ((tmp_path / "devin").resolve(),),
        "codex": (tmp_path / "codex" / "sessions", tmp_path / "codex" / "archived_sessions"),
        "cursor": (
            tmp_path / "cursor" / "chats",
            tmp_path / "cursor" / "projects",
            tmp_path / "tmp" / f"cursor-agent-logs-{os.getuid()}",
        ),
    }


def test_incremental_cache_releases_least_recently_used_transcripts(tmp_path: Path) -> None:
    import gc
    import weakref

    paths = [
        write_session(
            tmp_path,
            f"-work-project-{i}",
            [user_text(f"s{i}", f"u{i}", "2026-09-18T10:00:00Z", "investigate")],
        )
        for i in range(3)
    ]
    reader = _reader(tmp_path)
    with patch("openaidr.readers.claude_code._MAX_INCREMENTAL_FILES", 2):
        reader.collect_file(paths[0])
        reader.collect_file(paths[1])
        evicted = weakref.ref(reader._incremental[paths[1]])
        reader.collect_file(paths[0])  # Recently used, so keep this one.
        reader.collect_file(paths[2])
        gc.collect()
        assert evicted() is None
        assert set(reader._incremental) == {paths[0], paths[2]}


def test_incremental_cache_keeps_an_oversized_active_transcript_cached(tmp_path: Path) -> None:
    path = write_session(
        tmp_path,
        "-work-project",
        [
            user_text("s1", "u1", "2026-09-18T10:00:00Z", "investigate"),
            assistant_tool_use("s1", "u2", "2026-09-18T10:00:01Z", "call1", "Read"),
        ],
    )
    reader = _reader(tmp_path)
    with patch("openaidr.readers.claude_code._MAX_INCREMENTAL_BYTES", 1):
        before, failures = reader.collect_file(path)
        assert not failures
        assert path in reader._incremental
        projection = reader._incremental[path]
        _append(path, tool_result("s1", "u3", "2026-09-18T10:00:02Z", "call1", "contents"))
        after, failures = reader.collect_file(path)
        assert not failures
        # Still the same projection: the second read continued from its cursor
        # rather than being rebuilt from byte zero because it exceeds the budget.
        assert reader._incremental[path] is projection
        cold, failures = _reader(tmp_path).collect(Window(since=None))
        assert not failures
        assert after == cold
        assert after[0].turns[1].tool_calls[0].span == before[0].turns[1].tool_calls[0].span


def test_incremental_cache_bounds_combined_source_bytes(tmp_path: Path) -> None:
    paths = [
        write_session(
            tmp_path,
            f"-work-project-{i}",
            [user_text(f"s{i}", f"u{i}", "2026-09-18T10:00:00Z", "investigate")],
        )
        for i in range(3)
    ]
    reader = _reader(tmp_path)
    budget = paths[0].stat().st_size + paths[1].stat().st_size
    with patch("openaidr.readers.claude_code._MAX_INCREMENTAL_BYTES", budget):
        for path in paths:
            sessions, failures = reader.collect_file(path)
            assert sessions and not failures
        assert set(reader._incremental) == set(paths[1:])


def test_evicted_partial_record_is_committed_only_when_completed(tmp_path: Path) -> None:
    path = write_session(
        tmp_path,
        "-work-project",
        [user_text("s1", "u1", "2026-09-18T10:00:00Z", "investigate")],
    )
    appended = json.dumps(
        assistant_tool_use("s1", "u2", "2026-09-18T10:00:01Z", "call1", "Read")
    ).encode()
    with path.open("ab") as handle:
        handle.write(appended)
    other = write_session(
        tmp_path,
        "-work-project-2",
        [user_text("s2", "u3", "2026-09-18T10:00:00Z", "investigate")],
    )
    reader = _reader(tmp_path)
    with patch("openaidr.readers.claude_code._MAX_INCREMENTAL_FILES", 1):
        sessions, failures = reader.collect_file(path)
        assert not failures
        assert sessions[0].turn_count == 1
        assert path in reader._incremental
        # Reading a second file evicts `path`, the now-least-recently-used entry,
        # while its trailing record is still incomplete.
        other_sessions, failures = reader.collect_file(other)
        assert other_sessions and not failures
        assert path not in reader._incremental
        with path.open("ab") as handle:
            handle.write(b"\n")
        sessions, failures = reader.collect_file(path)
        assert not failures
        cold, failures = _reader(tmp_path).collect_file(path)
        assert not failures
        assert sessions == cold
        assert sessions[0].turn_count == 2
