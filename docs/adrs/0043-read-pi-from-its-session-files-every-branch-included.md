---
id: 0043
title: Read Pi from its session files, every branch included
status: accepted
date: 2026-11-17
supersedes: null
superseded-by: null
---

## Context

Pi writes one append-only JSONL file for each session. Its entries form a
tree through `parentId`. A person can go back to an earlier entry with `/tree`
and continue from there, and the old branch stays in the file. Pi's own
`buildSessionContext()` walks from the current leaf to the root and gives the
model only that path. Pi's `codemode` tool runs JavaScript that calls other
tools, and Pi records those calls on the script's own result as `nestedCalls`
([Pi](../specs/pi.md#a-codemode-script)).

## Decision

The reader reads every entry of the file in file order, a branch a person left
included. A `codemode` result's nested calls are a turn of their own, keyed by
the result's entry id, one call for each record, and the script itself is a
`script` call (ADR-0036).

## Alternatives considered

- **Read only the current branch, as Pi's context builder does.** Rejected:
  the calls on a branch a person left ran, and changed files or sent data. A
  session that drops them under-reports what the agent did. Which branch is
  current also changes when a person moves the leaf, so a call would leave
  and come back.
- **Key a nested call by its position after the model's calls in the same
  turn.** Rejected: results can arrive out of order, so a later result would
  move an earlier script's calls. The result's entry id never changes.
- **Fold the nested calls into the script call.** Rejected for the reasons
  ADR-0036 gives for Codex: one script can call a shell and an MCP server.

## Consequences

A session can hold calls from two branches that did not see each other. A
consumer that reasons about order reads file order, which is the order they
ran in. A nested call has no result body, since Pi records none.

## When to revisit

When Pi records which branch an entry was abandoned on, or when Pi records a
nested call's result.
