---
id: 0018
title: Read Devin CLI from its SQLite store read-only; re-emit what differs
status: proposed
date: 2026-10-09
supersedes: null
superseded-by: null
---

## Context

Devin CLI (and Devin Local, and editors driving it over ACP) keeps every session
in `<data>/sessions.db`, a SQLite database in WAL mode, written while the agent
runs. `adr-sensor` has no Devin parser. A reader has to see a consistent store
while it is being written, and has to follow it as it grows (ADR-0011).

## Decision

An OpenAIDR-owned reader opens `sessions.db` with `mode=ro` and reads every
table it needs inside one read transaction, so one pass sees one committed
state and never a half-written message.

**Growth is followed by comparing outputs, not by reasoning about inputs.** On
any watcher event under the data root -- `sessions.db`, `sessions.db-wal` (a
WAL commit changes only that file), a run log, a session lock -- `collect_file`
rebuilds every session and returns each one whose digest differs from the one
it last returned under that identity, held in memory only. The invariant, and
the test that holds it: after any event, a consumer that replaced the sessions
returned holds exactly what a cold read returns.

## Alternatives considered

- **Export sessions with the program's own `--export`**: rejected because it
  runs the agent's binary. This package reads files; it never executes the
  agent.
- **Read `transcripts/*.json`**: rejected because those files are export-only
  snapshots of a subset of the store, written only when asked for.
- **Open read-write to checkpoint the WAL first**: rejected; read-only sensing
  is a repository rule, and a read transaction already sees the WAL.
- **A per-session `row_id` cursor, plus re-placing the peers an event could
  affect**: rejected after review found four peer cases in turn. A session's
  placement depends on every other session's lock (ADR-0020), so a new
  session, a lock moving to another process, an unreadable lock or a new log
  can each change a session whose own rows did not change, and each arrives as
  a different watcher path. Enumerating which inputs an event touched misses
  the next case; comparing what each session now is cannot.

## Consequences

An event costs a rebuild of the whole store, which a pass already reads in
full; a session's digest is held for the reader's lifetime. A session deleted
from the store is not announced. If the rebuild is measured to matter, the
digest can be keyed on each session's inputs instead, provided the key covers
everything a session is built from, placement included.

A schema change upstream surfaces as a reported unsupported-schema failure
rather than a wrong answer; a migration newer than the one the reader was built
against (V17) is read for the known columns and reported. A store locked past a
short timeout is reported as busy, not read as empty.

## When to revisit

If Devin moves session state out of SQLite, or a migration renames a column
this reader requires.
