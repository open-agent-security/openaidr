---
id: 0018
title: Read Devin CLI from its SQLite store, read-only, inside one transaction
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
state and never a half-written message. Growth is followed with a per-session
cursor on `message_nodes.row_id`, held in memory only: a session whose highest
`row_id` rose is rebuilt in full. A watcher's `sessions.db-wal` path is accepted,
because a WAL commit changes only that file. A store whose identity changes
(device, inode, or the first migration's `applied_on`) or whose rows go
backwards is read cold.

## Alternatives considered

- **Export sessions with the program's own `--export`**: rejected because it
  runs the agent's binary. This package reads files; it never executes the
  agent.
- **Read `transcripts/*.json`**: rejected because those files are export-only
  snapshots of a subset of the store, written only when asked for.
- **Open read-write to checkpoint the WAL first**: rejected; read-only sensing
  is a repository rule, and a read transaction already sees the WAL.

## Consequences

A schema change upstream surfaces as a reported unsupported-schema failure
rather than a wrong answer; a migration newer than the one the reader was built
against (V17) is read for the known columns and reported. A store locked past a
short timeout is reported as busy, not read as empty.

## When to revisit

If Devin moves session state out of SQLite, or a migration renames a column
this reader requires.
