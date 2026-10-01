from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openaidr.collector import IncrementalCollector, collect, default_readers
from openaidr.kinds import parse_kind_filter
from openaidr.readers.base import Window
from openaidr.readers.codex import CodexReader
from tests.fixtures.codex_jsonl import call, message, meta, output, record, write_rollout


def test_messages_calls_metadata_and_times(tmp_path: Path) -> None:
    write_rollout(
        tmp_path,
        [
            meta(),
            record("turn_context", {"model": "synthetic-model", "cwd": "/synthetic/other"}),
            message(),
            call(),
            output(),
            call("custom", custom=True),
            output("custom", [{"type": "input_text", "text": "secret result"}], custom=True),
        ],
    )
    sessions, failures = CodexReader(tmp_path).collect(Window(None))
    assert failures == []
    s = sessions[0]
    assert (s.session_id, s.agent_kind, s.source) == ("codex:s1", "codex", "codex")
    assert s.model == "synthetic-model"
    assert s.agent_version == "0.1"
    assert s.entrypoint == "cli"
    assert s.started_at == datetime(2026, 9, 1, 10, tzinfo=UTC)
    assert s.last_activity_at == datetime(2026, 9, 1, 10, 0, 3, tzinfo=UTC)
    assert len(s.turns) == 3
    c = s.turns[1].tool_calls[0]
    assert c.provider_call_id == "call1"
    assert c.status == "unknown"  # serialized output does not carry success
    assert c.duration_ms == 1000
    assert c.working_directory == "/synthetic/other"
    assert s.turns[1].occurred_at == datetime(2026, 9, 1, 10, 0, 2, tzinfo=UTC)
    custom = s.turns[2].tool_calls[0]
    assert custom.arguments == {"input": "synthetic input"}
    assert json.loads(custom.result or "")[0]["text"] == "secret result"
    assert custom.result_size == len(custom.result or "")
    assert s.mcp_log_state == "not_attempted"


def test_result_join_is_scoped_to_session_and_call_family(tmp_path: Path) -> None:
    write_rollout(tmp_path, [meta("a"), call(), output(body="A")], "a")
    write_rollout(tmp_path, [meta("b"), call(), output(body="B", custom=True)], "b")
    sessions, failures = CodexReader(tmp_path).collect(Window(None))
    assert not failures
    by_id = {s.session_id: s for s in sessions}
    assert by_id["codex:a"].turns[0].tool_calls[0].result == "A"
    assert by_id["codex:b"].turns[0].tool_calls[0].result is None


def test_subagent_inherited_prefix_is_not_child_activity(tmp_path: Path) -> None:
    inherited = call("parent")
    inherited["ordinal"] = 1
    child = call("child")
    child["ordinal"] = 3
    write_rollout(
        tmp_path,
        [
            meta(
                "child",
                parent_thread_id="parent",
                source={"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}},
                subagent_history_start_ordinal=3,
            ),
            meta("parent"),
            inherited,
            child,
        ],
    )
    sessions, failures = CodexReader(tmp_path).collect(Window(None))
    assert not failures
    assert sessions[0].session_id == "codex:child"
    assert len(sessions[0].turns) == 1
    assert sessions[0].turns[0].is_sidechain
    assert sessions[0].turns[0].tool_calls[0].provider_call_id == "child"


def test_window_uses_mtime_and_includes_boundary(tmp_path: Path) -> None:
    path = write_rollout(tmp_path, [meta(), message()])
    cutoff = datetime(2026, 9, 2, tzinfo=UTC)
    os.utime(path, (cutoff.timestamp(), cutoff.timestamp()))
    assert len(CodexReader(tmp_path).collect(Window(cutoff))[0]) == 1
    os.utime(path, (0, 0))
    assert CodexReader(tmp_path).collect(Window(cutoff)) == ([], [])


@pytest.mark.parametrize("bad", [b"{invalid}\n", b"\xff\n"])
def test_malformed_complete_record_reports_path_without_content(tmp_path: Path, bad: bytes) -> None:
    path = write_rollout(tmp_path, [meta(), call()])
    with path.open("ab") as f:
        f.write(bad)
    sessions, failures = CodexReader(tmp_path).collect(Window(None))
    assert not sessions
    assert len(failures) == 1
    assert str(path) in failures[0].message
    assert "invalid}" not in failures[0].message


def test_unreadable_file_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_rollout(tmp_path, [meta()])
    original = Path.open

    def denied(self: Path, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic permission failure")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    sessions, failures = CodexReader(tmp_path).collect(Window(None))
    assert not sessions
    assert len(failures) == 1
    assert str(path) in failures[0].message


def test_history_reference_is_reported_as_incomplete(tmp_path: Path) -> None:
    write_rollout(
        tmp_path,
        [
            meta(
                history_base={
                    "thread_id": "parent",
                    "end_ordinal_exclusive": 5,
                    "end_byte_offset": 500,
                }
            ),
            message(),
        ],
    )
    sessions, failures = CodexReader(tmp_path).collect(Window(None))
    assert sessions
    assert len(failures) == 1
    assert "history_base" in failures[0].message


def test_incremental_partial_records_rotation_and_cold_parity(tmp_path: Path) -> None:
    path = write_rollout(tmp_path, [meta(), message(), call()])
    collector = IncrementalCollector(readers=[CodexReader(tmp_path)])
    before = collector.collect("codex", path)
    data = (json.dumps(output(body="café"), ensure_ascii=False) + "\n").encode()
    with path.open("ab") as f:
        f.write(data[:-2])
    assert collector.collect("codex", path) == before
    with path.open("ab") as f:
        f.write(data[-2:])
    after = collector.collect("codex", path)
    cold = collect(parse_kind_filter(["codex"]), Window(None), [CodexReader(tmp_path)])
    assert after == cold
    assert (
        after.sessions[0].turns[1].tool_calls[0].span
        == before.sessions[0].turns[1].tool_calls[0].span
    )
    replacement = write_rollout(tmp_path, [meta("replacement"), message()], "replacement")
    replacement.replace(path)
    assert collector.collect("codex", path).sessions[0].session_id == "codex:replacement"
    path.write_text(json.dumps(meta("short")) + "\n")
    assert collector.collect("codex", path).sessions[0].session_id == "codex:short"


def test_codex_is_registered_and_can_be_filtered(tmp_path: Path) -> None:
    write_rollout(tmp_path, [meta(), message()])
    result = collect(parse_kind_filter(["codex"]), Window(None), default_readers(root=tmp_path))
    assert len(result.sessions) == 1
    assert not result.failures


@pytest.mark.parametrize("mode", ["json", "text"])
def test_cli_codex_collection_withholds_private_content(tmp_path: Path, mode: str) -> None:
    import subprocess
    import sys

    secret = "sk-synthetic-private-content"
    tool = call(name="mcp__synthetic_server__lookup")
    payload = tool["payload"]
    assert isinstance(payload, dict)
    payload["arguments"] = json.dumps({"query": secret})
    write_rollout(
        tmp_path,
        [
            meta(base_instructions={"text": secret}),
            message(secret),
            record("event_msg", {"type": "user_message", "message": secret}),
            message(secret, "developer"),
            tool,
            output(body=secret),
            record("compacted", {"message": secret}),
        ],
    )
    args = ["--format", "json"] if mode == "json" else ["--detail"]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "openaidr",
            "sessions",
            "--agent-kind",
            "codex",
            "--since",
            "36500d",
            *args,
        ],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "OPENAIDR_CODEX_ROOT": str(tmp_path),
            "OPENAIDR_CLAUDE_ROOT": str(tmp_path / "claude"),
        },
    )
    assert result.returncode == 0, result.stderr
    assert secret not in result.stdout + result.stderr
    assert "lookup" in result.stdout
    if mode == "json":
        document = json.loads(result.stdout)
        assert len(document["sessions"]) == 1
        assert document["sessions"][0]["agent_kind"] == "codex"


def test_duplicate_outputs_withhold_result_and_duration(tmp_path: Path) -> None:
    write_rollout(tmp_path, [meta(), call(), output(), output(body="another result")])
    sessions, failures = CodexReader(tmp_path).collect(Window(None))
    assert not failures
    tool = sessions[0].turns[0].tool_calls[0]
    assert tool.result is None
    assert tool.duration_ms is None


def test_no_timestamp_is_invented_and_offsets_are_normalized(tmp_path: Path) -> None:
    first = meta()
    first.pop("timestamp")
    tool = call()
    tool["timestamp"] = "2026-09-01T15:30:02+05:30"
    write_rollout(tmp_path, [first, tool])
    sessions, failures = CodexReader(tmp_path).collect(Window(None))
    assert not failures
    assert sessions[0].started_at == datetime(2026, 9, 1, 10, 0, 2, tzinfo=UTC)
    assert sessions[0].turns[0].occurred_at == sessions[0].started_at
    tool.pop("timestamp")
    write_rollout(tmp_path, [first, tool])
    sessions, _ = CodexReader(tmp_path).collect(Window(None))
    assert sessions[0].started_at is None
    assert sessions[0].turns[0].occurred_at is None


def test_default_root_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAIDR_CODEX_ROOT")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    write_rollout(tmp_path / "sessions", [meta("home")])
    write_rollout(tmp_path / "override", [meta("override")])
    assert CodexReader().collect(Window(None))[0][0].session_id == "codex:home"
    monkeypatch.setenv("OPENAIDR_CODEX_ROOT", str(tmp_path / "override"))
    assert CodexReader().collect(Window(None))[0][0].session_id == "codex:override"
    assert CodexReader(tmp_path / "sessions").collect(Window(None))[0][0].session_id == "codex:home"


def test_unsupported_tool_and_compressed_file_are_reported(tmp_path: Path) -> None:
    path = write_rollout(tmp_path, [meta(), record("response_item", {"type": "local_shell_call"})])
    path.with_suffix(".jsonl.zst").write_bytes(b"synthetic compressed placeholder")
    sessions, failures = CodexReader(tmp_path).collect(Window(None))
    assert len(sessions) == 1
    assert len(failures) == 2
    assert any("local_shell_call" in f.message for f in failures)
    assert any("compressed" in f.message for f in failures)


def test_identical_calls_with_distinct_ids_keep_their_own_outputs(tmp_path: Path) -> None:
    write_rollout(tmp_path, [meta(), call("a"), call("b"), output("b", "B"), output("a", "A")])
    sessions, failures = CodexReader(tmp_path).collect(Window(None))
    assert not failures
    assert [t.tool_calls[0].result for t in sessions[0].turns] == ["A", "B"]


def test_empty_output_is_returned_not_pending(tmp_path: Path) -> None:
    write_rollout(tmp_path, [meta(), call(), output(body="")])
    tool = CodexReader(tmp_path).collect(Window(None))[0][0].turns[0].tool_calls[0]
    assert (tool.status, tool.result, tool.result_size) == ("unknown", "", 0)


def test_incremental_projects_only_appended_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openaidr.readers.codex import _Projection

    path = write_rollout(tmp_path, [meta(), call()])
    reader = CodexReader(tmp_path)
    assert reader.collect_file(path)[0]
    original = _Projection.project
    projected: list[int] = []

    def track(self, record, line):
        projected.append(line)
        return original(self, record, line)

    monkeypatch.setattr(_Projection, "project", track)
    assert reader.collect_file(path)[0]
    assert projected == []
    with path.open("a") as f:
        f.write(json.dumps(output()) + "\n")
    sessions, failures = reader.collect_file(path)
    assert not failures
    assert projected == [3]
    assert sessions[0].turns[0].tool_calls[0].result == "synthetic result"


def test_parent_and_child_with_shared_root_session_id_stay_distinct(tmp_path: Path) -> None:
    write_rollout(
        tmp_path, [meta("parent", session_id="parent"), call(), output(body="parent")], "parent"
    )
    write_rollout(
        tmp_path,
        [
            meta("child", session_id="parent", parent_thread_id="parent"),
            call(),
            output(body="child"),
        ],
        "child",
    )
    result = collect(parse_kind_filter(["codex"]), Window(None), [CodexReader(tmp_path)])
    assert not result.failures
    assert {s.session_id for s in result.sessions} == {"codex:parent", "codex:child"}
    assert {s.turns[0].tool_calls[0].result for s in result.sessions} == {"parent", "child"}
    assert next(s for s in result.sessions if s.session_id == "codex:child").turns[0].is_sidechain


def test_repeated_ids_and_outputs_stay_ambiguous_without_mutating_prior_snapshot(
    tmp_path: Path,
) -> None:
    path = write_rollout(tmp_path, [meta(), call(), output()])
    reader = CodexReader(tmp_path)
    before, _ = reader.collect_file(path)
    with path.open("a") as stream:
        for _ in range(100):
            stream.write(json.dumps(call()) + "\n")
            stream.write(json.dumps(output()) + "\n")
    after, failures = reader.collect_file(path)
    assert not failures
    assert before[0].turns[0].tool_calls[0].result == "synthetic result"
    calls = [turn.tool_calls[0] for turn in after[0].turns]
    assert len(calls) == len({tool.span for tool in calls}) == 101
    assert calls[0].span == before[0].turns[0].tool_calls[0].span
    assert after == CodexReader(tmp_path).collect(Window(None))[0]
    assert all(tool.status == "pending" and tool.result is None for tool in calls)
    assert all(tool.duration_ms is None for tool in calls)
