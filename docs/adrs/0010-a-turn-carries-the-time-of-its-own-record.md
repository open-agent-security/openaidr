---
id: 0010
title: A turn carries the time of its own record; a call carries none
status: accepted
date: 2026-09-15
supersedes: null
superseded-by: null
---

## Context

Every Claude Code transcript record carries a `timestamp`. This package already
read them: `_recorded` filed the `tool_use` record's time under `started` and the
`tool_result` record's time under `ended`, `_duration` subtracted the pair, and
both absolute instants were discarded. `Session.started_at` — the *earliest*
record, taken from the dependency — was the only absolute time to survive into
the model.

Consumers therefore had no way to say when anything happened. The finest clock
reachable through the model was a session's start, which for a long session is
wrong by hours and for a resumed one by weeks. Consumers compensated with the
transcript file's modification time, which is a fact about the file rather than
about the session: one file can hold more than one session after a resume, so
the value moves when a *different* session is appended to.

`docs/specs/session-collection.md` recorded per-turn time as "Not modelled — a
field that is always empty invites consumers to build on a value that never
arrives." That was measured against what `adr-sensor` could carry, and the same
document already records per-call *timings* as closed, recovered in this
package's own pass. The premise had changed and the row had not.

Two facts decided the shape of the fix, and both were verified in source rather
than assumed:

1. **The record's timestamp is reachable on the unambiguous key.** `started` and
   `ended` are keyed on the bare provider call id, which is why `_tool_calls`
   withholds a duration for a withheld or `pending` call (ADR-0002, ADR-0004).
   But the record's own timestamp sits on `(session, uuid, occurrence)` — the key
   `cwd_at` and `permission_modes` already use, and the key `_turns` already
   holds.
2. **A per-call start would restate the turn's.** `started[(sid, id)]` is written
   from the *record's* timestamp — the same record that projects to the turn. A
   call's start and its turn's occurrence time are the same instant by
   construction, and a call's end follows from that instant and the duration
   already published — when that duration is the transcript's own round trip.
   A duration the MCP connection log fills in (ADR-0003) times only the tool's
   own execution, not the issue-to-result span, so it does not recover an end
   the same way.

The join has one precondition: the record must carry both the identity the key
is built from and a timestamp. Where either is missing — a record with no
identity, a transcript whose second read fails, a kind whose records are shaped
differently — the turn is undated. That is why the field is nullable rather than
required, and it is the same precondition `permission_mode` already depends on.

## Decision

A **turn** carries the timestamp of the record that produced it, joined on
`(session, uuid, occurrence)`. A **session** carries, beside its start, the
newest record observed for it — named for the last activity seen, never as an
end. A **tool call** carries no absolute time: its start is its turn's, and —
only where the duration is the transcript's own round trip, not one the MCP
connection log filled in (ADR-0003) — its end follows from its turn's time and
its duration.

The call-id join is not entered. `_duration`, `_tool_calls` and the withheld and
`pending` guards are untouched, and the durations this package publishes are
byte-identical before and after.

Every time reported is **timezone-aware and converted to UTC**, matching the
zone the dependency already returns the session's start in. An offset-bearing
value is converted rather than passed through: the conversion is lossless, and
one canonical zone means every time on the model carries the same `tzinfo`
whatever produced it.

**Timestamps are interpreted by one rule, and it is the dependency's**: an
offset-bearing value is converted to UTC, and a value carrying no offset is taken
as UTC. This package does not apply a stricter rule to the values it recovers
than the dependency applies to the session start it produces, because a model in
which two time fields resolve zones differently is a model a consumer has to
memorise.

Absence stays absent, and the line is between *interpreting* a value and
*inventing* one. A record with no timestamp, a transcript whose second read
fails, and a turn whose record carried no identity all yield no time — never the
collection time, never a neighbouring turn's, never the file's mtime.

## Alternatives considered

- **`started_at` and `ended_at` on the tool call.** Rejected twice over. The
  start would duplicate the turn's own value, written once per call instead of
  once per record. The end is only reachable through the bare-call-id join,
  which this package already refuses to trust for a withheld or reused id — so
  the field would be either wrong on exactly the calls the guard exists to
  protect, or null on them, in which case the turn's time answers the question
  anyway.
- **A turn end, computed from the newest tool result the turn's calls drew.**
  Plausible, and it was specified before being cut. It requires inverting the
  call-id map to attribute a result to the turn that issued it, which reopens
  the reused-id ambiguity; it needs a rule for a turn holding a still-running
  call, where any answer overstates; and a consumer wanting the span of an
  episode gets it from the first and last *turn* it cares about, which is a
  consumer's question about its own findings rather than a fact about a turn.
- **Modelling a session end.** Rejected. Sessions are read while still being
  written, so the newest record is not an end and calling it one would state a
  fact the file does not support. The value is carried under a name that says
  what it is.
- **Interpolating an undated turn from its neighbours.** Rejected: absence is
  not falsehood. A neighbour's time is a guess with a true value's type, and the
  consumer cannot tell the two apart.
- **Treating an offset-less timestamp as local time.** Rejected for the same
  reason, and it is a tempting mistake because it produces a value that looks
  correct. A wall-clock reading with no zone can land anywhere in a day-wide
  band; a consumer windowing on it would admit or hide an event by a margin
  larger than most windows. Absent is the honest answer, and it is one a
  consumer can see. (Taking it as UTC instead is the dependency's own rule,
  discussed below rather than rejected here.)
- **Leaving normalisation to consumers.** Rejected: it makes every consumer
  reimplement it, and a naive value reaching one of them mixes with aware values
  in comparisons and folds — where the failure is a type error at best, and a
  silently wrong ordering at worst.
- **Applying a stricter zone rule than the dependency's** — reporting a zoneless
  timestamp as absent rather than taking it as UTC. Argued for, and rejected on
  consistency. The argument for it is real: a zoneless wall-clock reading can be
  anywhere in a day-wide band, a value wrong by the writer's offset is
  indistinguishable from a correct one, and cross-machine comparison is exactly
  what a uniform shift breaks silently.

  It was rejected because the dependency's behaviour cannot be changed from here,
  so strictness here does not produce a correct model — it produces a *split*
  one, where the session's start resolves a zone by guessing and a turn's time
  refuses to. A consumer would have to know which field does which, and the
  asymmetry would show up as a session that has a start but no dated turns. One
  rule that is occasionally generous beats two rules that are individually
  defensible.

  The cost is accepted and named: a zoneless timestamp is taken as UTC and may
  therefore be wrong by the writer's offset. Nothing in the model distinguishes
  such a value from a correct one, and a consumer comparing times across machines
  inherits that.

## Consequences

Consumers can date what they find, and can order and window on it. The session's
last-activity value gives them a recency signal that is a property of the session
rather than of the file holding it, which is what a file mtime is not.

The granularity is the record, not the call: parallel calls issued in one
assistant record share an instant, and within a turn only span order separates
them. This is stated rather than corrected, because a finer value would be the
same number written more times.

The cost is two nullable fields on the public model and one more thing the
reader must keep filed correctly. The occurrence counting the join depends on is
already load-bearing for `permission_mode` and span identity, so this adds a
consumer of that scheme rather than a new scheme.

## When to revisit

If an agent kind arrives whose records carry no per-record identity, leaving its
turns undated — the field stays nullable for exactly this reason, and the honest
answer is a gap for that kind rather than an interpolation.

If an agent kind writes zoneless timestamps in volume. Taking them as UTC is
tolerable while the kinds actually read write offsets; it stops being tolerable
once a kind's times are routinely shifted by its writer's zone. The answer then
is to *recover* the zone from elsewhere in the transcript rather than to guess a
different one — and if the dependency's rule and a recovered zone ever disagree,
this decision's consistency argument is what has to be re-argued.

If an agent kind writes timestamps as epoch numbers rather than ISO strings. A
parsing gap rather than a decision: the dependency reads them and this package's
own recovery does not, which is itself an inconsistency of the kind this ADR
exists to avoid, and should be closed when a kind needs it.

If an agent kind records a genuine per-call start distinct from its issuing
record's time, at which point a call-level time would carry information the
turn's does not, and this decision's second argument stops applying.

If a per-turn *end* is ever needed for something a consumer cannot answer from
the turns it cites — the alternative above records what it would cost.
