"""Rendering a collection for a person or for a machine.

An honest structured account of activity: what ran, how it ended. Nothing is
resolved to a component and nothing is judged.
"""

from __future__ import annotations

import json
import platform
import re
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from openaidr.collector import Collection
from openaidr.model import MCPConnection, Session, Turn

#: Distinct tool names to list before summarising the rest; the remainder is
#: always stated, since a truncated list with no marker reads as the whole.
_TOOLS_SHOWN = 12

_SINCE_PATTERN = re.compile(r"^(\d+)d$")


def parse_since(value: str | None) -> datetime | None:
    """`14d` -> a UTC cut-off. `None` means no cut-off."""
    if value is None:
        return None
    match = _SINCE_PATTERN.match(value.strip())
    if not match:
        raise ValueError(f"expected a day count like '14d', got {value!r}")
    days = int(match.group(1))
    if days == 0:
        # A window of zero days is not "everything" — it is a cut-off nothing
        # after this instant can ever be newer than, a plausible-looking but
        # empty request. Reject it rather than silently collecting nothing.
        raise ValueError(f"expected a positive day count, got {value!r}")
    return datetime.now(UTC) - timedelta(days=days)


def render_text(collection: Collection, detail: bool = False) -> str:
    """The summary. The trail itself only when asked for.

    A fortnight on one machine is hundreds of sessions and tens of thousands of
    calls; printing either buries the shape of the thing in its own detail. The
    summary is what a person reads, and `--detail` is there when a particular
    session is the question.
    """
    lines: list[str] = []
    if detail:
        for session in collection.sessions:
            lines.extend(_session_lines(session, detail))
    if not collection.sessions:
        lines.append("No sessions found.")
    lines.extend(_summary_lines(collection))
    lines.extend(_coverage_lines(collection))
    lines.extend(_mcp_coverage_lines(collection))
    for failure in collection.failures:
        lines.append(f"! {failure.agent_kind}: {failure.message}")
    return "\n".join(lines) + "\n"


def _mcp_coverage_lines(collection: Collection) -> list[str]:
    """Whether the MCP connection logs were readable, said once and plainly.

    Silence here would be the failure this package argues against everywhere
    else: a machine whose cache path is not the one we probe would report every
    transport as absent, which reads exactly like a machine running no MCP
    servers at all. It is stated even when everything worked, because "0
    remote" only means something once you know we looked.
    """
    sessions = collection.sessions
    if not sessions:
        return []
    with_mcp = [s for s in sessions if any(c.mcp_server for t in s.turns for c in t.tool_calls)]
    with_connections = [s for s in sessions if s.mcp_connections]
    if not with_mcp and not with_connections:
        return []
    lines: list[str] = []
    if not with_mcp:
        # A server can fail before any call is issued -- the connection log
        # still has a record, and a consumer reading only the call-scoped
        # section below would see nothing where a failed connection actually
        # happened.
        lines += [
            "",
            (
                f"MCP connection logs — no MCP call was issued, but {len(with_connections)} "
                "session(s) recorded a connection attempt"
            ),
        ]
        connections = [c for s in sessions for c in s.mcp_connections]
        return lines + _mcp_connection_lines(connections)
    states = Counter(s.mcp_log_state for s in with_mcp)
    applied = states.get("applied", 0)
    lines += [
        "",
        (f"MCP connection logs — {applied} of {len(with_mcp)} sessions with MCP calls enriched"),
    ]
    if states.get("no_log_root"):
        lines.append(
            f"    {states['no_log_root']} could not be read: no cache directory at any "
            f"known path on {platform.system() or 'this platform'}. Transport, server "
            "identity and MCP call outcomes are unavailable, not absent."
        )
    if states.get("no_log_for_session"):
        lines.append(
            f"    {states['no_log_for_session']} had MCP calls but no log — the cache is "
            "pruned on the agent's schedule, not ours."
        )
    if states.get("count_mismatch"):
        lines.append(
            f"    {states['count_mismatch']} withheld per-call outcomes: the log and the "
            "transcript disagree on how many calls were made — or the log could not be "
            "read whole — so which outcome belongs to which call cannot be established. "
            "Connection facts kept."
        )
    if states.get("session_id_collision"):
        lines.append(
            f"    {states['session_id_collision']} withheld everything: more than one "
            "project directory holds a log under this session's id — a copied or "
            "restored project — and which one is this session's cannot be established."
        )
    if states.get("not_attempted"):
        lines.append(
            f"    {states['not_attempted']} not attempted: a subagent transcript is "
            "keyed by its parent's session id, so the log filed under that id cannot "
            "be shown to be this subagent's share rather than the parent's or a "
            "sibling's."
        )
    overlap_withheld = sum(s.mcp_overlap_withheld for s in with_mcp)
    if overlap_withheld:
        lines.append(
            f"    {overlap_withheld} call(s) within applied sessions still withheld their "
            "outcome: two or more calls to the same tool overlapped, or the line "
            "announcing one of them was lost, and the log has nothing to say which "
            "finished first. Transport kept."
        )
    connections = [c for s in sessions for c in s.mcp_connections]
    return lines + _mcp_connection_lines(connections)


def _mcp_connection_lines(connections: list[MCPConnection]) -> list[str]:
    """Transport and failure-category counts, over whatever connections exist.

    Shared by both branches of `_mcp_coverage_lines`: a connection can exist
    for a session that never issued an MCP call at all, when the server fails
    before any call is made.
    """
    lines: list[str] = []
    if connections:
        transports = Counter(c.transport for c in connections if c.transport)
        failed = Counter(c.failure_category for c in connections if c.failure_category)
        if transports:
            lines.append(
                "    transports:  " + ", ".join(f"{n} {t}" for t, n in sorted(transports.items()))
            )
        if failed:
            lines.append(
                "    connections that failed:  "
                + ", ".join(f"{n} {c}" for c, n in sorted(failed.items()))
            )
    return lines


def _coverage_lines(collection: Collection) -> list[str]:
    """State what the outcomes do not cover, once, rather than on every row."""
    calls = [c for s in collection.sessions for t in s.turns for c in t.tool_calls]
    unknown = sum(1 for c in calls if c.status == "unknown")
    if not unknown:
        return []
    return [
        "",
        (
            f"{unknown} of {len(calls)} calls returned with no outcome this collector "
            "could establish; the agent's parser supplies no success signal."
        ),
    ]


def _bypassed(turn: Turn) -> bool:
    return turn.permission_mode == "bypassPermissions"


def _summary_lines(collection: Collection) -> list[str]:
    """What was collected, aggregated — tools reached for, and how calls ended."""
    sessions = collection.sessions
    if not sessions:
        return []
    turns = sum(s.turn_count for s in sessions)
    calls = [c for s in sessions for t in s.turns for c in t.tool_calls]
    tools: Counter[str] = Counter()
    servers: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    for call in calls:
        tools[f"{call.mcp_server}/{call.tool_name}" if call.mcp_server else call.tool_name] += 1
        if call.mcp_server:
            servers[call.mcp_server] += 1
        statuses[call.status] += 1
    kinds = Counter(s.agent_kind or "unmapped" for s in sessions)
    sidechain = sum(1 for s in sessions for t in s.turns if t.is_sidechain)
    truncated = sum(1 for c in calls if c.truncated)

    lines = [
        "",
        f"Summary — {len(sessions)} sessions, {turns} turns, {len(calls)} tool calls",
        "",
        "  agent kinds",
    ]
    lines.extend(f"    {count:>7}  {kind}" for kind, count in kinds.most_common())
    lines.extend(["", f"  tools called ({len(tools)} distinct)"])
    lines.extend(f"    {count:>7}  {name}" for name, count in tools.most_common(_TOOLS_SHOWN))
    remainder = len(calls) - sum(c for _, c in tools.most_common(_TOOLS_SHOWN))
    if remainder:
        lines.append(f"    {remainder:>7}  across {len(tools) - _TOOLS_SHOWN} further tools")
    if servers:
        lines.extend(["", f"  MCP servers reached ({len(servers)} distinct)"])
        lines.extend(f"    {count:>7}  {name}" for name, count in servers.most_common(8))
    # Outcomes are not tabulated: for a kind whose parser supplies no success
    # signal, `unknown` is nearly every row, and a table of one repeated word
    # crowds out the outcomes that carry meaning. The coverage line below states
    # the unknown count once; the rest are named here because a refusal or a
    # still-pending call is a fact about the session worth seeing.
    notable = " · ".join(
        f"{count} {status}"
        for status, count in statuses.most_common()
        if status != "unknown" and count
    )
    # Permission mode is reported only when guarding was *off*. Naming every mode
    # would bury the one that changes how the rest of this summary reads: with
    # checks bypassed a refusal cannot occur, so "no refusals" means nothing was
    # asked rather than everything was approved. Those are opposite facts.
    unguarded = [
        (session, sum(len(turn.tool_calls) for turn in session.turns if _bypassed(turn)))
        for session in collection.sessions
        if any(_bypassed(turn) for turn in session.turns)
    ]
    if unguarded:
        lines.extend(
            [
                "",
                (
                    f"  ran with permission checks bypassed: {len(unguarded)} sessions, "
                    f"{sum(count for _, count in unguarded)} calls"
                ),
                "    A refusal cannot be recorded while checks are bypassed, so no",
                "    refusal in these turns means none was asked for.",
            ]
        )

    # Result bodies are held whole and unbounded, so their size is reported
    # rather than capped. A cap chosen before the number is uncomfortable is an
    # arbitrary threshold; a number on screen is the signal to choose one. For
    # scale: 56.5M characters across one whole corpus cost 117MB peak RSS.
    body_chars = sum(len(call.result or "") for call in calls)
    largest = max((len(call.result or "") for call in calls), default=0)
    lines.extend(
        [
            "",
            (
                f"  result bodies held: {body_chars / 1e6:.1f}M characters, "
                f"largest {largest:,} — carried whole, not capped"
            ),
        ]
    )

    trailing = f"  {sidechain} subagent turns · {truncated} results abridged upstream"
    if notable:
        trailing += f" · {notable}"
    lines.extend(["", trailing])
    return lines


def _session_lines(session: Session, detail: bool = False) -> list[str]:
    started = session.started_at.isoformat() if session.started_at else "unknown start"
    kind = session.agent_kind or "unmapped kind"
    calls = sum(len(t.tool_calls) for t in session.turns)
    header = f"{session.session_id}  [{kind}]  {started}  {session.turn_count} turns  {calls} calls"
    lines = [header]
    if not detail:
        return lines
    for turn in session.turns:
        for call in turn.tool_calls:
            name = f"{call.mcp_server}/{call.tool_name}" if call.mcp_server else call.tool_name
            # `unknown` is the common case for kinds whose parser supplies no
            # success signal, and a column repeating one word is not information.
            # The run's footer states the coverage instead; a blank here claims
            # nothing, where `ok` would have claimed success.
            status = "" if call.status == "unknown" else call.status
            size = f"{call.result_size}c" if call.result_size is not None else "-"
            marker = " (sidechain)" if turn.is_sidechain else ""
            if call.truncated:
                marker += " (truncated)"
            line = f"    {status:<11} {size:>9}  {name}{marker}"
            # error_text is local-only, not forbidden from local rendering — this
            # text surface is exactly the local reading the spec names it for.
            if call.error_text:
                line += f"  ({call.error_text})"
            lines.append(line)
    return lines


def render_json(collection: Collection) -> str:
    """The activity view: `public_descriptor()`'s fields plus what a person
    reading this machine's own output also needs — model, working directory,
    per-call status and result size. `working_directory` is kept
    because it is what tells sessions in different projects apart, and it
    carries far lower sensitivity than what was said or run. Everything else
    `LOCAL_ONLY` permits a local surface to carry — turn text, tool
    arguments, results, error text, machine, user — is withheld here by
    default: this document is exactly the shape most likely to be
    redirected to a file, piped into another program, or pasted somewhere,
    and none of that is `public_descriptor()`'s own, separate, deliberately
    narrower contract for what a consumer may carry outward.
    """
    document = {
        "sessions": [_session_document(s) for s in collection.sessions],
        "failures": [asdict(f) for f in collection.failures],
    }
    return json.dumps(document, indent=2, default=_encode)


def _session_document(session: Session) -> dict[str, object]:
    return {
        **session.public_descriptor(),
        "model": session.model,
        "source": session.source,
        "working_directory": session.working_directory,
        "mcp_log_state": session.mcp_log_state,
        "mcp_overlap_withheld": session.mcp_overlap_withheld,
        "mcp_connections": [
            # `failure_detail` is LOCAL_ONLY: a server's own words about a
            # failure can carry a URL or a header fragment, and this document
            # is the one most likely to be piped somewhere else.
            {k: v for k, v in asdict(connection).items() if k != "failure_detail"}
            for connection in session.mcp_connections
        ],
        "turns": [
            {
                "position": turn.position,
                "role": turn.role,
                "is_sidechain": turn.is_sidechain,
                "tool_calls": [
                    {
                        "span": call.span,
                        "tool_name": call.tool_name,
                        "mcp_server": call.mcp_server,
                        "status": call.status,
                        "result_size": call.result_size,
                        "truncated": call.truncated,
                    }
                    for call in turn.tool_calls
                ],
            }
            for turn in session.turns
        ],
    }


def _encode(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON serialisable: {type(value)!r}")
