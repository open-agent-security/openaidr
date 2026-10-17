---
id: 0036
title: A Code Mode cell is a script, and the tools it runs are their own calls
status: proposed
date: 2026-10-17
supersedes: null
superseded-by: null
---

## Context

Every model in Codex 0.163.0's catalogue declares `tool_mode:
"code_mode_only"`. The model calls one tool, `exec`, with JavaScript, and the
script calls the real tools. Codex's `submit_nested_tool`
(`core/src/tools/code_mode/mod.rs`) dispatches each of them through the same
tool runtime a direct call uses, tagged `ToolCallSource::CodeMode`, so each
writes its own completion item: an `McpToolCall`, a `CommandExecution` or a
`FileChange`, with an id of its own. The cell's output opens with a status
line from `format_script_status`: `Script completed`, `Script failed`,
`Script terminated`, or `Script running with cell ID …`.

The reader already turned each nested item into its own call (ADR-0030: an
execution no call names is its own call). The cell itself was a
`custom_tool_call` named `exec` that no table knew: no tool class, and status
`unknown`, since nothing read its status line. A session read as `exec`,
failed call, `exec`, failed call, `exec`, failed call. A consumer asking
whether the agent did the same thing three times in a row saw three unrelated
calls between them. Seen in a live run: three identical failed MCP calls
returned no retry finding.

## Decision

**A Code Mode cell is classed `script`, a new member of the closed tool-class
vocabulary (ADR-0021): a built-in that runs other tools, each of which the
agent records as its own call.** Codex's `exec` is the only entry today.

**A cell's status is its own status line,** read before anything the script
printed: `ok`, `error`, `interrupted`, or `pending` while it is still running.
It is never the outcome of a tool the script ran, which is that tool's own
call. Only a `custom_tool_call` named `exec` is read this way; another tool
whose output opens with the same words keeps its own outcome.

The nested calls are unchanged. The cell stays in the session, at its own
span, because a script that failed before its tool ran is the only record of
that attempt.

## Alternatives considered

- **Drop the cell and keep only its nested calls.** Rejected: a script that
  fails before its tool runs, or one that only reads `ALL_TOOLS`, writes no
  nested item, so the attempt would vanish. It would also make the reader
  decide what a session means, which is a consumer's job.
- **Fold the nested calls into the cell, as one call.** Rejected: one cell can
  run several tools, each with its own server, arguments and outcome. A cell
  that runs a shell command and an MCP call has no single name or class.
- **Class the cell `other`.** Rejected: `other` says a built-in fits no class.
  A consumer reading an ordered run of calls needs to know this one is a
  container whose tools appear separately, which `other` does not say.
- **Join each nested item to its cell.** Rejected for now: Codex links a
  nested call to its cell by `cell_id` in its own runtime, and the rollout
  records neither on the item. Linking them by position would guess.
- **Class Code Mode's `wait` too.** Deferred: no `wait` cell was observed in
  any recorded session, and `wait` is also a collaboration tool's name.

## Consequences

A consumer that reads calls in order can step past a `script` call, since the
tools it ran are already in the session. A session from a Code Mode model now
has every cell with a class and an outcome Codex recorded, where before each
was `unknown`.

A script that failed before its tool ran is one `error` call of class
`script`, with no nested call. A consumer can read it as an attempt that did
not reach a tool.

## When to revisit

When a recorded session shows a Code Mode `wait` cell. When Codex records the
cell on its nested items, so the two can be joined by Codex's own records.
When Codex changes `format_script_status`.
