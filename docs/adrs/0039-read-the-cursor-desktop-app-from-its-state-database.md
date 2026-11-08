---
id: 0039
title: Read the Cursor desktop app from its state database with the CLI's decoder
status: proposed
date: 2026-10-26
supersedes: null
superseded-by: null
---

## Context

The Cursor desktop app's agent panel is the surface most Cursor users use, and
its conversations were not read (spec C9). Cursor 3.27.12 ran here against a
mock model, with its agent loop on the machine. It wrote every conversation to
one SQLite database, `<user data>/User/globalStorage/state.vscdb`, table
`cursorDiskKV`. Each conversation is a `composerData:<id>` row. Its
`conversationState` field holds the same `ConversationStateStructure` root that
the CLI keeps in a blob. The root lists `agentKv:blob:<hex>` rows that are the
CLI's blobs byte for byte: JSON prompt messages with Cursor's typed outcome,
and protobuf turns.

The same database also holds `bubbleId:*` rows, the panel's own projection of
each conversation. A bubble has Cursor's internal tool name, the raw arguments
and a result string, but not the typed outcome.

The desktop app writes no token usage to the database. Its structured log
writes the CLI's usage line for each model response, with no conversation id.
One window runs several conversations at once.

## Decision

1. **The desktop app is a second container for the CLI's decoder.** The reader
   takes the root from `composerData.conversationState`, else from
   `agentKv:checkpoint:<id>`, and the blobs from `agentKv:blob:<hex>`. Every
   rule after that (turns, keys, spans, statuses, tool names) is the CLI's.
2. **One kind, `cursor`,** as ADR-0037 decides. A desktop conversation is
   `cursor:<composerId>`; a sub-agent is `cursor:<root>:<child>`.
3. **Usage comes from the structured log, placed only when it is
   unambiguous.** A usage line belongs to the one turn open in its file, or to
   the one sub-agent span open in it. With two open, or none, the line is not
   placed.
4. **The entrypoint is `ide`.** Only the desktop app writes this database, and
   a person starts each conversation in it. A conversation that an automation
   or a background agent created has none.

## Alternatives considered

- **Read the bubbles.** Rejected: they hold no typed outcome, so a status would
  come from Cursor's result strings, which ADR-0037 rules out. Their tool names
  are internal (`run_terminal_command_v2`), not the model-facing names the tool
  table and every consumer use.
- **Register `adr-sensor`'s Cursor parser.** Rejected for the reasons in
  ADR-0037: it reads the bubbles, keys turns by position and opens the
  database read-write.
- **A second kind, such as `cursor-ide`.** Rejected: the two surfaces share one
  configuration tree, one conversation format and one OpenACA BOM. A consumer
  that wants the surface reads the entrypoint.
- **Context fill as usage.** `promptTokenBreakdown.totalUsedTokens` and the
  root's token details are the context's size. Rejected for the reason in
  ADR-0038: there is no output count and no cached share.
- **Place a usage line by the turn last started,** as the CLI's debug log is
  placed. Rejected: one window runs several conversations at once, so the turn
  last started is often another conversation's. A silent wrong answer is worse
  than a gap.
- **No entrypoint for the desktop app.** Rejected: a consumer could not tell a
  person at the panel from a program, and the store itself establishes it.

## Consequences

Desktop conversations arrive with the CLI's full arguments, results, typed
statuses, denial kinds, exit codes, durations, sub-agents and client errors.
They have usage only where the agent loop runs on the machine (the spec's
C11): the Private Inference build, and the standard app where Cursor's agent
host runs the loop. In the standard app with the loop on Cursor's server, a
desktop session has no responses. Lines written while two conversations run at
once are dropped. One database holds every conversation, so the reader keeps
each decoded conversation against a digest of its `composerData` value.

## When to revisit

When the desktop app writes usage to its database, when a usage line names its
conversation, or when the desktop app and the CLI diverge in their blob format.
