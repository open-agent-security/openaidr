---
id: 0020
title: Place Devin's run-log facts on a session through its lock, or not at all
status: proposed
date: 2026-10-09
supersedes: null
superseded-by: null
---

## Context

Devin's store records MCP calls, never connections. Each process writes
`<data>/logs/devin_<timestamp>_<pid>.log`, which at the default level logs every
MCP connection (transport, URL, outcome, failure reason), the build version,
and whether it runs as an ACP server. No line names a session. Each session's
`<data>/session_locks/<id>.lock` holds the PID of the process that last held it.
Connections are made before the session's first row is written, so a time
window starting at the session's creation misses them.

## Decision

A session's process log is the log whose file name carries its lock's PID and
whose span covers the session's activity. If another session's lock names the
same process over that span, the log's connection lines cannot be divided
between them: the session's `mcp_log_state` is `placement_ambiguous` and it gets
no connections, while the process-level facts (`agent_version`, `entrypoint`)
are kept. Failures announced as the background description-cache pass are set
aside; attempts that still disagree leave `connected` unknown. Sub-agent
sessions run in their parent's process and share its placement.

## Alternatives considered

- **Place by time overlap alone**: rejected; connections precede the session
  row, and two sessions in one process are indistinguishable by time.
- **Give every overlapping session the whole log**: rejected; it attributes one
  session's servers to another, the confident wrong answer ADR-0002 forbids.

## Consequences

Only the newest holder of a session is known, so a session resumed across
processes is placed on its last process only. A REPL process that opens several
sessions makes each of them ambiguous for connections. Because one session's
lock changes another's placement, a growing store re-emits by comparing each
session's output, not by tracing which session an event touched (ADR-0018).
`failure_category` gains
`spawn` and `policy`, the two most actionable reasons this log states.

## When to revisit

If Devin's log starts naming the session on connection lines, or a lock
records more than the last holder.
