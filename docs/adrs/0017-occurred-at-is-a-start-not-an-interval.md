---
id: 0017
title: occurred_at is a start, never an interval
status: proposed
date: 2026-10-08
supersedes: null
superseded-by: null
---

## Context

ADR-unlinked until now, `ContextItem.occurred_at` (#45) carries the time an
attachment record was written, fixing a real gap: a consumer counting the
calls an MCP server's instructions reached previously had to count every call
in the session, overstating a server connected mid-session by every call made
before it.

A completed review flagged that the field's first docstring overstated the
fix: "the item is in context from then on" promises an open-ended active
window, but the transcript can remove a server's instructions mid-session
without this package ever seeing it. A real transcript (reported against
Claude Code upstream, issue 96154) writes the removal as its own
`mcp_instructions_delta` record carrying `removedNames` and no `addedNames` /
`addedBlocks`. `_recover_session_scoped`'s attachment handling reads an
item's body from `addedBlocks` alone; a record with none produces no body, and
`if body is None: return` drops the record before it reaches `context` at all
— not as an item with no text, as nothing. The reader has no way to represent
"this server's instructions stopped reaching the model here," so a consumer
trusting an unbounded window past `occurred_at` can count calls made after a
real removal, and double-count across a remove-then-reconnect pair, since the
reconnect's own record becomes a second item with its own `occurred_at` and
nothing marks the first one's end.

## Decision

`occurred_at` states only a lower bound: *reaching the model starts here*. It
promises nothing about when, or whether, that stops. The docstring says so
directly and cites this ADR. No code changes: the dropped removal-only record
was already dropped before #45, and nothing in #45 reads or writes a
removal boundary.

## Alternatives considered

- **Preserve the removal-only record as a marker (no body, no parts, just a
  time) and let a consumer close the interval from it.** Rejected for this
  PR: it is a second new field (an item with nothing to read, existing only
  to be subtracted from another item's position) and a new rule for every
  reader kind's recovery pass to agree on, where #45 is additive and
  single-field. A real feature, but a bigger one than restating what one
  field promises.
- **Compute and publish an `ended_at` on `ContextItem` now.** Rejected: it
  needs the marker above to exist first, plus a rule for pairing a removal to
  the item it ends (by name? by source? a listing names several at once,
  ADR-0016) and a rule for an item still active when the session's last
  record was read — which ADR-0010 already refused to call an end for the
  same reason: a session read while still being written has no end, only a
  last-seen instant.
- **Leave the original docstring and trust consumers to read the code.**
  Rejected: a docstring that promises more than the model delivers is worse
  than a narrower one, for the same reason absence is modelled as absence
  everywhere else in this package.

## Consequences

A consumer reading `occurred_at` gets an honest lower bound and nothing it
has to unlearn later. Counting calls "carried by this item" from
`occurred_at` to the session's end still overstates a server removed and not
reconnected — that overstatement is smaller than counting from session start
(the bug #45 fixes) but not zero, and is now a stated limitation rather than
a silent one.

## When to revisit

If a consumer need for an end boundary becomes concrete enough to justify the
marker-then-pairing work above — at which point the second alternative is the
starting design, not this one re-litigated.
