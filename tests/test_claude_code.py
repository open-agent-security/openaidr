"""The Claude Code reader: what we normalise on top of `adr-sensor`'s parse.

These tests write real Claude Code JSONL, let the dependency parse it, and assert
on what this package adds — identity, per-kind tool naming, and outcomes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from openaidr.readers.base import Window
from openaidr.readers.claude_code import ClaudeCodeReader
from tests.fixtures.claude_jsonl import assistant_tool_use, tool_result, user_text, write_session


def _sessions(root: Path):
    sessions, _failures = ClaudeCodeReader(root=root).collect(Window(since=None))
    return sessions


def _calls(root: Path):
    return [c for s in _sessions(root) for t in s.turns for c in t.tool_calls]


def test_a_session_is_identified_by_kind_and_the_agents_own_id(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        "-work-project",
        [user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "look at the repository")],
    )
    session = _sessions(tmp_path)[0]
    assert session.session_id == "claude-code:s1"
    assert session.agent_kind == "claude-code"


def test_the_upstream_source_name_is_retained_for_filtering(tmp_path: Path) -> None:
    """The agent's own name for itself survives normalisation, so a consumer can
    still select on it when the mapped kind is not the axis they want."""
    write_session(
        tmp_path,
        "-work-project",
        [user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "look at the repository")],
    )
    assert _sessions(tmp_path)[0].source == "claude"


def test_a_subagent_transcript_is_its_own_session(tmp_path: Path) -> None:
    """Every record in a subagent file carries the *parent's* session id, so the
    file's own path is the only thing that distinguishes it (ADR-0001)."""
    write_session(tmp_path, "-p", [user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "parent")])
    directory = tmp_path / "-p" / "s1" / "subagents"
    directory.mkdir(parents=True)
    (directory / "agent-abc123.jsonl").write_text(
        json.dumps(user_text("s1", "u9", "2026-08-01T10:00:02.000Z", "delegated")) + "\n",
        encoding="utf-8",
    )
    ids = sorted(s.session_id for s in _sessions(tmp_path))
    assert ids == ["claude-code:s1", "claude-code:s1:agent-abc123"]


def test_every_turn_in_a_subagent_transcript_is_marked_sidechain(tmp_path: Path) -> None:
    directory = tmp_path / "-p" / "s1" / "subagents"
    directory.mkdir(parents=True)
    (directory / "agent-abc123.jsonl").write_text(
        json.dumps(user_text("s1", "u9", "2026-08-01T10:00:02.000Z", "delegated")) + "\n",
        encoding="utf-8",
    )
    session = _sessions(tmp_path)[0]
    assert all(turn.is_sidechain for turn in session.turns)


def test_a_repeated_sequence_id_does_not_collide(tmp_path: Path) -> None:
    """Real transcripts re-emit a record; only the later turn is disambiguated,
    so a span already emitted for the first never moves."""
    record = assistant_tool_use("s1", "dup", "2026-08-01T10:00:00.000Z", "toolu_1", "Read")
    again = assistant_tool_use("s1", "dup", "2026-08-01T10:00:01.000Z", "toolu_2", "Bash")
    write_session(tmp_path, "-p", [record, again])
    session = _sessions(tmp_path)[0]
    keys = [t.key for t in session.turns]
    assert len(set(keys)) == len(keys)
    assert keys[0] == "dup"
    spans = [c.span for t in session.turns for c in t.tool_calls]
    assert len(set(spans)) == len(spans)


def test_a_repeated_sequence_id_keeps_each_occurrences_own_call_id(tmp_path: Path) -> None:
    """The recovery pass keyed `call_ids` on the record uuid alone would let the
    second occurrence's provider call id overwrite the first's at the same
    `(uuid, position)` key -- so the first, already-disambiguated turn would
    silently read the second's id, result and working directory instead of its
    own. Occurrence must be part of that key, not just of the turn key."""
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use(
                "s1", "dup", "2026-08-01T10:00:00.000Z", "toolu_1", "Read", cwd="/first"
            ),
            assistant_tool_use(
                "s1", "dup", "2026-08-01T10:00:01.000Z", "toolu_2", "Bash", cwd="/second"
            ),
            tool_result("s1", "r1", "2026-08-01T10:00:02.000Z", "toolu_1", "first contents"),
            tool_result("s1", "r2", "2026-08-01T10:00:03.000Z", "toolu_2", "second contents"),
        ],
    )
    session = _sessions(tmp_path)[0]
    first_call = session.turns[0].tool_calls[0]
    second_call = session.turns[1].tool_calls[0]
    assert (first_call.tool_name, first_call.provider_call_id) == ("Read", "toolu_1")
    assert (second_call.tool_name, second_call.provider_call_id) == ("Bash", "toolu_2")
    assert first_call.result == "first contents"
    assert second_call.result == "second contents"
    assert first_call.working_directory == "/first"
    assert second_call.working_directory == "/second"


def test_a_provider_call_id_reused_across_calls_withholds_both_outcomes(
    tmp_path: Path,
) -> None:
    """Upstream resolves a result by scanning for a structurally-identical
    pending `ToolUsage` anywhere in the transcript and updates every match it
    finds, not only the call that issued it. Two identical calls sharing one
    provider id therefore both end up holding the same copied result — the
    same silent misattribution ADR-0001 already rejects for a repeated turn
    key, so both outcomes are withheld rather than trusted."""
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "dup-id", "Read"),
            assistant_tool_use("s1", "u2", "2026-08-01T10:00:01.000Z", "dup-id", "Read"),
            tool_result("s1", "u3", "2026-08-01T10:00:02.000Z", "dup-id", "file contents"),
        ],
    )
    calls = _calls(tmp_path)
    assert len(calls) == 2
    for call in calls:
        assert call.status == "pending"
        assert call.result is None


def test_distinct_provider_ids_with_identical_arguments_withhold_both_outcomes(
    tmp_path: Path,
) -> None:
    """The same misattribution reaches calls that never shared a provider id:
    upstream matches by the call's structure, not its id, so two distinct
    calls with the same tool and arguments can still end up sharing one
    result while the other's real result is silently dropped."""
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "id-1", "Read"),
            assistant_tool_use("s1", "u2", "2026-08-01T10:00:01.000Z", "id-2", "Read"),
            tool_result("s1", "u3", "2026-08-01T10:00:02.000Z", "id-1", "result one"),
            tool_result("s1", "u4", "2026-08-01T10:00:03.000Z", "id-2", "result two"),
        ],
    )
    calls = _calls(tmp_path)
    assert len(calls) == 2
    for call in calls:
        assert call.status == "pending"
        assert call.result is None


def test_a_genuine_repeated_identical_outcome_is_also_withheld(tmp_path: Path) -> None:
    """Two calls that ran and returned independently, one fully resolved before
    the next began, produce the same shared-result shape as the dependency's
    mismatch once their results happen to be identical. Nothing that survives
    the dependency boundary distinguishes a genuine repeat from a mismatched
    one, so both are withheld the same way (ADR-0002) rather than trusting a
    result that might belong to the other call."""
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "id-1", "Read"),
            tool_result("s1", "u2", "2026-08-01T10:00:01.000Z", "id-1", "unchanged contents"),
            assistant_tool_use("s1", "u3", "2026-08-01T10:00:02.000Z", "id-2", "Read"),
            tool_result("s1", "u4", "2026-08-01T10:00:03.000Z", "id-2", "unchanged contents"),
        ],
    )
    calls = _calls(tmp_path)
    assert len(calls) == 2
    for call in calls:
        assert call.status == "pending"
        assert call.result is None


def test_mcp_tool_name_is_split_into_server_and_tool(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use(
                "s1", "u1", "2026-08-01T10:00:00.000Z", "t1", "mcp__github__get_issue"
            )
        ],
    )
    call = _calls(tmp_path)[0]
    assert (call.mcp_server, call.tool_name) == ("github", "get_issue")


def test_a_builtin_tool_has_no_server(tmp_path: Path) -> None:
    write_session(
        tmp_path, "-p", [assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "t1", "Read")]
    )
    assert _calls(tmp_path)[0].mcp_server is None


def test_a_returned_call_is_unknown_not_ok(tmp_path: Path) -> None:
    """Upstream's `success` for this kind means only that a result exists — it
    never reads `is_error` — so passing it through as `ok` would launder an
    absence of evidence into a claim of success."""
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "t1", "Read"),
            tool_result("s1", "u2", "2026-08-01T10:00:00.250Z", "t1", "file contents"),
        ],
    )
    call = _calls(tmp_path)[0]
    assert call.status == "unknown"
    assert call.result_size == len("file contents")


def test_an_unresolved_call_is_pending(tmp_path: Path) -> None:
    write_session(
        tmp_path, "-p", [assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "t1", "Read")]
    )
    assert _calls(tmp_path)[0].status == "pending"


def test_a_refusal_is_read_from_the_record_not_guessed_from_its_wording(tmp_path: Path) -> None:
    """The transcript records `toolDenialKind` on a refused call. Reading it is
    exact where matching phrasing was not."""
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "t1", "Bash"),
            tool_result(
                "s1",
                "u2",
                "2026-08-01T10:00:01.000Z",
                "t1",
                "The user doesn't want to proceed with this tool use.",
                toolDenialKind="user-rejected",
            ),
        ],
    )
    call = _calls(tmp_path)[0]
    assert call.status == "rejected"
    assert call.denial_kind == "user-rejected"


def test_who_refused_is_kept_because_it_is_a_different_fact(tmp_path: Path) -> None:
    """A person declining and a classifier blocking are both refusals, and are not
    the same event to anything reasoning about intent."""
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "t1", "Bash"),
            tool_result(
                "s1",
                "u2",
                "2026-08-01T10:00:01.000Z",
                "t1",
                "Permission denied by the auto mode classifier.",
                toolDenialKind="automode-blocked",
            ),
        ],
    )
    call = _calls(tmp_path)[0]
    assert call.status == "rejected"
    assert call.denial_kind == "automode-blocked"


def test_a_document_discussing_approvals_is_not_a_refusal(tmp_path: Path) -> None:
    """The defect this replaces. Substring matching on refusal phrasing scored
    80.2% precision over 21,874 real results, and its false positives were files
    *about* permissions — including this reader's own source. A result is a
    refusal only where the record says so.
    """
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "t1", "Read"),
            tool_result(
                "s1",
                "u2",
                "2026-08-01T10:00:01.000Z",
                "t1",
                "# Spec\n\nA destructive command requires approval, and the user "
                "rejected calls are recorded as such.",
            ),
        ],
    )
    call = _calls(tmp_path)[0]
    assert call.status == "unknown"
    assert call.denial_kind is None


def test_a_refusal_is_matched_whole_never_by_substring(tmp_path: Path) -> None:
    """A result that merely *contains* a refusal body is not itself refused."""
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "t1", "Bash"),
            tool_result(
                "s1",
                "u2",
                "2026-08-01T10:00:01.000Z",
                "t1",
                "This command requires approval",
                toolDenialKind="user-rejected",
            ),
            assistant_tool_use("s1", "u3", "2026-08-01T10:00:02.000Z", "t2", "Read"),
            tool_result(
                "s1",
                "u4",
                "2026-08-01T10:00:03.000Z",
                "t2",
                "the log said: This command requires approval -- and then it continued",
            ),
        ],
    )
    statuses = [c.status for c in _calls(tmp_path)]
    assert statuses == ["rejected", "unknown"]


def test_ordinary_output_mentioning_an_error_is_not_called_rejected(tmp_path: Path) -> None:
    """Precision over recall: a false `rejected` claims a person made a decision."""
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "t1", "Bash"),
            tool_result("s1", "u2", "2026-08-01T10:00:01.000Z", "t1", "error: exit code 1"),
        ],
    )
    assert _calls(tmp_path)[0].status == "unknown"


def test_the_dependencys_progress_output_never_reaches_stdout(tmp_path: Path, capsys) -> None:
    """Upstream narrates on stdout; this package emits machine-readable output."""
    write_session(
        tmp_path,
        "-p",
        [user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "look at the repository")],
    )
    _sessions(tmp_path)
    assert capsys.readouterr().out == ""


def test_an_unreadable_file_does_not_lose_the_other_sessions_and_reports_a_failure(
    tmp_path: Path,
) -> None:
    """`ClaudeParser` catches its own file-level error, prints it, and returns no
    data for that file — indistinguishable from an empty file unless the printed
    line is captured and reported rather than discarded."""
    write_session(
        tmp_path,
        "-p",
        [user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "look at the repository")],
    )
    broken = tmp_path / "-p" / "broken.jsonl"
    broken.write_bytes(b"\xff\xfe not valid utf-8 at all")
    sessions, failures = ClaudeCodeReader(root=tmp_path).collect(Window(since=None))
    assert [s.session_id for s in sessions] == ["claude-code:s1"]
    assert len(failures) == 1
    assert failures[0].agent_kind == "claude-code"
    assert str(broken) in failures[0].message
    # Characterizes the exact printed form `adr-sensor==1.0.0` uses for this
    # failure, so a later pin bump that changes it is a deliberate re-check
    # rather than a silent drift in what this reader treats as a diagnostic.
    assert "[CLAUDE] Error reading" in failures[0].message


def test_a_session_the_dependency_judges_trivial_is_not_reported(tmp_path: Path) -> None:
    """A limit inherited from the dependency, recorded rather than discovered.

    `adr-sensor` drops a session whose single message is five characters or
    fewer and calls no tool. Nothing here can see such a session, so its absence
    is upstream's judgement rather than this package's account of the machine.
    """
    write_session(tmp_path, "-p", [user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "hi")])
    assert _sessions(tmp_path) == []


def test_the_window_excludes_files_modified_before_it(tmp_path: Path) -> None:
    import os

    path = write_session(
        tmp_path,
        "-p",
        [user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "look at the repository")],
    )
    old = (datetime.now(UTC) - timedelta(days=30)).timestamp()
    os.utime(path, (old, old))
    since = datetime.now(UTC) - timedelta(days=14)
    assert ClaudeCodeReader(root=tmp_path).collect(Window(since=since)) == ([], [])


def test_a_transcript_that_vanishes_during_the_window_check_does_not_lose_the_others(
    tmp_path: Path, monkeypatch
) -> None:
    """The window check stats a file the glob just listed a moment earlier.

    A file that disappears, is replaced, or otherwise cannot be stat'd in that
    gap must not raise out of `collect` entirely — that would discard every
    session already accumulated from other transcripts and report a
    whole-reader failure over one missing file.
    """
    import openaidr.readers.claude_code as claude_code

    write_session(tmp_path, "-a", [user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "one")])
    gone = write_session(tmp_path, "-b", [user_text("s2", "u2", "2026-08-01T10:00:00.000Z", "two")])

    real_within_window = claude_code._within_window

    def flaky_within_window(path: Path, window: Window) -> bool:
        if path == gone:
            raise OSError("vanished")
        return real_within_window(path, window)

    monkeypatch.setattr(claude_code, "_within_window", flaky_within_window)
    sessions, failures = ClaudeCodeReader(root=tmp_path).collect(Window(since=None))

    assert [s.session_id for s in sessions] == ["claude-code:s1"]
    assert any(str(gone) in f.message and "vanished" in f.message for f in failures)


def test_a_long_result_is_carried_whole(tmp_path: Path) -> None:
    """Upstream abridges every result to 1,000 characters from the middle — a cap
    that on one corpus sat *below* the median result size and cut 38% of results.
    The body is where a consumer's evidence usually is, so it is read from the
    record instead, unabridged.
    """
    body = "A" * 4000
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "t1", "Read"),
            tool_result("s1", "u2", "2026-08-01T10:00:01.000Z", "t1", body),
        ],
    )
    call = _calls(tmp_path)[0]
    assert call.result == body
    assert call.truncated is False
    assert call.result_size == len(body)


def test_a_call_with_no_recorded_body_falls_back_and_says_it_was_abridged() -> None:
    """The record's body is reached through the call's own id. Where none can be
    joined — a transcript that became unreadable between the two passes — all
    there is is upstream's already-abridged copy, and it must still report that
    it is abridged and how long the original was.

    Exercised directly: on real transcripts every call carries an id (16,673 of
    16,673 measured), so this path is a correctness guard rather than a
    routinely-taken branch, and contriving a file to reach it would test the
    contrivance instead.
    """
    from openaidr.readers.claude_code import _MCPEnrichment, _tool_calls, _Transcript

    class _Usage:
        tool_name = "Read"
        tool_type = "tool"
        server_name = None
        arguments: ClassVar[dict[str, object]] = {}
        result = "A" * 400 + "... [truncated 3200 chars] ..." + "A" * 400
        status = "success"
        error = None

    empty = _Transcript({}, {}, {}, {}, {}, {}, {}, {}, {}, {})
    (call,) = _tool_calls(
        [_Usage()],  # type: ignore[list-item]
        "claude-code:s1",
        "u1",
        "u1",
        0,
        set(),
        empty,
        _MCPEnrichment(state="not_attempted"),
    )

    assert call.truncated is True
    assert call.result_size == 4000


def test_an_untruncated_result_reports_its_own_length(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "t1", "Read"),
            tool_result("s1", "u2", "2026-08-01T10:00:01.000Z", "t1", "short body"),
        ],
    )
    call = _calls(tmp_path)[0]
    assert call.truncated is False
    assert call.result_size == len("short body")


def test_a_calls_span_survives_a_reread_of_a_growing_transcript(tmp_path: Path) -> None:
    """The reader is rerun against the same file as it grows — not the pure
    `span_id` formatter called twice in isolation — because the risk the
    stability contract guards against lives in how a turn already on disk is
    keyed as later records arrive, not in string formatting (ADR-0001)."""
    write_session(
        tmp_path,
        "-p",
        [assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "t1", "Read")],
    )
    pending = _calls(tmp_path)[0]
    assert pending.status == "pending"

    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "t1", "Read"),
            tool_result("s1", "u2", "2026-08-01T10:00:00.250Z", "t1", "file contents"),
            user_text("s1", "u3", "2026-08-01T10:00:01.000Z", "go on"),
            assistant_tool_use("s1", "u4", "2026-08-01T10:00:02.000Z", "t2", "Bash"),
        ],
    )
    resolved, later = _calls(tmp_path)

    assert resolved.span == pending.span
    assert resolved.status == "unknown"
    assert later.span != resolved.span


def test_the_project_root_is_recovered_when_upstream_reports_none(tmp_path: Path) -> None:
    """Upstream reports `cwd` only from the record that first created the
    session, and that record often carries none. The directory holding the
    transcript encodes the root, and discovery is ours."""
    project = tmp_path / "work" / "my-project"
    project.mkdir(parents=True)
    encoded = str(project).replace("/", "-")
    record = user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "look at the repository")
    del record["cwd"]
    write_session(tmp_path, encoded, [record])
    session = _sessions(tmp_path)[0]
    assert session.working_directory == str(project)


def test_a_directory_name_containing_a_dash_is_not_mis_split(tmp_path: Path) -> None:
    """A dash separates path segments and appears inside names; only a candidate
    that exists on disk is returned, so the longer name wins."""
    nested = tmp_path / "Projects" / "OpenACA-AIDR" / "ADR"
    nested.mkdir(parents=True)
    (tmp_path / "Projects" / "OpenACA").mkdir(parents=True, exist_ok=True)
    encoded = str(nested).replace("/", "-")
    record = user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "look at the repository")
    del record["cwd"]
    write_session(tmp_path, encoded, [record])
    session = _sessions(tmp_path)[0]
    assert session.working_directory == str(nested)


def test_a_project_that_no_longer_exists_stays_unknown(tmp_path: Path) -> None:
    """A path that cannot be verified is left unknown rather than invented."""
    record = user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "look at the repository")
    del record["cwd"]
    write_session(tmp_path, "-Users-nobody-deleted-project", [record])
    session = _sessions(tmp_path)[0]
    assert session.working_directory is None


def test_cwd_is_recovered_when_upstream_fixed_it_from_a_record_that_had_none(
    tmp_path,
) -> None:
    """Upstream takes `project_path` from the first record bearing the session id
    and never refills it. A transcript that opens with a `queue-operation` — which
    carries no `cwd` — therefore loses a directory its later records state plainly.
    """
    directory = tmp_path / "-nowhere-at-all"
    directory.mkdir(parents=True)
    (directory / "s1.jsonl").write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {"type": "queue-operation", "sessionId": "s1"},
                {
                    "type": "assistant",
                    "sessionId": "s1",
                    "uuid": "u1",
                    "timestamp": "2026-08-01T10:00:00.000Z",
                    "isSidechain": False,
                    "cwd": "/work/real-project",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}
                        ],
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    sessions, failures = ClaudeCodeReader(root=tmp_path).collect(Window(since=None))

    assert failures == []
    assert [s.working_directory for s in sessions] == ["/work/real-project"]


def test_one_sessions_recorded_cwd_is_never_lent_to_another(tmp_path) -> None:
    """A file can hold more than one session; a directory belongs to exactly one."""
    directory = tmp_path / "-nowhere-at-all"
    directory.mkdir(parents=True)
    (directory / "both.jsonl").write_text(
        "\n".join(
            json.dumps(record)
            for record in (
                {
                    "type": "assistant",
                    "sessionId": "with-cwd",
                    "uuid": "u1",
                    "timestamp": "2026-08-01T10:00:00.000Z",
                    "isSidechain": False,
                    "cwd": "/work/only-mine",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "sessionId": "without-cwd",
                    "uuid": "u2",
                    "timestamp": "2026-08-01T10:00:01.000Z",
                    "isSidechain": False,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "toolu_2", "name": "Read", "input": {}}
                        ],
                    },
                },
            )
        )
        + "\n",
        encoding="utf-8",
    )

    sessions, _ = ClaudeCodeReader(root=tmp_path).collect(Window(since=None))

    directories = {s.session_id: s.working_directory for s in sessions}
    assert directories["claude-code:with-cwd"] == "/work/only-mine"
    assert directories["claude-code:without-cwd"] is None


def test_the_permission_mode_holds_until_the_next_declaration(tmp_path: Path) -> None:
    """It is not a session-level fact. A session can enter `bypassPermissions`
    partway through, and only the turns after that point ran unguarded.
    """
    write_session(
        tmp_path,
        "-p",
        [
            user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "first", permissionMode="default"),
            assistant_tool_use("s1", "u2", "2026-08-01T10:00:01.000Z", "t1", "Bash"),
            user_text(
                "s1",
                "u3",
                "2026-08-01T10:00:02.000Z",
                "second",
                permissionMode="bypassPermissions",
            ),
            assistant_tool_use("s1", "u4", "2026-08-01T10:00:03.000Z", "t2", "Bash"),
        ],
    )
    modes = [turn.permission_mode for turn in _sessions(tmp_path)[0].turns]
    assert modes == ["default", "default", "bypassPermissions", "bypassPermissions"]


def test_a_mode_declared_in_one_session_does_not_reach_another(tmp_path: Path) -> None:
    """One file can hold more than one session. A mode holds until the next
    declaration *within its own session*; lending it to a session that declared
    nothing would state the stronger, wrong fact that that session ran unguarded.
    """
    write_session(
        tmp_path,
        "-p",
        [
            user_text(
                "s1", "u1", "2026-08-01T10:00:00.000Z", "first", permissionMode="bypassPermissions"
            ),
            assistant_tool_use("s1", "u2", "2026-08-01T10:00:01.000Z", "t1", "Bash"),
            user_text("s2", "u3", "2026-08-01T10:00:02.000Z", "second"),
            assistant_tool_use("s2", "u4", "2026-08-01T10:00:03.000Z", "t2", "Bash"),
            assistant_tool_use("s1", "u5", "2026-08-01T10:00:04.000Z", "t3", "Bash"),
        ],
    )
    modes = {
        session.session_id: [turn.permission_mode for turn in session.turns]
        for session in _sessions(tmp_path)
    }
    assert modes["claude-code:s1"] == ["bypassPermissions"] * 3
    assert modes["claude-code:s2"] == [None, None]


def test_the_agent_build_and_entrypoint_are_recorded(tmp_path: Path) -> None:
    """Behaviour changes between agent versions, and a programmatic entrypoint
    means no human was at the keyboard."""
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use(
                "s1",
                "u1",
                "2026-08-01T10:00:00.000Z",
                "t1",
                "Read",
                version="2.1.241",
                entrypoint="sdk-cli",
            ),
        ],
    )
    session = _sessions(tmp_path)[0]
    assert session.agent_version == "2.1.241"
    assert session.entrypoint == "sdk-cli"


def test_a_transcript_that_declares_no_mode_leaves_it_unknown(tmp_path: Path) -> None:
    """Absent is not `default`: not knowing what was enforced is its own answer."""
    write_session(
        tmp_path,
        "-p",
        [assistant_tool_use("s1", "u1", "2026-08-01T10:00:00.000Z", "t1", "Read")],
    )
    assert _sessions(tmp_path)[0].turns[0].permission_mode is None


def test_a_recorded_success_is_ok_and_a_recorded_failure_is_error(tmp_path: Path) -> None:
    """`is_error` is the agent's own statement about whether the call worked. It
    is dropped by the shared event schema and read back here, which is what makes
    `ok` reachable at all — upstream's `status` says `success` merely because a
    result exists.
    """
    worked = tool_result("s1", "u3", "2026-08-01T10:00:01.000Z", "t1", "done")
    worked["message"]["content"][0]["is_error"] = False
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u2", "2026-08-01T10:00:00.000Z", "t1", "Bash"),
            worked,
            assistant_tool_use("s1", "u4", "2026-08-01T10:00:02.000Z", "t2", "Bash"),
            tool_result("s1", "u5", "2026-08-01T10:00:03.000Z", "t2", "boom", is_error=True),
        ],
    )
    assert [c.status for c in _calls(tmp_path)] == ["ok", "error"]


def test_a_result_that_states_no_outcome_stays_unknown(tmp_path: Path) -> None:
    """Absent `is_error` is not success. Roughly a fifth of real results carry
    none, and `ok` there would assert something the record does not support."""
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u2", "2026-08-01T10:00:00.000Z", "t1", "Bash"),
            tool_result("s1", "u3", "2026-08-01T10:00:01.000Z", "t1", "some output"),
        ],
    )
    assert _calls(tmp_path)[0].status == "unknown"


def test_a_calls_duration_needs_both_ends_of_it(tmp_path: Path) -> None:
    """A start alone would invite reading *still running* as *fast*."""
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u2", "2026-08-01T10:00:00.000Z", "t1", "Bash"),
            tool_result("s1", "u3", "2026-08-01T10:00:01.500Z", "t1", "done"),
            assistant_tool_use("s1", "u4", "2026-08-01T10:00:02.000Z", "t2", "Bash"),
        ],
    )
    calls = _calls(tmp_path)
    assert calls[0].duration_ms == 1500
    assert calls[1].duration_ms is None


def test_the_providers_own_call_id_is_carried(tmp_path: Path) -> None:
    """Span identity stays derived (ADR-0001) — changing what a span is would move
    every row a consumer has addressed — but the provider's id lets a consumer
    correlate with anything else that saw the same call."""
    write_session(
        tmp_path,
        "-p",
        [
            assistant_tool_use("s1", "u2", "2026-08-01T10:00:00.000Z", "toolu_abc", "Bash"),
            tool_result("s1", "u3", "2026-08-01T10:00:01.000Z", "toolu_abc", "done"),
        ],
    )
    assert _calls(tmp_path)[0].provider_call_id == "toolu_abc"
