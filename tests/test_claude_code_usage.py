"""Token usage: one response per provider id, stated by its last record.

The acceptance tests of `docs/specs/token-usage.md`, numbered as there.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from openaidr.model import TokenUsage
from openaidr.readers.base import Window
from openaidr.readers.claude_code import ClaudeCodeReader
from tests.fixtures.claude_jsonl import (
    ai_title,
    assistant_response,
    cost_state,
    custom_title,
    system_event,
    tool_result,
    user_text,
    write_session,
    write_subagent_session,
)

T0 = "2026-10-06T10:00:00.000Z"
T1 = "2026-10-06T10:00:01.000Z"
T2 = "2026-10-06T10:00:02.000Z"
T3 = "2026-10-06T10:00:03.000Z"


def _sessions(root: Path):
    sessions, _failures = ClaudeCodeReader(root=root).collect(Window(since=None))
    return {s.session_id: s for s in sessions}


def _session(root: Path, records: list[dict]):
    write_session(root, "-p", records)
    return _sessions(root)["claude-code:s1"]


def _usage(output: int, **extra: object) -> dict:
    return {
        "input_tokens": 3,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 200,
        "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 200},
        "output_tokens": output,
        "service_tier": "standard",
        "inference_geo": "not_available",
        **extra,
    }


def _final_usage(output: int) -> dict:
    return _usage(
        output,
        output_tokens_details={"thinking_tokens": 40},
        server_tool_use={"web_search_requests": 1, "web_fetch_requests": 2},
        speed="standard",
    )


def test_1_records_sharing_a_message_id_are_one_response(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [
            user_text("s1", "u1", T0, "go"),
            assistant_response("s1", "a1", T1, "msg_1", _usage(10)),
            assistant_response("s1", "a2", T2, "msg_1", _usage(50)),
            assistant_response("s1", "a3", T3, "msg_1", _final_usage(120), stop_reason="end_turn"),
        ],
    )
    assert [r.response_id for r in session.responses] == ["msg_1"]


def test_2_the_last_record_states_the_response(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [
            user_text("s1", "u1", T0, "go"),
            assistant_response("s1", "a1", T1, "msg_1", _usage(10)),
            assistant_response("s1", "a2", T2, "msg_1", _usage(50)),
            assistant_response("s1", "a3", T3, "msg_1", _final_usage(120), stop_reason="end_turn"),
        ],
    )
    response = session.responses[0]
    assert response.usage == TokenUsage(
        input_tokens=3,
        cache_read_input_tokens=1000,
        cache_creation_input_tokens=200,
        cache_creation_5m_input_tokens=0,
        cache_creation_1h_input_tokens=200,
        output_tokens=120,
        reasoning_tokens=40,
        web_search_requests=1,
        web_fetch_requests=2,
    )
    assert (response.service_tier, response.speed, response.inference_region) == (
        "standard",
        "standard",
        "not_available",
    )
    assert response.model == "claude-opus-5"
    assert response.request_id == "req_1"
    assert response.occurred_at == datetime(2026, 10, 6, 10, 0, 3, tzinfo=UTC)
    assert response.is_sidechain is False


def test_3_fields_are_not_merged_across_records(tmp_path: Path) -> None:
    earlier = _usage(10, speed="fast")
    later = _usage(50)
    later.pop("service_tier")
    session = _session(
        tmp_path,
        [
            user_text("s1", "u1", T0, "go"),
            assistant_response("s1", "a1", T1, "msg_1", earlier),
            assistant_response("s1", "a2", T2, "msg_1", later),
        ],
    )
    response = session.responses[0]
    assert response.speed is None
    assert response.service_tier is None


def test_4_final_is_a_stop_reason_on_the_last_record(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [
            user_text("s1", "u1", T0, "go"),
            assistant_response("s1", "a1", T1, "msg_1", _usage(5)),
            assistant_response("s1", "a2", T1, "msg_1", _final_usage(80), stop_reason="tool_use"),
            assistant_response("s1", "a3", T2, "msg_2", _usage(7)),
            assistant_response("s1", "a4", T3, "msg_2", _usage(9)),
        ],
    )
    final = {r.response_id: (r.final, r.usage.output_tokens) for r in session.responses}
    assert final == {"msg_1": (True, 80), "msg_2": (False, 9)}


def test_5_a_response_that_produces_no_turn_is_still_a_response(tmp_path: Path) -> None:
    thinking = [{"type": "thinking", "thinking": "considering", "signature": "x"}]
    session = _session(
        tmp_path,
        [
            user_text("s1", "u1", T0, "go"),
            assistant_response("s1", "a1", T1, "msg_think", _usage(30), content=thinking),
            assistant_response("s1", "a2", T2, "msg_text", _usage(5), stop_reason="end_turn"),
        ],
    )
    assert [r.response_id for r in session.responses] == ["msg_think", "msg_text"]
    assert "msg_think" not in {t.response_id for t in session.turns}


def test_6_a_count_the_record_does_not_state_is_none(tmp_path: Path) -> None:
    usage = {"input_tokens": 3, "output_tokens": 10}
    session = _session(
        tmp_path,
        [user_text("s1", "u1", T0, "go"), assistant_response("s1", "a1", T1, "msg_1", usage)],
    )
    response = session.responses[0]
    assert response.usage == TokenUsage(input_tokens=3, output_tokens=10)
    assert (response.service_tier, response.speed, response.inference_region) == (None,) * 3


def test_7_responses_go_to_the_session_named_on_their_records(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        "-p",
        [
            user_text("s1", "u1", T0, "first"),
            assistant_response("s1", "a1", T1, "msg_1", _usage(10), stop_reason="end_turn"),
            user_text("s2", "u2", T2, "resumed"),
            assistant_response("s2", "a2", T3, "msg_2", _usage(20), stop_reason="end_turn"),
        ],
    )
    sessions = _sessions(tmp_path)
    assert [r.response_id for r in sessions["claude-code:s1"].responses] == ["msg_1"]
    assert [r.response_id for r in sessions["claude-code:s2"].responses] == ["msg_2"]


def test_8_a_subagents_responses_are_its_own_and_sidechain(tmp_path: Path) -> None:
    delegation = tool_result(
        "s1",
        "r1",
        T2,
        "toolu_task",
        "done",
        toolUseResult={"usage": _usage(99), "totalTokens": 1302},
    )
    write_session(
        tmp_path,
        "-p",
        [
            user_text("s1", "u1", T0, "go"),
            assistant_response("s1", "a1", T1, "msg_parent", _usage(10), stop_reason="tool_use"),
            delegation,
        ],
    )
    write_subagent_session(
        tmp_path,
        "-p",
        "s1",
        "x",
        [
            user_text("s1", "su1", T1, "delegated"),
            assistant_response("s1", "sa1", T2, "msg_sub", _usage(99), stop_reason="end_turn"),
        ],
    )
    sessions = _sessions(tmp_path)
    parent, subagent = sessions["claude-code:s1"], sessions["claude-code:s1:agent-x"]
    assert [r.response_id for r in parent.responses] == ["msg_parent"]
    assert [(r.response_id, r.is_sidechain) for r in subagent.responses] == [("msg_sub", True)]


def test_responses_are_ordered_by_their_first_record(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [
            user_text("s1", "u1", T0, "go"),
            assistant_response("s1", "a1", T1, "msg_a", _usage(1)),
            assistant_response("s1", "a2", T2, "msg_b", _usage(2)),
            assistant_response("s1", "a3", T3, "msg_a", _usage(3), stop_reason="end_turn"),
        ],
    )
    assert [r.response_id for r in session.responses] == ["msg_a", "msg_b"]


def test_10_turns_carry_the_response_they_belong_to(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [
            user_text("s1", "u1", T0, "go"),
            assistant_response("s1", "a1", T1, "msg_1", _usage(5)),
            assistant_response("s1", "a2", T2, "msg_1", _usage(9), stop_reason="end_turn"),
        ],
    )
    assert [(t.role, t.response_id) for t in session.turns] == [
        ("user", None),
        ("assistant", "msg_1"),
        ("assistant", "msg_1"),
    ]


def test_11_a_response_without_a_request_id_is_still_reported(tmp_path: Path) -> None:
    session = _session(
        tmp_path,
        [
            user_text("s1", "u1", T0, "go"),
            assistant_response("s1", "a1", T1, "msg_1", _usage(5), request_id=None),
        ],
    )
    assert [(r.response_id, r.request_id) for r in session.responses] == [("msg_1", None)]


def test_12_a_synthetic_response_is_carried_with_its_zero_counts(tmp_path: Path) -> None:
    zeros = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    session = _session(
        tmp_path,
        [
            user_text("s1", "u1", T0, "go"),
            assistant_response(
                "s1", "a1", T1, "msg_syn", zeros, model="<synthetic>", stop_reason="stop_sequence"
            ),
        ],
    )
    response = session.responses[0]
    assert response.model == "<synthetic>"
    assert response.usage == TokenUsage(input_tokens=0, output_tokens=0, cache_read_input_tokens=0)


def test_15_the_clients_running_totals_are_not_read(tmp_path: Path) -> None:
    """A `cost-state` record is recognised and ignored: it changes no response,
    no turn and nothing else about the session (ADR-0015)."""
    records = [
        user_text("s1", "u1", T0, "go"),
        assistant_response("s1", "a1", T1, "msg_1", _usage(5), stop_reason="end_turn"),
    ]
    totals = cost_state(
        "s1",
        1.25,
        {"claude-opus-5": {"inputTokens": 20, "outputTokens": 500, "costUSD": 1.25}},
    )
    without = _session(tmp_path / "without", records)
    with_totals = _session(tmp_path / "with", [*records, totals])
    assert with_totals == without
    assert not hasattr(with_totals, "client_reported_cost")


def test_16_the_generated_title_is_its_own_field(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        "-p",
        [
            user_text("s1", "u1", T0, "go"),
            assistant_response("s1", "a1", T1, "msg_1", _usage(5), stop_reason="end_turn"),
            ai_title("s1", "Draft"),
            ai_title("s1", "Fix the parser"),
            user_text("s2", "u2", T2, "other"),
            assistant_response("s2", "a2", T3, "msg_2", _usage(5), stop_reason="end_turn"),
            custom_title("s2", "Named by hand"),
        ],
    )
    sessions = _sessions(tmp_path)
    generated, named = sessions["claude-code:s1"], sessions["claude-code:s2"]
    assert (generated.generated_title, generated.title) == ("Fix the parser", None)
    assert (named.generated_title, named.title) == (None, "Named by hand")


def test_17_a_compaction_carries_its_own_records_time(tmp_path: Path) -> None:
    boundary = system_event(
        "s1", "c1", T2, "compact_boundary", compactMetadata={"trigger": "auto", "preTokens": 9}
    )
    untimed = system_event(
        "s1", "c2", T3, "compact_boundary", compactMetadata={"trigger": "manual"}
    )
    del untimed["timestamp"]
    session = _session(
        tmp_path,
        [
            user_text("s1", "u1", T0, "go"),
            assistant_response("s1", "a1", T1, "msg_1", _usage(5), stop_reason="end_turn"),
            boundary,
            untimed,
        ],
    )
    assert [c.occurred_at for c in session.compactions] == [
        datetime(2026, 10, 6, 10, 0, 2, tzinfo=UTC),
        None,
    ]
