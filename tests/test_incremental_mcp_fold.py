"""The incremental MCP log reader folds only what was appended, and always
produces the index a cold read of the same bytes would (ADR-0013)."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from openaidr.readers import claude_code_mcp
from openaidr.readers.claude_code_mcp import _IncrementalMCPLogReader, read_mcp_logs
from tests.fixtures import mcp_logs as log


def _log_path(cache: Path, project: str, server: str, name: str) -> Path:
    directory = cache / project / f"mcp-logs-{server}"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / name


def _line(record: dict) -> bytes:
    return (json.dumps(record) + "\n").encode()


def _script(rng: random.Random) -> list[bytes]:
    """One log file's worth of records: connections, overlapping calls,
    waits, failures, blank lines and the odd undecodable record."""
    lines: list[bytes] = []
    for _ in range(rng.randint(3, 12)):
        session = rng.choice(["s1", "s2", "s3"])
        tool = rng.choice(["search", "fetch"])
        kind = rng.random()
        if kind < 0.1:
            lines.append(_line(log.connected(session)))
        elif kind < 0.15:
            lines.append(b"not json at all\n")
        elif kind < 0.2:
            lines.append(b"\n")
        elif kind < 0.45:
            lines.append(_line(log.calling(session, tool)))
        elif kind < 0.65:
            lines.append(_line(log.completed(session, tool, ms=rng.randint(1, 99))))
        elif kind < 0.8:
            lines.append(_line(log.failed(session, tool, ms=rng.randint(1, 99))))
        else:
            lines.append(_line(log.still_running(session, tool, seconds=rng.randint(1, 60))))
    return lines


def _at_a_record_boundary(path: Path) -> bool:
    """Whether a cold read and an incremental read are meant to agree on this
    file. The cold reader counts an unterminated fragment as a line; the
    incremental one waits for its newline (ADR-0012), so while a fragment is
    pending they can disagree about which record is last."""
    data = path.read_bytes()
    return not data or data.endswith(b"\n")


def _rebuilt(reader: _IncrementalMCPLogReader, cache: Path) -> claude_code_mcp.MCPLogIndex:
    """The index a full rebuild from the reader's own complete lines gives."""
    discovered = claude_code_mcp._discover_mcp_logs((cache,))
    assert isinstance(discovered, claude_code_mcp._DiscoveredLogs)
    return claude_code_mcp._fold_mcp_logs(discovered, lambda path: list(reader._files[path]._lines))


@pytest.mark.parametrize("seed", range(40))
def test_incremental_index_matches_a_cold_read_at_every_step(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    cache = tmp_path / "cache"
    paths = [
        _log_path(cache, "-work-a", "books", "2026-01-01T00-00-00-000Z.jsonl"),
        _log_path(cache, "-work-a", "books", "2026-01-02T00-00-00-000Z.jsonl"),
        _log_path(cache, "-work-a", "files", "2026-01-01T00-00-00-000Z.jsonl"),
        _log_path(cache, "-work-b", "books", "2026-01-01T00-00-00-000Z.jsonl"),
    ]
    pending = {path: b"".join(_script(rng)) for path in paths}
    for path in paths:
        path.write_bytes(b"")
    reader = _IncrementalMCPLogReader(roots=(cache,))

    steps = 0
    while any(pending.values()) and steps < 200:
        steps += 1
        path = rng.choice([p for p in paths if pending[p]])
        cut = rng.randint(1, len(pending[path]))
        with path.open("ab") as handle:
            handle.write(pending[path][:cut])
        pending[path] = pending[path][cut:]
        if rng.random() < 0.05:
            # A rotated log: new inode, rewritten from the start.
            content = path.read_bytes()
            path.unlink()
            path.write_bytes(content)

        incremental = reader.read()
        assert incremental == _rebuilt(reader, cache), f"seed {seed}, step {steps}"
        if all(_at_a_record_boundary(p) for p in paths):
            assert incremental == read_mcp_logs((cache,)), f"seed {seed}, step {steps}"


def test_an_appended_record_is_the_only_one_folded(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "cache"
    books = log.write_server_log(
        cache,
        "books",
        [log.connected("s1"), log.calling("s1", "search"), log.completed("s1", "search")],
    )
    log.write_server_log(cache, "files", [log.connected("s2"), log.calling("s2", "fetch")])
    reader = _IncrementalMCPLogReader(roots=(cache,))
    reader.read()
    applied: list[str] = []
    original = claude_code_mcp._apply

    def counting(message, server, logs):
        applied.append(message)
        return original(message, server, logs)

    monkeypatch.setattr(claude_code_mcp, "_apply", counting)
    with books.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(log.calling("s1", "search")) + "\n")

    index = reader.read()

    assert applied == ["Calling MCP tool: search"]
    assert index == read_mcp_logs((cache,))


def test_a_returned_index_does_not_change_when_later_lines_are_folded(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    books = log.write_server_log(
        cache, "books", [log.connected("s1"), log.still_running("s1", "search", seconds=5)]
    )
    reader = _IncrementalMCPLogReader(roots=(cache,))
    before = reader.read()
    snapshot = read_mcp_logs((cache,))

    with books.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(log.completed("s1", "search")) + "\n")
        handle.write(json.dumps(log.connect_failed("s1", 9, "503", "gone")) + "\n")
    after = reader.read()

    assert before == snapshot
    assert after != before
    assert after == read_mcp_logs((cache,))


def test_an_undecodable_last_record_becomes_torn_once_followed(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    books = log.write_server_log(cache, "books", [log.connected("s1")])
    with books.open("a", encoding="utf-8") as handle:
        handle.write("{broken\n")
    reader = _IncrementalMCPLogReader(roots=(cache,))

    first = reader.read()
    with books.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(log.calling("s1", "search")) + "\n")
    second = reader.read()

    assert ("-work-project", "books") not in first.incomplete_project_servers
    assert ("-work-project", "books") in second.incomplete_project_servers
    assert second == read_mcp_logs((cache,))
