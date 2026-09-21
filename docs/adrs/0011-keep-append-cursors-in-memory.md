---
id: 0011
title: Keep append cursors in memory for growing transcripts
status: accepted
date: 2026-09-18
supersedes: null
superseded-by: null
---

## Context

The cold collector reparses every selected transcript on every invocation. That
is the right recovery path, but not the right steady-state path for a daemon
that already knows which session file changed. A synthetic Claude Code session
with 10,000 tool calls took about 9.6 seconds to collect cold; correlation and
deterministic analysis over the resulting model together took about 0.04
seconds. Re-projecting the growing JSONL through `adr-sensor` dominated the
latency and already exceeded a five-second detection budget.

Claude Code transcripts are append-only during ordinary operation, but writes
need not be atomic at the record boundary. A reader can observe the first bytes
of a JSON object before its terminating newline. It must therefore distinguish
bytes read from records committed, or it can briefly report a call result that
the agent has not finished writing.

The dependency exposes a whole-file parser and a private per-record projector;
it does not expose a resumable parser state. OpenAIDR already depends on the
projector's filtering rules in `_projects_to_message` (ADR-0004), so the pinned
dependency version makes that narrow private method a bounded compatibility
surface rather than a new kind of coupling.

## Decision

OpenAIDR provides a stateful `IncrementalCollector`. Given an agent kind and a
transcript path, it retains one in-memory projection and byte cursor per file,
reads only bytes appended since the previous call, and returns the current full
`Session` model. The ordinary windowed `collect` API remains the cold recovery
path and is unchanged.

Only newline-terminated JSONL records advance the committed cursor. An
incomplete trailing record is retained in memory and contributes nothing until
its newline arrives. An inode change or an observed truncation discards the
cursor and rebuilds that file from byte zero. Cursor state is not persisted;
after process restart the first read is deliberately cold.

For Claude Code, appended records are projected with the pinned dependency's
per-record extraction method. OpenAIDR retains the minimum state needed to
reproduce the dependency's tool-result matching semantics, including its
structural-equality behavior, and contract tests compare incremental and cold
results for that edge case.

The recovery pass for fields the dependency drops still reads the committed
prefix, and constructing the returned immutable model is proportional to the
current session size. This decision removes the measured dominant projection
cost; it does not claim every remaining operation is constant-time.

## Alternatives considered

- **Reparse the whole changed file.** Rejected because the measured 10,000-call
  case already exceeds the detection budget before analysis starts.
- **Return only appended turns.** Rejected because result records update calls
  emitted earlier, and consumers need the current session context for
  correlation and rules. A turn-only delta cannot express that update without
  defining a second patch model.
- **Put the cursor in each consumer.** Rejected because JSONL framing,
  truncation, rotation, and the agent-specific projection are reader concerns.
  Duplicating them would make every consumer an agent-log parser.
- **Persist cursors and projected sessions.** Rejected for this stage. It adds
  schema/versioning and transcript-retention concerns to avoid a cold read only
  after process restart; the normal steady-state path needs neither.
- **Wait for a public resumable dependency API.** Rejected because none exists
  in the pinned release and the current latency is already outside the budget.

## Consequences

A long-lived consumer can process a growing transcript without repeatedly
paying the dependency's whole-file projection cost. Cold collection remains a
simple source of truth and restart recovery requires no sidecar database.

The incremental collector is intentionally stateful and must be reused across
calls. Constructing one per filesystem event silently degrades back to cold
reads. Memory grows with the active sessions retained by the process, and an
in-place rewrite that truncates and regrows beyond the previous offset between
two observations is outside the append-only contract unless it also changes
the inode.

The private dependency method is a compatibility risk. The exact behavior is
pinned by incremental-versus-cold tests and by the exact dependency version;
changing that version requires running those tests before release.

## When to revisit

If the recovery pass or full-model construction approaches the latency budget
on measured real sessions, make recovery stateful or introduce a versioned
session-patch model. If cursor state must survive daemon restarts, design a
persisted format with explicit dependency and model versions rather than
serializing implementation objects. If an agent kind stores sessions in a
database or rewrites records in place, give that kind its own incremental
reader contract instead of pretending it is append-only JSONL.
