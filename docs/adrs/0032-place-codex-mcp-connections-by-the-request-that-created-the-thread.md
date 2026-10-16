---
id: 0032
title: Place Codex's MCP connections by the request that created the thread, never by time
status: proposed
date: 2026-10-16
supersedes: null
superseded-by: null
---

## Context

Codex keeps MCP startup out of the rollout (`rollout/src/policy.rs`). The
terminal UI and app-server log it to `logs_<n>.sqlite` at their default level:
the handshake with the server's advertised name and version, and each startup
failure with Codex's error. `codex exec` writes no rows at all. Startup lines
carry a process id and no thread id.

In the app-server run, a server that failed slowly logged its failure 1.3
seconds after the thread's first thread-tagged line, so placing lines by a time
window around a thread's start misplaces them. Every startup line is logged
inside the `thread/start` request that created its thread, and another line in
the same request carries that thread's id.

## Decision

`readers/codex_log.py` reads only the startup targets (`rmcp::service`,
`codex_mcp::rmcp_client`), read-only, re-read whenever a rollout is (as the
Claude Code reader re-reads its connection logs). A startup line belongs to the
thread whose own tagged line sits in the same app-server request span
(connection id and request id). A line logged in no request is placed only
where its process served one thread; otherwise the thread's `mcp_log_state` is
`placement_ambiguous`. A thread no row names is `no_log_for_session`, which is
always the case for `codex exec`. Disagreeing lines for one server leave
`connected` unknown.

## Alternatives considered

- **Place by time around the thread's start.** Rejected: slow failures cross
  the window, as measured.
- **Turn on a log file, `RUST_LOG` or OTel.** Rejected: each is a configuration
  change on the developer's machine, and read-only sensing makes none.
- **Infer a connection from the calls.** Rejected: a call says a server
  answered, not how it connected, and says nothing about a server that failed.

## Consequences

Interactive Codex sessions have connections with advertised identity, which
Devin's default log level does not give. Programmatic sessions have none, and
say so. Transport is known only where a failure names an HTTP client; endpoint
is never taken from error text.

## When to revisit

When `codex exec` writes log rows, when Codex names a thread on its startup
lines, or when a log line names a stdio launch.
