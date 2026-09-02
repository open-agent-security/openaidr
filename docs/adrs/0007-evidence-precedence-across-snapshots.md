---
id: 0007
title: Resolve every fact from the freshest read that can identify the call
status: accepted
date: 2026-09-02
supersedes: null
superseded-by: null
---

## Context

The Claude Code reader builds one call from three independent reads of the same
underlying state, taken at three different instants within a single `collect()`
pass:

1. **The MCP connection log**, read once at the top of `collect()` — the oldest.
2. **Upstream's `parse_jsonl_file(path)`**, in `_read_file`.
3. **This reader's own `_recorded(path)` recovery pass** over the raw JSONL — the
   newest.

Every one of those files is append-only and every session may still be being
written, so the three reads can legitimately disagree: a call the log caught
`still running`, that upstream then saw without a result, can have its result on
disk by the time the recovery pass reads the same file. None of the three is
wrong; they are snapshots of a growing file at different times.

Four separate rounds of external review on PR #3 reported the same class of
defect at four different merge sites, and each site had invented its own
freshness rule independently — with the rules contradicting one another:

- The MCP outcome was allowed to rewrite a transcript-proven `unknown` to
  `pending`, letting the *oldest* read overwrite a newer one.
- The MCP outcome's `duration_ms` — which for an unresolved call is an elapsed
  wait, not a round trip — was copied onto a call that stayed `pending`.
- Conversely, a result present only in the recovery pass was deliberately
  *discarded* because upstream's older snapshot had none, letting the older read
  overwrite the newer one and reporting a completed call as unresolved until
  some later collection pass happened to catch it.

That is the same shape ADR-0004 found for keys, one layer along: a rule that was
never named centrally gets re-derived at each site, and half the derivations come
out backwards.

## Decision

Two rules, applied at every site where two reads describe the same call.

**Identity first.** A read contributes nothing unless it can identify *this*
call. A structurally-duplicated or reused-id call (ADR-0002), an ordinally
unplaceable MCP outcome (ADR-0006), a subagent's parent-keyed log (ADR-0001) are
all withheld regardless of freshness — a newer read of the wrong call is not
better evidence, it is a confident wrong answer.

**Then freshness.** Among reads that *can* identify the call, the newest wins,
per fact. Concretely: the recovery pass beats upstream's snapshot, and both beat
the MCP log. A status is therefore derived from whether *any* read shows the call
returned, not from whether the oldest one did, and `pending` means no read saw a
result. A fact one read merely lacks is a gap the next may fill; a fact a read
positively states is only ever superseded by a *newer* read stating otherwise,
never by an older one.

The corollary that makes this checkable: no call may report a fact its own status
contradicts. A `pending` call carries no result and no duration — including the
MCP log's elapsed wait, which measures a call the log never saw finish.

## Alternatives considered

- **Require agreement between reads before asserting anything** — rejected: the
  reads are taken at different instants by construction, so disagreement is the
  normal case for any active session, not a corruption signal. This would report
  every in-progress session's finished calls as unresolved.
- **Always prefer upstream's snapshot as the single source of truth** — rejected:
  it is the read that carries the least (no provider call id, no `is_error`, no
  timings, results abridged from the middle), which is why the recovery pass
  exists at all. Deferring to it discards the evidence this reader was written
  to recover.
- **Freshness before identity — take the newest read unconditionally** —
  rejected: it inverts the priority that ADR-0001, ADR-0002 and ADR-0006 exist
  to protect. Newer evidence about a call that cannot be shown to be this one is
  exactly the silent misattribution those decisions withhold on.
- **Snapshot all three reads atomically** — rejected: not available. The files
  are written by another process, and the parse of one is a dependency call this
  package does not control the timing of.

## Consequences

A call that finished between two reads within one pass is now reported as
finished, rather than reported `pending` and corrected on a later pass — which
matters most for the steady-state design in `docs/specs/session-collection.md`,
where a consumer draws a span when parsed and expects its outcome to arrive.

The cost is that a call's reported facts can come from more than one read of the
file, so a `result` and a `status` are not guaranteed to be from the same
instant. That is acceptable because both are monotonic: an append-only transcript
never un-finishes a call, so the newer read can only ever be at least as complete.

What to watch for: any *new* join added to this reader is a fourth merge site,
and the rule has to be applied there deliberately. The failure is silent by
construction — nothing in the output says a fact came from the wrong read.

## When to revisit

If a transcript is ever observed to be rewritten rather than appended to — a
compaction that rewrites history in place, say — the monotonicity argument fails
and the newest read stops being the most complete one. Also if the MCP log ever
gains a call id, since the ordinal join and its withholding would then be
replaced by an identity join, and the log would become a read that *can* identify
a call rather than one that can only be placed by position.
