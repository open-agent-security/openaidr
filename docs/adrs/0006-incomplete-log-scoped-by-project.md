---
id: 0006
title: State the ordinal join's preconditions, with an incomplete read scoped by project
status: accepted
date: 2026-09-02
supersedes: 0005
superseded-by: null
---

## Context

ADR-0003 established the ordinal join: the MCP log carries no span and no call
id, so an outcome is attributed by position within its `(server, tool)`
sequence, guarded by a per-key count agreement between log and transcript.

That guard answers one question — did both sides see the same number of calls
— and review found two ways for it to answer `yes` about a sequence that is
nonetheless unorderable, plus a third way the read backing the guard itself
can be partial. All three turn on evidence a read can lose without the counts
saying so:

- Two calls to one key in flight at once complete in either order, and the
  log says nothing about which is which. The counts still agree.
- A line that fails to decode is skipped. If it was a `Calling` line, the only
  evidence of that overlap is gone, and the surviving completions still make
  the counts agree. A torn *final* write is ordinary — the log is appended to
  live — but an earlier one is a record that existed and could not be read.
- A server's log can be split across several files, one per run. If one file
  is unreadable while the others parse cleanly, the count built from the
  readable remainder cannot be told from a count that really is complete.

Measured across 1,655 real log files: 863 completions, every one of them
preceded by an outstanding `Calling` line, one key with real overlap, and no
malformed lines at all. The preconditions hold in practice; nothing enforced
them until ADR-0005 first stated and checked all three (superseded here).

**What ADR-0005 got right and what it missed.** All three preconditions and
the "withhold at the narrowest scope that covers it" principle stand
unchanged. What it missed is in the third: the file that fails to read sits
under a directory nested inside a *project* directory
(`<cache>/<project>/mcp-logs-<server>/*.jsonl`), so an unreadable file is
only ever this *project's* share of this server's log — the same fact
ADR-0004 already relies on to key `by_session` as `(project, session id)`,
because a server name is no more unique across projects than a session id is.
ADR-0005 marked the bare server name incomplete, globally. A session in
`project-a` that calls a server named `books` was withheld the moment *any*
project on the machine had an unreadable `books` log, including one it never
shared a file with — the same class of defect ADR-0004 describes, one field
later.

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
   file with a malformed line anywhere but its end, both mark their
   `(project, server)` pair incomplete — not the server name alone — and
   every session whose own resolved cache project calls that server is
   treated as a count mismatch rather than trusted on a guard run against a
   partial read. `MCPLogIndex.incomplete_servers: frozenset[str]` becomes
   `incomplete_project_servers: frozenset[tuple[str, str]]`, keyed the same
   way `by_session` already is.

   The check in `claude_code.py` uses the *resolved* cache-side project for
   the session being enriched, not the transcript's own project string:
   `MCPLogIndex.for_session` now returns that resolved project alongside the
   logs and the ambiguity flag, because the two are not always the same
   string (ADR-0004's tie-break argument again — a session id found under
   exactly one cache project is that session's log whatever the transcript's
   own directory is called). Checking against the transcript's own project
   name would reintroduce the same class of bug in the other direction: a
   session whose cache log lives under a differently-mangled project name
   would never match its own incompleteness marker.

Withholding at each scope is reported rather than silent: `unordered` keys
raise `mcp_overlap_withheld` on a session that is otherwise `applied`, and an
incomplete `(project, server)` pair produces `count_mismatch`. Connection-scoped
facts — transport, endpoint, advertised identity — survive all three, because
none of them depends on ordering calls.

## Alternatives considered

- **Require a `Calling` line before every outcome, unconditionally.**
  Rejected: it makes the join depend on a client behaviour we have measured
  but not been promised. A client version that stops writing the line, or
  writes it only for some transports, would silently withhold every outcome on
  the machine. Keying the requirement to whether *this log* records the line
  for *this key* asks only for internal consistency.
- **Mark only the affected key incomplete when a line fails to decode.**
  Rejected: what the skipped line said is unknowable, including which key it
  belonged to. `(project, server)` is the narrowest honest scope.
- **Treat any malformed line as an ordinary torn append.** This is what the
  code did before ADR-0005. Rejected: it is only true of the last line. The
  log is appended to live, so the end of the file can be half-written; the
  middle cannot be, and a hole there is loss rather than concurrency.
- **Drop the whole file on a malformed line.** Rejected for the reason
  ADR-0003 gives throughout: a partial read reported as partial is worth more
  than no read at all, and the connection facts in the readable remainder are
  unaffected by the ordinal question.
- **Leave incompleteness scoped by server name alone, as ADR-0005 had it.**
  Rejected: "narrowest honest scope" was already the stated principle: bare
  server scope answered a question ADR-0005 did not ask — whether to narrow
  *further* than server, to the individual `(server, tool)` key — and
  correctly said no, because the skipped content's key is unknowable. It was
  not a considered choice to leave the scope wider than the project boundary
  ADR-0004 already draws everywhere else this reader joins against the cache.
- **Check incompleteness against the transcript's own project name instead of
  the resolved one.** Rejected: the two manglings disagree on 99 of 139 real
  cache directories, so this would either miss real incompleteness
  (undercounting) or require exact-name agreement everywhere, which ADR-0004
  already rejected as losing more coverage than the collision it guards
  against.

## Consequences

The join declines in three situations, all of them reported. On measured data
none of them fire, so the coverage cost today is zero; the cost is paid only
where the log is genuinely unorderable or genuinely incomplete, which is the
intended trade.

A same-named server in an unrelated project no longer withholds outcomes it
never shared a file with — a coverage gain over ADR-0005's behaviour, not a
loosening: the cross-project bleed that behaviour permitted is now
structurally impossible. The cost is one more return value threaded through
`for_session`; every existing call site had to be updated to receive it.

`mcp_overlap_withheld` is still raised by two causes (preconditions 1 and 2),
and its name says only the first; the docstring and the rendered line carry
the fuller meaning. `count_mismatch` is now raised by two causes too (a count
disagreement, or an incomplete `(project, server)` read) and already carried
one name for both.

## When to revisit

If Claude Code ever writes a call id into the `Calling`/`completed` pair, the
ordinal join and preconditions 1 and 2 become unnecessary: the join becomes an
id lookup, and this ADR should be superseded rather than extended.

If Claude Code ever writes a stable, unmangled project identifier into both
the transcript and the cache, `for_session`'s tie-break and this ADR's
resolved-project indirection for precondition 3 both become unnecessary —
project identity would already be exact everywhere it is used, in
`by_session` and in `incomplete_project_servers` alike.
