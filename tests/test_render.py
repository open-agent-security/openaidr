from datetime import UTC, datetime

from openaidr.collector import Collection
from openaidr.model import MCPConnection, Session, ToolCall, Turn
from openaidr.readers.base import ReaderFailure
from openaidr.render import parse_since, render_json, render_text

_START = datetime(2026, 8, 1, 10, 0, tzinfo=UTC)


def _session() -> Session:
    call = ToolCall(
        span="claude-code:s1:toolu_1",
        tool_name="get_issue",
        mcp_server="github",
        status="rejected",
        arguments={"id": 1},
        result="denied",
        result_size=6,
        error_text="denied",
        truncated=False,
    )
    turn = Turn(
        position=0, key="u1", role="assistant", text="", is_sidechain=False, tool_calls=(call,)
    )
    return Session(
        session_id="claude-code:s1",
        agent_kind="claude-code",
        source="claude",
        started_at=_START,
        model="claude-opus-5",
        working_directory="/work",
        machine=None,
        user=None,
        agent_version="2.1.241",
        entrypoint="cli",
        turns=(turn,),
    )


def test_text_output_names_the_tool_its_server_status_and_size() -> None:
    """Per-call rows are the detail view; the default is one line per session."""
    output = render_text(Collection(sessions=[_session()], failures=[]), detail=True)
    assert "claude-code:s1" in output
    assert "github/get_issue" in output
    assert "rejected" in output
    assert "6c" in output


def test_text_output_shows_error_text_for_a_failed_call() -> None:
    """`error_text` is local-only, not forbidden from local rendering: this is
    the surface a local reader needs to see why a call didn't succeed."""
    output = render_text(Collection(sessions=[_session()], failures=[]), detail=True)
    assert "denied" in output


def test_text_output_reports_a_failed_kind_rather_than_hiding_it() -> None:
    collection = Collection(sessions=[], failures=[ReaderFailure("cursor", "unreadable")])
    output = render_text(collection)
    assert "cursor" in output and "unreadable" in output


def test_text_output_says_so_when_there_is_nothing() -> None:
    assert "no sessions" in render_text(Collection()).lower()


def test_json_output_is_machine_readable_and_carries_spans() -> None:
    import json

    document = json.loads(render_json(Collection(sessions=[_session()], failures=[])))
    call = document["sessions"][0]["turns"][0]["tool_calls"][0]
    assert call["span"] == "claude-code:s1:toolu_1"
    assert call["status"] == "rejected"
    assert call["result_size"] == 6
    assert document["failures"] == []


def test_json_output_withholds_local_only_content_by_default() -> None:
    """`--format json` is a deliberately redacted activity view, not the full
    local model: `working_directory` is kept to tell sessions in different
    projects apart, but text, arguments, results, error text, machine and
    user — everything else `LOCAL_ONLY` would permit a local surface to show
    — are withheld, because this is the output shape most likely to be
    redirected to a file or piped elsewhere.
    """
    import json

    document = json.loads(render_json(Collection(sessions=[_session()], failures=[])))
    session_doc = document["sessions"][0]
    assert "machine" not in session_doc
    assert "user" not in session_doc
    turn = session_doc["turns"][0]
    assert "text" not in turn
    call = turn["tool_calls"][0]
    assert "arguments" not in call
    assert "result" not in call
    assert "error_text" not in call


def test_parse_since_accepts_a_day_count() -> None:
    since = parse_since("14d")
    assert since is not None
    assert (datetime.now(UTC) - since).days in (13, 14)


def test_parse_since_rejects_nonsense() -> None:
    import pytest

    with pytest.raises(ValueError):
        parse_since("last tuesday")


def test_parse_since_rejects_a_zero_or_negative_count() -> None:
    """A window of zero days is not `None`'s "everything" — it is a cut-off
    nothing after this instant can ever be newer than, a plausible-looking but
    empty request. A negative count is already rejected by the pattern."""
    import pytest

    with pytest.raises(ValueError):
        parse_since("0d")
    with pytest.raises(ValueError):
        parse_since("-3d")


def test_the_default_view_is_the_summary_alone() -> None:
    """Hundreds of sessions and tens of thousands of calls; printing either
    buries the shape of the thing in its own detail."""
    output = render_text(Collection(sessions=[_session()], failures=[]))
    assert "Summary" in output
    assert "claude-code:s1" not in output
    detail = render_text(Collection(sessions=[_session()], failures=[]), detail=True)
    assert "claude-code:s1" in detail
    assert "1 turns  1 calls" in detail
    # The tool still appears in the summary's own list; what the default view
    # drops is the session trail and its per-call rows.
    assert "rejected" in detail and "6c" in detail
    assert "6c" not in output


def test_the_summary_counts_tools_servers_and_outcomes() -> None:
    output = render_text(Collection(sessions=[_session()], failures=[]))
    assert "Summary — 1 sessions, 1 turns, 1 tool calls" in output
    assert "agent kinds" in output and "claude-code" in output
    assert "tools called" in output and "github/get_issue" in output
    assert "MCP servers reached" in output and "github" in output
    # Outcomes are not tabulated — `unknown` would be nearly every row — but a
    # refusal is a fact about the session worth seeing.
    assert "how calls ended" not in output
    assert "1 rejected" in output


def test_mcp_coverage_reports_overlap_withheld_calls_separately_from_applied() -> None:
    """`applied` alone would read as full coverage. A session can be `applied`
    -- the log and transcript agree on totals -- and still have withheld some
    calls' outcomes because two calls to the same tool overlapped; that has to
    be its own line, not silently folded into "N of N enriched"."""
    call = ToolCall(
        span="claude-code:s1:toolu_1",
        tool_name="search",
        mcp_server="books",
        status="unknown",
        arguments={},
        result=None,
        result_size=None,
        error_text=None,
        truncated=False,
    )
    turn = Turn(
        position=0, key="u1", role="assistant", text="", is_sidechain=False, tool_calls=(call,)
    )
    session = Session(
        session_id="claude-code:s1",
        agent_kind="claude-code",
        source="claude",
        started_at=_START,
        model=None,
        working_directory=None,
        machine=None,
        user=None,
        mcp_log_state="applied",
        mcp_overlap_withheld=1,
        turns=(turn,),
    )
    output = render_text(Collection(sessions=[session], failures=[]))
    assert "1 of 1 sessions with MCP calls enriched" in output
    assert "1 call(s) within applied sessions still withheld their outcome" in output

    import json

    document = json.loads(render_json(Collection(sessions=[session], failures=[])))
    assert document["sessions"][0]["mcp_overlap_withheld"] == 1


def test_mcp_coverage_names_a_session_id_collision_rather_than_a_pruned_cache() -> None:
    """Two projects holding a log under one session id is a different answer
    from no log at all, and reporting it as `0 of 1 enriched` with no reason
    would read exactly like a pruned cache."""
    call = ToolCall(
        span="claude-code:s1:toolu_1",
        tool_name="search",
        mcp_server="books",
        status="unknown",
        arguments={},
        result=None,
        result_size=None,
        error_text=None,
        truncated=False,
    )
    turn = Turn(
        position=0, key="u1", role="assistant", text="", is_sidechain=False, tool_calls=(call,)
    )
    session = Session(
        session_id="claude-code:s1",
        agent_kind="claude-code",
        source="claude",
        started_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        model=None,
        working_directory=None,
        machine=None,
        user=None,
        mcp_log_state="session_id_collision",
        turns=(turn,),
    )
    output = render_text(Collection(sessions=[session], failures=[]))
    assert "0 of 1 sessions with MCP calls enriched" in output
    assert "1 withheld everything: more than one project directory" in output


def test_mcp_coverage_names_not_attempted_rather_than_leaving_it_unexplained() -> None:
    """A subagent's MCP calls are counted in the denominator but never
    enriched, since the log is keyed by the parent's session id and cannot be
    shown to be this subagent's share. Every other non-`applied` state gets a
    line; leaving this one out would read as an unexplained gap rather than
    evidence withheld on purpose."""
    call = ToolCall(
        span="claude-code:s1:toolu_1",
        tool_name="search",
        mcp_server="books",
        status="unknown",
        arguments={},
        result=None,
        result_size=None,
        error_text=None,
        truncated=False,
    )
    turn = Turn(
        position=0, key="u1", role="assistant", text="", is_sidechain=True, tool_calls=(call,)
    )
    session = Session(
        session_id="claude-code:s1",
        agent_kind="claude-code",
        source="claude",
        started_at=_START,
        model=None,
        working_directory=None,
        machine=None,
        user=None,
        mcp_log_state="not_attempted",
        turns=(turn,),
    )
    output = render_text(Collection(sessions=[session], failures=[]))
    assert "0 of 1 sessions with MCP calls enriched" in output
    assert "1 not attempted: a subagent transcript" in output


def test_a_connection_that_failed_before_any_call_is_still_reported() -> None:
    """A server can fail during startup, before any call is ever issued -- the
    session then has `mcp_connections` but no MCP tool call, and the failure
    must not disappear just because there is no call to attach it to."""
    turn = Turn(position=0, key="u1", role="assistant", text="", is_sidechain=False, tool_calls=())
    session = Session(
        session_id="claude-code:s1",
        agent_kind="claude-code",
        source="claude",
        started_at=_START,
        model=None,
        working_directory=None,
        machine=None,
        user=None,
        mcp_log_state="applied",
        mcp_connections=(MCPConnection(server="books", connected=False, failure_category="auth"),),
        turns=(turn,),
    )
    output = render_text(Collection(sessions=[session], failures=[]))
    assert "no MCP call was issued, but 1 session(s) recorded a connection attempt" in output
    assert "connections that failed:  1 auth" in output


def test_the_summary_states_the_remainder_when_the_tool_list_is_cut() -> None:
    """A truncated list with no marker reads as the whole of it."""
    calls = tuple(
        ToolCall(
            span=f"claude-code:s1:u1:{i}",
            tool_name=f"tool-{i}",
            mcp_server=None,
            status="ok",
            arguments={},
            result="x",
            result_size=1,
            error_text=None,
            truncated=False,
        )
        for i in range(20)
    )
    turn = Turn(
        position=0, key="u1", role="assistant", text="", is_sidechain=False, tool_calls=calls
    )
    session = Session(
        session_id="claude-code:s1",
        agent_kind="claude-code",
        source="claude",
        started_at=_START,
        model=None,
        working_directory=None,
        machine=None,
        user=None,
        agent_version="2.1.241",
        entrypoint="cli",
        turns=(turn,),
    )
    output = render_text(Collection(sessions=[session], failures=[]))
    assert "across 8 further tools" in output
