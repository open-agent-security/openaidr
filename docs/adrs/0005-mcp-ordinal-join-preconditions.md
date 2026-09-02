---
id: 0005
title: State the ordinal join's preconditions and withhold when they are unproven
status: accepted
date: 2026-09-02
supersedes: null
superseded-by: null
---

## Context

ADR-0003 established the ordinal join: the MCP log carries no span and no call
id, so an outcome is attributed by position within its `(server, tool)`
sequence, guarded by a per-key count agreement between log and transcript.

That guard answers one question — did both sides see the same number of calls
— and review found two ways for it to answer `yes` about a sequence that is
nonetheless unorderable. Both turn on the `Calling MCP tool` line, which is the
only evidence in the log that separates one call from the next:

- Two calls to one key in flight at once complete in either order, and the log
  says nothing about which is which. The counts still agree.
- A line that fails to decode is skipped. If it was a `Calling` line, the only
  evidence of that overlap is gone, and the surviving completions still make
  the counts agree. A torn *final* write is ordinary — the log is appended to
  live — but an earlier one is a record that existed and could not be read.

Measured across 1,655 real log files: 863 completions, every one of them
preceded by an outstanding `Calling` line, one key with real overlap, and no
malformed lines at all. The preconditions hold in practice; nothing enforced
them.

## Decision

The ordinal join has three stated preconditions, and each one that cannot be
shown to hold withholds at the narrowest scope that covers it.

1. **No two calls to a key in flight at once.** A second `Calling` for a key
   already outstanding marks that key `unordered` for the whole session, and
   its outcomes are left out of the join.
2. **No `Calling` line lost.** An outcome arriving with nothing outstanding
   for its key marks that key `unordered` too: either a line went missing or
   two calls overlapped and the announcement of the second was what was lost,
   and both make completion order unusable as invocation order. A log that
   records no `Calling` line for a key at all — an older client that does not
   write them — is not held to a line it was never going to produce.
3. **The read is whole.** A file that could not be opened or decoded, and a
   file with a malformed line anywhere but its end, both mark their *server*
   incomplete, and every session that calls that server is treated as a count
   mismatch rather than trusted on a guard run against a partial read.

Withholding at each scope is reported rather than silent: `unordered` keys
raise `mcp_overlap_withheld` on a session that is otherwise `applied`, and an
incomplete server produces `count_mismatch`. Connection-scoped facts —
transport, endpoint, advertised identity — survive all three, because none of
them depends on ordering calls.

## Alternatives considered

- **Require a `Calling` line before every outcome, unconditionally.**
  Rejected: it makes the join depend on a client behaviour we have measured
  but not been promised. A client version that stops writing the line, or
  writes it only for some transports, would silently withhold every outcome on
  the machine. Keying the requirement to whether *this log* records the line
  for *this key* asks only for internal consistency.
- **Mark only the affected key incomplete when a line fails to decode.**
  Rejected: what the skipped line said is unknowable, including which key it
  belonged to. Server scope is the narrowest honest one.
- **Treat any malformed line as an ordinary torn append.** This is what the
  code did. Rejected: it is only true of the last line. The log is appended to
  live, so the end of the file can be half-written; the middle cannot be,
  and a hole there is loss rather than concurrency.
- **Drop the whole file on a malformed line.** Rejected for the reason
  ADR-0003 gives throughout: a partial read reported as partial is worth more
  than no read at all, and the connection facts in the readable remainder are
  unaffected by the ordinal question.

## Consequences

The join now declines in three more situations than it did, all of them
reported. On measured data none of them fire, so the coverage cost today is
zero; the cost is paid only where the log is genuinely unorderable, which is
the intended trade.

`mcp_overlap_withheld` is now raised by two causes, not one, and its name says
only the first. Renaming it is a breaking change for consumers, so the
docstring and the rendered line carry the fuller meaning instead — worth
watching if a third cause is ever added.

## When to revisit

If Claude Code ever writes a call id into the `Calling`/`completed` pair, the
ordinal join and all three preconditions become unnecessary: the join becomes
an id lookup and this ADR, along with the count guard in ADR-0003, should be
superseded rather than extended.
