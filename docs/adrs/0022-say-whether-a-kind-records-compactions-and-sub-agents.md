---
id: 0022
title: Say whether a kind records compactions and sub-agents at all
status: proposed
date: 2026-10-09
supersedes: null
superseded-by: null
---

## Context

An empty `compactions` list can mean *no compaction happened* or *this kind
does not record one*; the same holds for sub-agent sessions. With one kind the
two readings coincided. With two they do not, and a consumer reasoning about
what a session's turns can still show needs to know which it has.

## Decision

`Session` gains `compactions_recorded` and `subagents_recorded`, both
defaulting to false. A reader sets each true only where the kind has a record
for it: Claude Code sets both; Devin CLI sets the first, and the second where
its store has `subagent_heads`.

## Alternatives considered

- **Infer from the kind name downstream**: rejected; it moves per-kind
  knowledge out of the package that holds it.
- **Leave the lists empty and document it**: rejected; "absence is not
  falsehood" is enforced in the model elsewhere (`mcp_log_state`), not in prose.

## Consequences

Defaults keep every existing constructor valid. A kind added later reports
false until its reader proves otherwise.

## When to revisit

If a third list acquires the same ambiguity; then the flags should become one
field describing what the kind records.
