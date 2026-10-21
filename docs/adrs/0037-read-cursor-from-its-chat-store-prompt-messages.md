---
id: 0037
title: Read Cursor from its chat store; a prompt message is a turn, and Cursor's typed outcome is the status
status: proposed
date: 2026-10-21
supersedes: null
superseded-by: null
---

## Context

Cursor sessions were absent from the output. `adr-sensor`'s Cursor parser reads
the desktop app's bubbles. Measured on a real `state.vscdb`, it keys 2,143 of
2,165 messages by position, keeps 0 of 1,178 result bodies, sets no directory
or model, and opens the database read-write.

The Cursor CLI, run against a mock model, writes one SQLite store for each
conversation, a sub-agent's included: content-addressed blobs and one `meta`
row. The root blob lists JSON prompt messages, the messages the model
received. Each assistant message carries its tool calls with their ids and
arguments. Each tool message carries the result text and Cursor's typed
outcome as JSON (`success`, `failure` with `exitCode`, `rejected` with a
reason). The protobuf turns carry each call's start and end time. The same
CLI also writes a JSONL transcript, which has no results, ids or times.

The model-facing tool names change with the model family: `gpt-5.5` sees
`ReadFile` and `ApplyPatch`, `claude-sonnet-4-5` sees `Read`, `Write` and
`StrReplace`.

## Decision

An OpenAIDR-owned reader, `readers/cursor.py`, of the CLI's chat stores,
opened read-only.

1. **A prompt message is a turn.** An assistant message is keyed by its own
   `id`, any other by its blob id, with an occurrence suffix for a repeat.
   A tool message updates the call whose `toolCallId` it names and adds no turn.
2. **A call's status is Cursor's typed outcome**, read from
   `highLevelToolCallResult.output`, never from the result text. An MCP
   `success` with `isError` is `error`. A refusal's `denial_kind` comes from
   its reason.
3. **Times come from the protobuf steps**, decoded for the few fields the
   reader needs, joined on `tool_call_id`.
4. **One kind, `cursor`,** for the CLI and the desktop app, as OpenACA names
   it. The desktop app's store is a second location for the same decoder once
   a real store confirms its envelope.
5. **A sub-agent is `cursor:<root>:<child>`**, from `meta.subagentInfo`.

## Alternatives considered

- **Register `adr-sensor`'s Cursor parser.** Rejected: positional keys move a
  span when Cursor rewrites a bubble, and it keeps no results.
- **Read the transcript JSONL.** Rejected: it has no results, call ids,
  timestamps or outcomes, and Cursor rewrites it in full after a compaction.
- **Read only the protobuf steps.** Rejected: the typed call has about 76
  cases, each with its own argument and result messages, so a schema table
  would mirror Cursor's protocol. The JSON messages carry the same outcome in
  a shape the reader already reads.
- **Two kinds, one for the CLI and one for the desktop app.** Rejected: they
  share one configuration tree and one conversation format, and OpenACA
  builds one `cursor` BOM for both.
- **Classify by the model-facing name alone.** Kept, with both spellings in
  the table, since the typed case is not in the JSON message.

## Consequences

Cursor CLI sessions arrive through both collectors with full arguments and
results, stable spans, real statuses and denial kinds, exit codes and
durations. A new tool name is unknown, not misclassified. The desktop app is
not read until its envelope is confirmed (spec C9).

## When to revisit

When Cursor stops writing `highLevelToolCallResult` into the prompt messages,
when the desktop app's store is confirmed, or when the CLI and the desktop app
diverge in format.
