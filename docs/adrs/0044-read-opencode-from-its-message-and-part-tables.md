---
id: 0044
title: Read OpenCode from its message and part tables
status: accepted
date: 2026-11-17
supersedes: null
superseded-by: null
---

## Context

OpenCode 1.19.5 keeps sessions in `opencode.db`. It projects each session
event into two sets of tables: `message` and `part`, one JSON row for each
message and part, and the newer `session_message`, for a runtime under
development in `packages/core`. The agent loop in `packages/opencode` publishes
`message.updated` and `message.part.updated` and reads `message` and `part`
(`session/message-v2.ts`). OpenCode 0.x kept the same messages and parts as
JSON files under `storage/` ([OpenCode](../specs/opencode.md#the-tables)).

## Decision

The reader opens `opencode.db` read-only, in one transaction for each pass, and
reads `session`, `message` and `part`. A message is a turn keyed by its id, and
a call's span is its tool part's position among its message's tool parts.

## Alternatives considered

- **Read `session_message`.** Rejected for now: the shipped agent loop does
  not read it, and its rows change shape between releases. The `part` rows
  are the shape OpenCode's own API serves.
- **Read the 0.x JSON files.** Rejected: OpenCode 1.x moves them into the
  database, and a second reader for an old layout would read the same
  sessions twice.
- **Ask a running OpenCode server for its sessions.** Rejected: OpenAIDR reads
  what agents write to disk and starts nothing (read-only sensing).

## Consequences

The reader depends on table and column names, not on the JSON schema alone.
A migration that renames them is an unsupported-schema failure, reported and
not guessed around.

## When to revisit

When OpenCode's agent loop moves to `session_message`, or stops writing
`message` and `part`.
