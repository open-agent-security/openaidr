---
id: 0012
title: Keep MCP log append cursors in memory
status: superseded
date: 2026-09-21
supersedes: null
superseded-by: 0013
---

## Context

The incremental Claude Code collector projects only appended transcript records,
but each collection still rereads every MCP connection log on the machine from
byte zero. Those logs are supplemental session evidence: a connection or tool
outcome can be appended while the transcript itself is unchanged. Repeating all
of that I/O makes steady-state collection grow with the machine's accumulated
connection logs and undermines the same detection-latency goal that motivated
the transcript cursor in ADR-0011.

Connection-log records also arrive through live JSONL writes. A reader can see a
partial final record, and a `still running` record can later be followed by a
completion. The existing cold reader seals an unresolved wait into an immutable
snapshot outcome after a pass, so incrementally mutating that sealed result
would leave both the provisional unresolved outcome and the later completion.

## Decision

The incremental Claude Code reader keeps one in-memory byte cursor and complete
line cache per MCP connection-log file. Each pass discovers the current log
files, reads only bytes appended after each cursor, and admits only
newline-terminated records. It then rebuilds a fresh `MCPLogIndex` from the
cached complete lines, preserving the cold reader's ordering, incompleteness,
and sealing semantics without rereading old bytes from disk.

An inode change or observed truncation resets that file's state and reads it
from byte zero. A file that disappears is removed from the cursor cache. Cursor
state is process-local; the first pass after restart remains cold.

## Alternatives considered

- **Reread every connection log on each transcript update.** Rejected because
  steady-state I/O grows with all logs on the host, including unrelated
  sessions and projects.
- **Mutate the prior `MCPLogIndex` with appended records.** Rejected because the
  index is an exported snapshot whose unresolved waits have already been sealed
  as outcomes. Undoing and replaying those derived outcomes would couple the
  cursor to aggregation internals and make recovery from rotation harder.
- **Persist per-file cursors.** Rejected for the same reason as ADR-0011: the
  normal steady-state path needs no persistence, while a durable cursor adds
  versioning and recovery state for an optimization whose cold path is already
  correct.

## Consequences

Repeated incremental collection no longer rereads the existing contents of MCP
connection logs. Rebuilding the immutable aggregate still scales with the
number of cached complete lines, and discovery still walks the cache tree; this
decision removes repeated disk reads, not all work proportional to retained
history. Memory grows with the complete connection-log lines retained by the
long-lived reader.

MCP-log-only changes affect the next collection even when no transcript record
was appended. Scheduling that collection when a supplemental file changes is a
consumer concern; this reader defines the stateful read once invoked.

As with ADR-0011, an in-place rewrite that truncates and regrows past the prior
offset between observations is outside the append-only contract unless it also
changes inode.

## When to revisit

If rebuilding the aggregate or walking the cache tree approaches the detection
budget, retain parsed per-file contributions or add a change notification
interface without moving agent-specific log parsing into consumers. If cursor
state must survive process restarts, define a versioned persisted format rather
than serializing these implementation objects.
