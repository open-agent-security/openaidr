---
id: 0024
title: Enrich Codex from durable structured records
status: accepted
date: 2026-09-29
supersedes: 0023
superseded-by: null
---

## Context

ADR-0023 established the owned Codex reader but omitted structured events and
context. Claude already fills the corresponding shared model fields. Codex's
plain function/custom outputs still omit success, but persisted completion
records carry explicit outcomes.

Verified against Codex source at
[`e07e58c8429019de78b138d7138deaaf7f3ef22c`](https://github.com/openai/codex/tree/e07e58c8429019de78b138d7138deaaf7f3ef22c):
`codex-rs/rollout/src/policy.rs` distinguishes persisted events from transient
ones; `codex-rs/protocol/src/items.rs`, `models.rs`, and `protocol.rs` define
the record shapes. Local schema inspection also found command IDs distinct
from the outer code-tool call IDs. Fixtures remain synthetic.

## Decision

Retain ADR-0023's reader, root discovery, identity, inherited-history handling,
append-only cursor, complete-record failure handling, body preservation and
privacy rules. Replace its outcome and context exclusions as follows:

- Project persisted `item_completed` command, file-change and MCP facts, plus
  legacy patch/MCP completion events. Match only a unique provider ID within
  the session, across call families. An explicit different thread ID is ignored.
  Ambiguous calls/outputs remain withheld. Updating a call preserves its span.
- A completion with its own execution ID creates its own physical-line turn
  when no raw call has that ID. This covers executions nested inside code tools.
  Never attach one to an outer call by position. Repeated completion snapshots
  update that execution rather than create another call.
- Map explicit completion, failure, decline, nonzero exit code and MCP `isError`
  into the shared statuses. Plain function/custom output alone remains unknown;
  its wording is not a success or refusal signal. Preserve full raw outputs
  when present. Durations remain call-to-output round trips; process execution
  durations are not substituted for that different interval.
- Capture base instructions and developer/system messages as local context.
  Capture the first explicit user-message event (legacy or paginated) as the
  initial prompt, rather than mistake injected user-role material for it.
  Record approval policy and sandbox type on subsequent turns as permission
  mode. Record compaction summaries and boundaries, with trigger `unknown`
  and absent token counts. Never replay replacement history.
- Collect native tool-search calls/outputs by family and ID, and hosted web
  search records. Missing tool-search IDs do not justify a positional join.
  Preserve MCP identity, per-call cwd and plugin attribution where explicit.

## Alternatives and limits

Parsing every protocol event would imply unavailable coverage: current rollout
policy omits MCP startup and provider-reroute events. MCP connection metadata,
transport, and provider refusals therefore remain unavailable. Title indexes,
skill attribution, compressed/external history and other native tool families
remain outside this reader's coverage. Unsupported response call/output families
continue to report gaps. Reasoning and agent-to-agent messages are not projected.

Copying Claude's recovery passes would duplicate agent-specific assumptions.
Use the existing shared model, collectors, discovery, timestamps, spans and
rendering instead. Codex wire normalization stays in its reader. No dependencies,
model schema changes, network access or upload path are introduced.
