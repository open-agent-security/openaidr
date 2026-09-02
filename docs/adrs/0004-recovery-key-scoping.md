---
id: 0004
title: Scope every recovery key to the identity that makes it unique
status: accepted
date: 2026-09-02
supersedes: null
superseded-by: null
---

## Context

This reader joins three sources that the shared event schema cannot carry
between them: upstream's projected `AgentEvent`, its own recovery pass over the
raw JSONL, and Claude Code's per-server MCP connection log. Every one of those
joins is a dictionary lookup, and each key was originally the narrowest thing
that looked like an identifier — a record uuid, a provider call id, a session
id.

Seven rounds of external review on PR #1 found the same defect four times over,
each time in a different key, and each time the report reads identically: the
key is not unique over the population it is looked up in, the last write wins,
and one call silently receives another's provider id, result, `is_error`,
duration, working directory or attribution. Nothing about the output says it
happened. The individual fixes landed as they were reported (session-scoping
the uuid counter, occurrence-keying `call_ids`), which is how the same class
kept reappearing one identifier along.

Measured on one machine, none of these are exotic: 386 of 594 real transcripts
hold more than one session in one file, 5 hold a repeated `(session, uuid)`
pair across 265 records, and 99 of 139 MCP cache directory names have no exact
counterpart under `~/.claude/projects`.

## Decision

Every key used to recover a fact is scoped to the full identity that makes it
unique, and the two sides of a join count occurrence over the same population.
Concretely:

- A record uuid is unique only within its session, so every uuid-keyed
  recovery is keyed `(session id, uuid, occurrence)`.
- **Occurrence is counted only over the records upstream projects into
  `chat_history`**, because that is the population `_turns` counts its own
  occurrence over. `_projects_to_message` restates upstream's filter for this
  purpose, and it is the one place this reader depends on upstream's filtering
  rather than on its output.
- A provider call id is unique only within the session that issued it, so
  `started`, `ended`, `errored`, `denials` and `results` are keyed
  `(session id, call id)`, and duplicate-id withholding is computed per
  session rather than per file.
- An MCP log's session id is unique only within the mangled project directory
  it is filed under, so the log index is keyed `(project, session id)`. A
  session id found under exactly one project is that session's log whatever
  the directory is called; the project name is consulted **only** to break a
  tie between two projects claiming the same id, and where it cannot, the
  whole enrichment is withheld as `session_id_collision` — connections
  included, because here it is the identity itself that is in doubt.

## Alternatives considered

- **Key the MCP index by project and require an exact name match.** Rejected
  on measurement: the two directory manglings are not the same function. The
  cache truncates a long project path and appends a hash suffix
  (`...-agent-local-ditto-1e835041-318a-4985-a288-3baa1e-n0kpsc`); the
  transcript root does not, and 99 of 139 cache directory names have no
  counterpart. Requiring agreement would withhold enrichment from every
  long-pathed project to guard against a collision that appears in none of
  them.
- **Treat a session id as globally unique, since it is a UUID.** Rejected:
  the collector already reports the collision it produces for transcripts.
  Copied, restored and independently rooted project directories are the
  ordinary way one becomes two, and a UUID's uniqueness is a property of how
  it was minted, not of how many times a directory was duplicated afterwards.
- **Count occurrence over every record in the file.** This is what the code
  did, and it is wrong for the reason the whole ADR is about: `_turns` counts
  over `chat_history`, so a record upstream drops — a `system` boundary, an
  `attachment`, a `user` record carrying a tool result — takes an occurrence
  number no turn will ever ask for and shifts every later record with the
  same uuid one place past its own turn's key.
- **Detect divergence between the two counters and withhold.** Rejected as
  strictly worse than agreeing: withholding is the answer when two sources
  genuinely cannot be reconciled, not when they can be made to count the same
  thing.
- **Leave the provider-call-id keys file-global, since `_reused_call_ids`
  already withholds duplicates.** Rejected: it counts records that *issue* an
  id. A second session recording a `tool_result` under an id only the first
  session ever issued produces exactly one `tool_use`, so nothing looks
  duplicated, and the first call reports the stranger's result and a duration
  measured against the stranger's clock.

## Consequences

Every join in this reader now names the scope it is valid in, in its own type.
The cost is real and worth stating: `_projects_to_message` restates a
dependency's private filtering, so an upstream change to which records become
`ChatMessage`s desynchronises the two counters again, silently. It is pinned
against that by the reader's own tests rather than by upstream's contract, and
it is recorded as an upstream ask — a parser that carried a record's identity
through projection would let the restatement go.

Session-scoping the provider-call-id keys narrows duplicate-id withholding
from per-file to per-session, which recovers outcomes for calls that were
previously withheld only because an unrelated session in the same file reused
an id. That is a coverage gain, not a loosening: the cross-session bleed those
withholdings guarded against is now structurally impossible.

The MCP collision path withholds connection facts, which the count-mismatch
path deliberately keeps. The asymmetry is the point and should not be
"corrected" into consistency: a count mismatch means we know which connection
this session had and cannot order its calls; a collision means we do not know
whose connection it is.

## When to revisit

If `adr-sensor` grows a way to carry a record's own identity — file offset,
raw occurrence, or the session it was read under — through projection, the
restatement in `_projects_to_message` should be deleted in favour of it. If
Claude Code ever writes the transcript's project directory into the MCP log
record itself, the tie-break becomes an exact join and the collision state
becomes unreachable.
