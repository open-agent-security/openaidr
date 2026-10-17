---
id: 0034
title: A Codex item's result is its own field, an empty one included
status: proposed
date: 2026-10-16
supersedes: null
superseded-by: null
---

## Context

ADR-0033 put every Codex 0.163.0 call item in one table, but left reading a
result to a loop that tried `aggregated_output`, `stdout`, `result`,
`content_items` and `results` in turn, skipping any that were empty. A result
recorded as `""` or `[]` fell through to the next field and then to `None`.
Review on open-agent-security/openaidr#76 found it twice, for a web search's
`results: []` and then a dynamic tool's `content_items: []`. A test asking
every item at once found it for three more: an empty command output, an empty
patch `stdout`, and an empty image result.

## Decision

Each entry in `_CALL_ITEMS` names the one field its item records its result in
(`docs/specs/codex.md#completion-items`). Present, even as `""` or `[]`, that
field is the call's result; missing or `null`, the call has none. An MCP
item's `result` is a `CallToolResult`, whose `content` is the result. No other
field is read as a result, and no field falls back to another.

## Alternatives considered

- **Keep the loop, testing each field with `is not None`.** Rejected: the
  loop's order, not the item's type, would still decide which field is the
  result, and a field one item uses for its result another may use for
  something else (an MCP item's `result` is a `CallToolResult`, an image's a
  base64 string).
- **Treat an empty value as no result.** Rejected: a search that found
  nothing and a call that recorded nothing are different outcomes, and
  absence is not falsehood (AGENTS.md).

## Consequences

An empty recorded output now has `result` `""` or `"[]"` and a `result_size`.
A field Codex adds to an item is not read until the table names it.
