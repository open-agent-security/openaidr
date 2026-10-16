---
id: 0030
title: Read Codex rollouts on the Claude Code reader's design, outcomes from Codex's records alone
status: proposed
date: 2026-10-16
supersedes: null
superseded-by: null
---

## Context

Codex sessions were absent from the output entirely. `adr-sensor`'s Codex
parser reads 619 of 4,205 calls in a measured corpus, marks every returned call
successful and truncates bodies at 1,000 characters. The unmerged reader in #35
predates the tool class, the absence flags and the Devin work, and its ADR
numbers are now taken.

Codex CLI 0.163.0, run against a mock model, writes one append-only rollout per
thread, a sub-agent's included, as Claude Code writes one transcript per
session. So the Claude Code reader is the design to follow, not Devin's, whose
store is one SQLite file for every session. The run also showed three facts a
reader must get right: a refused command leaves no execution record, only
Codex's `Rejected(…)` envelope as its output; an item can be written before the
call it completes; and every descendant thread names the root thread in
`session_meta.session_id`.

## Decision

An OpenAIDR-owned reader, `readers/codex.py`, on the Claude Code reader's
design: per-file append cursors (ADR-0011) under the same LRU bound
(ADR-0029), `collect_file` returning the file's session on every read, and
`locations()` naming only where rollouts live.

- **A record is a turn**, keyed `o<ordinal>` (else `l<line>`); a call's span is
  `span_id(session, key, 0)`. Outputs and completion items update the call
  whose `call_id` they name and never add a turn. An item written before its
  call opens the turn the call then takes, so no span moves. A `call_id` used
  twice joins neither use.
- **A call's outcome comes only from Codex's records**: its completion item
  (`CommandExecution`, `FileChange`, `McpToolCall`, ...), the header Codex
  writes above an output (`Process exited with code N`), or the rejection
  envelope at the start of the output, whose reasons name a person, the
  Guardian reviewer, a hook, an exec-policy rule or a built-in rule. Output
  that is none of these is `unknown`.
- **A sub-agent is `codex:<root>:<child>`**, its root read from
  `session_meta.session_id` at any depth.
- **A client error is a turn with role `error`**: `task_complete.error`'s
  message, at the record's time, as Claude Code writes its API errors into its
  transcript.
- **A patch's files are the canonical key `edits`**, one `{path, kind,
  new_text}` per file, since one canonical `path` cannot describe a patch that
  touches several.

## Alternatives considered

- **Register `adr-sensor`'s Codex parser.** Rejected: it drops `custom_tool_call`,
  where `apply_patch` lives, and reads every returned call as successful.
- **Follow the Devin reader: rebuild every session on any event and re-emit
  what differs.** Rejected: rollouts are per thread and append-only, so a
  per-file cursor reads only what was appended, and a cold rebuild of every
  thread per event reads every file again.
- **Leave every output with no item `unknown`.** Rejected: a refused command
  has no item, so every refusal would read as an unknown outcome, and a
  consumer could no longer tell a refusal from a failure.
- **Name a sub-agent by its immediate parent.** Rejected: a depth-two child's
  run would then be a sub-agent, not the root.
- **Merge a call into the preceding assistant message**, as `adr-sensor` does.
  Rejected: the call takes its neighbour's time, and one turn's content grows
  as later records arrive.

## Consequences

Codex sessions arrive through both collectors with full arguments and results,
stable spans, real statuses and denial kinds, per-call working directories,
exit codes and durations. Legacy (pre-paginated) rollouts keep exit codes only
in Codex's output header, which the reader reads. Compressed `.jsonl.zst`
rollouts are reported, not read. No record names a skill a call served, so
`attributed_skill` is `None`.

## When to revisit

When Codex persists its execution events again in a way that disagrees with
the header, when compressed rollouts become the default, or when a rollout can
be rewritten in place rather than replaced by a new file.
