---
id: 0003
title: Read Claude Code's MCP connection logs, and withhold outcomes the ordinal join cannot place
status: accepted
date: 2026-09-01
supersedes: null
superseded-by: null
---

## Context

The transcript is silent about MCP in the ways that matter most. Measured on a
real 537-session corpus: of 130 MCP tool calls, **129 read `unknown`** despite
every one having returned, so a successful MCP call could not be told from a
failed one. Nothing records which transport carried a call, nothing records what
the server calls itself, and nothing records that a connection failed to come up
at all — one server in that corpus had failed **419 times** on a malformed
authorization header with no trace of it anywhere a consumer could see.

This matters beyond data quality. Everything else knowable about an MCP call —
its tool name, its argument keys — comes from a schema the *server* authored, so
a consumer reading them restates what the component says about itself rather
than observing anything. The transport is the exception: it is negotiated and
recorded by the *client*, so it is independent evidence, and it is the only
thing about an MCP call that can be observed at all.

Claude Code writes all of it to per-server JSONL logs under its CLI cache
(`<cache>/<mangled-project-dir>/mcp-logs-<server>/`), tagged with the
`sessionId`. Measured against the same corpus, those logs cover **130 of 130**
MCP calls with no gaps.

## Decision

OpenAIDR reads those logs as best-effort enrichment for the `claude-code` kind,
in `readers/claude_code_mcp.py`.

Connection-scoped facts — transport, endpoint, the server's advertised name and
version, whether it connected, why it did not — become `Session.mcp_connections`.
Call-scoped facts fill `ToolCall.status`, `ToolCall.duration_ms` and the new
`ToolCall.transport`.

**A second source only ever fills a gap.** A status the transcript itself stated
is evidence from the session and is never overwritten; the log resolves
`unknown` and nothing else. The same rule governs duration, where the two sources
measure different things — the transcript times the round trip, the log times
the tool's execution.

**Outcomes are attributed by position, guarded by a count.** The logs carry no
span and no call id, only an ordered run of `Tool 'x' completed`/`failed` lines,
so the Nth call to a tool in the transcript is matched to the Nth log record for
that tool. That is sound only while both sides recorded every call, so per-tool
counts must agree before any outcome is attributed. Where they do not, the
connection-scoped facts are kept — they are properties of the connection and a
miscounted call sequence does not touch them — and the per-call half is withheld.
The comparison runs only over tools the transcript actually calls: the client
makes MCP calls of its own (`closeAllDiffTabs`, `getDiagnostics` against the IDE
server) that never appear in a transcript, and outcomes are queued and popped by
tool name, so a log-only tool cannot shift one that is in both.

**Every way this falls short is a state, not a silence.** `Session.mcp_log_state`
carries `applied`, `no_log_root`, `no_log_for_session`, `count_mismatch` or
`not_attempted`, and the CLI prints the breakdown even when nothing went wrong.

**A failure's category travels; the server's words do not.** A connection failure
message is free text an MCP server author chose and can carry a URL or a header
fragment, so `failure_detail` is `LOCAL_ONLY` while `failure_category` — one of
`auth`, `timeout`, `http_status`, `network`, `protocol`, `unknown` — is not.

## Alternatives considered

- **Leave MCP calls as they are.** Rejected: it is the one class of call where
  the transcript states no outcome, and the gap was measured at 129 of 130. A
  field that is `unknown` for a whole call class is not a coverage statement any
  more, it is a blind spot.
- **Attribute outcomes positionally with no guard.** Rejected, and this is the
  decision most likely to be re-suggested because the join is exact on today's
  corpus. One dropped log line shifts every later outcome in that session onto
  the wrong call, with nothing to show for it — the same silent-misattribution
  failure ADR-0001 and ADR-0002 both refuse. A gap that says so beats an
  attribution that cannot be checked.
- **Compare whole per-tool count maps rather than only the transcript's tools.**
  Rejected after it fired on real data: the client's own IDE traffic appears in
  the log and never in a transcript, so this read routine behaviour as
  corruption and withheld outcomes over nothing.
- **Interpret the transport here** — mark a `claudeai-proxy` call as network
  egress. Rejected: this package answers what an agent did, never what it means.
  `stdio` in particular proves nothing either way, since a stdio server can still
  reach the internet; treating the transport as a capability is a consumer's
  conclusion to draw and to defend.
- **Degrade silently when the cache is absent.** Rejected: a machine whose cache
  path is not the one probed would report every transport as absent, which reads
  exactly like a fleet running no remote MCP servers. That is the confusion this
  package exists to prevent.
- **Ask the server directly** by connecting and calling `tools/list`. Rejected:
  it executes the component under assessment, and the schemas it returns are
  server-authored anyway, so it would answer a different question than the one
  the transport answers.

## Consequences

The transcript's largest remaining `unknown` population for a whole call class is
closed, a call class that previously observed nothing becomes observable, and
each server gains a client-recorded identity that no manifest supplies.

It costs a dependency on an undocumented cache path whose directory names mangle
the project directory, and which the agent prunes on its own schedule. The path
is probed per platform and absence is normal, but a Claude Code release that
moves or restructures it will silently reduce this to today's behaviour — which
is why the state is reported rather than inferred from empty results. Watch
`mcp_log_state` counts: a sudden shift to `no_log_root` is the signal.

The log format itself is unversioned prose (`Successfully connected (transport:
stdio) in 451ms`). A reworded message stops matching, and the failure mode is a
quiet loss of enrichment rather than an error.

## When to revisit

If Claude Code publishes a supported interface for MCP connection state, prefer
it and delete the log parsing. If the log gains a per-call identifier, the
ordinal join and its guard both become unnecessary — replace them rather than
keeping both. If another agent kind writes comparable logs, the shape here
(connection-scoped record, call-scoped transport, an explicit state) should
generalise, but the parsing must stay per-kind: a shared pattern across
differently-worded logs is the silent wrong answer this package refuses
everywhere else.
