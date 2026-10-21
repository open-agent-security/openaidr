---
id: 0038
title: Cursor usage comes from its debug log, placed by the conversation each turn names
status: proposed
date: 2026-10-21
supersedes: null
superseded-by: null
---

## Context

Cursor writes no token usage to its chat store or its transcript. Each root
blob records `token_details.used_tokens`, the size of the context, with no
output count and no cached share. Print mode prints `result.usage` to stdout,
summed over the run. The `stop` hook receives a turn's usage, but only a
configured hook sees it, and in print mode it did not fire.

Cursor's debug log, on by default, writes one `Setting token details for client
token ring` line for each model response, with the model, input, output, cache
read and cache write tokens (Seen). It also writes `agent_cli.turn.start` and
`agent_cli.turn.outcome` lines that name the conversation, the request and the
surface, and a line for each MCP server that failed to load. A sub-agent's
usage lines carry the id of the call that started it. Cursor keeps the 50
newest logs and none older than 7 days.

## Decision

The reader takes usage, the entrypoint, client error times and MCP connection
failures from the debug log, `readers/cursor_log.py`.

1. **A usage line is one response.** It belongs to the conversation of the
   turn in force; a line with a `toolId` belongs to the sub-agent that call
   started. Its id is `<request_id>:<n>`.
2. **Input excludes the cache.** `input_tokens` is `inputTokens` minus cache
   reads and writes, as for Codex (ADR-0031). A cache write of 0 is `None`.
3. **A turn links to a response only when the counts agree** for its request.

## Alternatives considered

- **Context fill as input tokens.** Rejected: it has no output and no cached
  share, so a priced total would charge cached reads at the full rate.
- **The `stop` hook.** Rejected: it needs a hook installed on the developer's
  machine, which read-only sensing rules out, and it did not fire in print mode.
- **Print mode's stdout.** Rejected: only the caller sees it, summed over the
  run.
- **Estimate tokens from the text.** Rejected: this package reports tokens as
  recorded (ADR-0015).

## Consequences

Cursor responses carry real tokens while the log is kept. A session read after
its log was pruned has no responses; a consumer that filed them earlier keeps
them. A session run with `CURSOR_AGENT_DISABLE_DEBUG_LOG` has none, and its
`mcp_log_state` says so. A successful MCP connection writes no line, so only
failures are connections.

## When to revisit

When Cursor writes usage into the store, when the debug log changes its line
shape, or when one process serves several conversations at once.
