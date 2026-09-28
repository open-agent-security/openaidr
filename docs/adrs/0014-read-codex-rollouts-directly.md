---
id: 0014
title: Read Codex rollout records directly and preserve their evidence boundaries
status: accepted
date: 2026-09-28
supersedes: null
superseded-by: null
---

## Context

The pinned `adr-sensor==1.0.0` Codex parser drops custom tool calls, merges
separate response records into mutable assistant messages, truncates bodies,
and declares any output successful. These limitations were verified in its
`codex_parser.py`, not inferred from its advertised format support.

Codex's public source was checked at
[`46fdd5ef39735f4159cdcf0ec5e85c10521494e5`](https://github.com/openai/codex/tree/46fdd5ef39735f4159cdcf0ec5e85c10521494e5):
`codex-rs/protocol/src/models.rs` defines function/custom calls and serializes
output as a string or content array, omitting the internal success flag;
`codex-rs/protocol/src/protocol.rs` defines metadata and subagent history
boundaries; `codex-rs/rollout/src/recorder.rs` appends newline-terminated JSON.

## Decision

Use an OpenAIDR-owned reader for Codex, on the existing cold and incremental
contracts. The first `session_meta.id` supplies `codex:<id>`; Codex child
threads have their own ids, unlike Claude Code subagent transcripts. Copied
metadata inside a child's inherited prefix does not replace that identity.
The recorded `subagent_history_start_ordinal` excludes inherited parent
activity. Referenced `history_base` prefixes are not followed and are reported
as incomplete history. The collector reports duplicate session ids across files.

Each user/assistant message or function/custom call record produces one turn.
Physical line number supplies the turn key, including skipped or blank lines
in the count. The span is the existing `session:turn_key:call_index` shape.
Appending records cannot change it; rewriting/reordering a file is outside
that identity contract. Results join within a file/session on call family and
provider call id. Reused call ids or duplicate outputs withhold result and
duration, without moving any previously emitted span. Unlike the dependency's
Claude parser, this reader does not match calls by structural equality.

Calls without an unambiguous output are `pending`; returned calls are
`unknown`. Output prose is not parsed as a success, rejection or interruption
signal. Body arrays are JSON-encoded into the model's string field without
elision. Durations use the issuing and output record timestamps only. Negative
or missing round trips remain absent. Times follow ADR-0010's UTC rule.

Incremental reads commit only newline-terminated records and retain their
projection in memory. Inode replacement or observed truncation resets cold.
Malformed complete records withhold the file's session and report its path and
line number, without echoing record content: an unread record may conceal a
second claimant of a call id. A torn final record waits for its newline.

## Alternatives considered

- **Register the dependency parser unchanged.** Rejected because custom calls
  disappear and returned errors become success claims. Adding recovery passes
  would duplicate almost the entire parser while retaining its mutable turns.
- **Merge calls into the previous assistant message.** Rejected because that
  assigns a call its neighbour's timestamp and makes one turn's content grow
  through later records. Record-level turns state exactly what was recorded.
- **Guess success from returned text or custom-call `status`.** Rejected:
  serialized output omits the execution success flag, and a completed model
  call item is not proof that the invoked tool succeeded.
- **Replay compaction replacement history as fresh calls.** Rejected because
  it is model context, not a second execution of those calls.
- **Resolve new callable namespaces to MCP servers by splitting names.**
  Rejected because these may be sanitized/hashed aliases. Only legacy flat
  `mcp__server__tool` names are split; namespaced names stay whole.

## Consequences

Codex's function and custom tool calls are available through both collectors
and the existing CLI, with full local arguments/results and stable append
identity. No dependency or model schema change is needed.

This is bounded rollout coverage, not all Codex persistence. Compressed
`.jsonl.zst` files and unsupported response tool-call/output families produce
reported gaps. Archived sessions are read only when their directory is supplied
as the root. Reasoning, event-message mirrors, injected context, compaction
summaries and agent-to-agent messages are not projected. There is no MCP
connection enrichment, refusal recovery, or outcome-text classification.
The existing CLI privacy boundary remains: prompt/argument/result text is
withheld, while its JSON activity view still includes the working directory;
`public_descriptor()` excludes that too.

## When to revisit

When Codex exposes a stable session export API, when another response family
needs coverage, or when outcomes can be recovered from a documented structured
signal. Referenced history, compressed archives, and in-place rewrites require
separate designs rather than silent extensions of the append-only contract.
