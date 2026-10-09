---
id: 0021
title: A call carries its tool class and canonical arguments, filled per kind
status: proposed
date: 2026-10-09
supersedes: null
superseded-by: null
---

## Context

With a second kind, the same act has two names: Claude Code's shell is `Bash`
with `command`, Devin CLI's is `exec` with `command`; Claude Code writes a file
with `Write`/`file_path`, Devin with `write`/`file_path`. Every consumer
matching tool names would have to learn every kind's vocabulary, which is the
per-kind knowledge this package exists to hold.

## Decision

`ToolCall` gains `tool_class`, from a closed vocabulary (`shell`,
`shell_control`, `file_read`, `file_search`, `file_write`, `file_edit`,
`web_fetch`, `web_search`, `delegate`, `skill`, `mcp`, `other`), and
`canonical_arguments` (`command`, `path`, `content`, `new_text`, `url`,
`agent`, `skill`), both filled from per-kind tables in `toolclass`. A name a
table does not know gets no class, never `other`; a key a call did not record
is absent. `tool_name` and `arguments` stay exactly as recorded.

## Alternatives considered

- **Rename every kind's tools to Claude Code's names**: rejected; it makes one
  vocabulary canonical without saying so, has no name for `apply_patch`, and
  loses what the agent actually called.
- **Leave it to consumers**: rejected; each would rebuild the same tables and
  drift.

## Consequences

The tables are a coverage claim and must grow with each kind's tools. A class
says what a tool is, never what a call did.

## When to revisit

If a kind's tool cannot be placed in one class, or a consumer needs a key the
vocabulary lacks.
