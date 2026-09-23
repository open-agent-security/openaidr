---
id: 0013
title: Fold appended MCP log lines into per-server state
status: accepted
date: 2026-09-23
supersedes: 0012
superseded-by: null
---

## Context

ADR-0012 gave the incremental Claude Code reader one in-memory byte cursor per
MCP connection log, then rebuilt a fresh `MCPLogIndex` from every cached
complete line on each pass. It named the cost it left in place — rebuilding
scales with all retained lines — and the condition for revisiting it: if that
rebuild approaches the detection budget, retain parsed per-file contributions.

It did. On a machine with a real Claude Code cache (8,109 log files, 94,535
lines) the rebuild alone took about 0.77s per pass. A long-lived collector that
polls a session once a second therefore spent most of a core re-deriving an
index that one appended line had barely changed. Reusing the previous index
when nothing moved removed the idle cost, but an active session appends to its
logs continuously, so the append path still paid the whole rebuild.

## Decision

The incremental reader keeps ADR-0012's cursors unchanged — one in-memory byte
cursor and complete-line cache per log file, newline-terminated records only,
an inode change or truncation resetting that file, a vanished file dropped,
nothing persisted — and replaces the per-pass rebuild with an incremental fold.

Every key a log record can touch in `SessionMCPLogs` belongs to one server, so
the files of one `(project, server)` pair fold independently of every other
pair. Each pair keeps an unsealed fold of its files in the cold reader's order.
When the only change to a pair is complete lines appended to its last file
within the same cursor epoch, only those lines are folded into it. Any other
change to a pair — a file added, removed, reset or unreadable, or growth in a
file before the last — refolds that pair from its cached lines. The exported
index is assembled from sealed copies of each session's pairs, and a session
none of whose pairs changed keeps its previous sealed copy. An undecodable line
is judged torn once a later record follows it in the same file, which is what
the cold reader decides when it sees the whole file.

## Alternatives considered

- **Keep rebuilding from cached lines on every change (ADR-0012).** Rejected:
  that is the measured cost, and it is paid on the path a live session is on.
- **Mutate the previously exported index.** Rejected, as in ADR-0012: an
  exported index has sealed its unresolved waits into outcomes, and a consumer
  may still hold it. The unsealed fold is separate state; only copies of it are
  sealed and handed out.
- **Fold incrementally per file rather than per pair.** Rejected: a server's
  log is split across one file per run, and the in-flight state that detects
  overlapping calls carries from one file into the next, so a file's
  contribution is not independent of the files before it.
- **Share one fold across all pairs and patch it.** Rejected: undoing a pair's
  earlier contribution to a shared structure is the same coupling to
  aggregation internals ADR-0012 refused.

## Consequences

An append to one log costs time proportional to the appended lines plus the
sessions they touch, not to every retained line. Discovery still walks the
cache tree and stats every file on each pass, which is now most of what a pass
costs; that is the next thing to revisit.

A randomized test compares the incremental index with a rebuild from the same
cached lines after every step, and with a cold read wherever every file ends in
a newline. While a file ends in an unterminated fragment the two readers can
disagree about whether the line before it is a torn record — the cold reader
counts the fragment as a line — and they agree again once the newline arrives.
That difference predates this decision.

Memory now also holds one unsealed fold per pair and one sealed copy per
session, in addition to the cached lines.

## When to revisit

If walking the cache tree approaches the detection budget, skip directories
whose metadata has not changed or add a change-notification interface, keeping
agent-specific log parsing in this package. If cursor or fold state must
survive process restarts, define a versioned persisted format rather than
serializing these objects.
