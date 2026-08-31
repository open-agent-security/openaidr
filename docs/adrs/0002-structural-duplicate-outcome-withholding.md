---
id: 0002
title: Withhold outcomes for structurally duplicate tool calls, not just duplicate call ids
status: accepted
date: 2026-08-29
supersedes: null
superseded-by: null
---

## Context

ADR-0001 decided that a duplicated provider call id gets its outcome withheld
on every occurrence, rather than risk attaching a result to the wrong call.
Tracing the pinned parser's own matching code
(`adr_sensor/parsers/claude_parser.py:236-264`) showed the risk is wider than a
duplicated id: the parser resolves a result by locating a *structurally equal*
pending `ToolUsage` anywhere in the transcript — same tool name, type and
arguments — and updates every match it finds, not only the call whose id
produced the result. Two calls that never shared a provider id, one issued
while the other is still outstanding, can end up holding one call's result
while the other's real result is silently dropped.

The natural mitigation is to flag a group of otherwise-identical calls that
end up holding the same result value and withhold their outcomes, on the
theory that a shared value is the visible trace of the parser's mismatch.
Tracing further showed that theory is not sound: the same projected shape —
two structurally identical calls holding one identical result string — is
also what two *genuinely independent, correctly attributed* calls produce
when they happen to return the same content (rereading an unchanged file is
the ordinary case). The projected model this package receives carries neither
the provider call id nor any timing signal for when each result actually
arrived, so nothing here can tell those two situations apart. Recovering that
signal would mean re-parsing the raw transcript ourselves instead of trusting
the shared event schema — the second parse the module docstring already
rejects as out of scope for this adapter.

## Decision

Extend ADR-0001's preference — absence over a wrongly attached result — from
"duplicated call id" to "any group of calls that share tool name, type,
server and arguments and end up holding the same non-`None` result." Every
call in such a group reports `pending` with no result, regardless of whether
the shared value came from the parser's mismatch or from two calls that
legitimately returned the same thing. The two cases are indistinguishable
from the data this adapter has, so both are treated the same way.

## Alternatives considered

- **Only withhold when the calls share a provider id** (ADR-0001's original,
  narrower scope) — rejected because the reproduction above shows the parser's
  mismatch is keyed on structural equality, not on the id; two calls with
  distinct ids are exposed to the same silent substitution, and limiting the
  mitigation to the id case leaves that path uncovered.
- **Re-parse the raw JSONL to recover provider ids and result-arrival order,
  then disambiguate genuine duplicates from mismatched ones** — rejected for
  the same reason the module already gives for not doing a second parse: it
  would duplicate the pinned dependency's own parsing logic against the raw
  format instead of trusting the schema boundary, moving format knowledge back
  into this package that the dependency exists to own.
- **Leave the shared result in place and report both calls `ok`/`unknown` as
  the dependency projects them** — rejected because it accepts the parser's
  mismatch silently: a call whose real result was dropped would report
  someone else's data as its own with no signal that anything was withheld.

## Consequences

A session with genuinely repeated identical tool outcomes — the same file
read twice with no change in between is the common case — reports `pending`
for both calls instead of the correct, already-available result. This is a
real cost, not a corner case: it fires on ordinary idempotent reuse, not only
on the parser defect it was written to catch. It is accepted because the
alternative is an adapter that sometimes reports a fabricated result as fact
with no way for a consumer to tell which calls are trustworthy, and this
package's stated preference is absence over a wrongly attached outcome.

**Watch for:** if measurement on real sessions shows this firing often enough
that the withheld-but-legitimate case dominates the actually-mismatched case,
the trade-off should be revisited rather than left to compound quietly.

## When to revisit

Revisit if the pinned parser starts carrying the provider call id (or a
result-arrival marker) through its projected schema — either would let this
adapter tell a genuine duplicate from a mismatched one without a second parse,
and the withholding could narrow back to the cases that are actually
ambiguous.
