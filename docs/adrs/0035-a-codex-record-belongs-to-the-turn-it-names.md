---
id: 0035
title: A Codex record belongs to the turn it names, the turn in force only when it names none
status: proposed
date: 2026-10-16
supersedes: null
superseded-by: null
---

## Context

Codex can complete a call after a newer turn has begun, and names the call's
own turn on the event: `ItemCompletedEvent.turn_id` and each legacy
`*_end` event's `turn_id` are required fields (`protocol/src/protocol.rs`),
and Codex's own `ThreadHistoryBuilder` keeps earlier turns so late completions
can reuse them (`app-server-protocol/src/protocol/thread_history.rs`). The
reader filed a completion with no call record under the turn in force instead,
so a late one took the newer turn's response and read as `pending` after its
own turn had ended (review on open-agent-security/openaidr#76).

## Decision

A record's turn is the one it names: a response item's
`internal_chat_message_metadata_passthrough.turn_id`, an event's `turn_id`.
The turn in force, from the last `turn_context` or `task_started`, is used only
for a record that names none. The turn decides a call's ended state
(`pending` or `unknown`) and the response it waits for.

A web search's `results` are kept whole as JSON, never read as text blocks:
Codex keeps them opaque so new fields pass through (`WebSearchItem`,
`protocol/src/items.rs`). This refines ADR-0034's per-field result.

## Alternatives considered

- **The turn in force for every record.** Rejected: it is wrong for exactly
  the late completions Codex says it writes.
- **Join a text block's `text` for any list of objects that all have one.**
  Rejected for `results`: a result's URL and title would be dropped whenever
  each result also carried `text`.

## Consequences

A late completion's call carries its own turn's response and is not
`pending` once that turn ended. A web search's result is its JSON as written.
