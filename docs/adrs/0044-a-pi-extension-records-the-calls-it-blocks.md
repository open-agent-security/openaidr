---
id: 0044
title: A Pi extension records the calls it blocks
status: accepted
date: 2026-11-17
supersedes: null
superseded-by: null
---

## Context

A Pi extension can block a tool call from its `tool_call` handler. Pi then
writes an error result with the extension's reason as its text. That result
has the same shape as a tool's own failure, so the reader reported a blocked
call as `error`. A consumer reads `error` as a call that ran and failed. It
must read a blocked call as one that never ran. Detail:
[`docs/specs/pi.md`](../specs/pi.md#status).

## Decision

An extension that blocks a call writes a Pi custom entry of `customType`
`openaidr.tool-call-blocked` with `data.toolCallId`. The reader reports that
call as `rejected`, with `denial_kind` `extension-blocked`. A result that says
the call succeeded outweighs the entry.

## Alternatives considered

- **Read the reason text of the error result**: rejected because every
  extension writes its own reason. A pattern would also match a tool error with
  the same words.
- **Name the convention after one extension**: rejected because any extension
  that blocks calls can write the entry. A name that belongs to one product
  would also break "nothing proprietary".
- **Let the entry outweigh every result**: rejected because a wrong entry would
  then hide a call that ran. Only a call with no result or an error result can
  be rejected.

## Consequences

A block is `rejected` only when the extension that blocked it writes the entry.
Pi's own example extensions do not write it, so their blocks still read as
`error`. Any extension can write the entry, so the reader trusts extensions as
much as Pi does.

## When to revisit

Revisit if Pi records blocks in the tool result itself.
