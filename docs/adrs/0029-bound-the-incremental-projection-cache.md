---
id: 0029
title: Bound the incremental projection cache by recency and source bytes
status: accepted
date: 2026-10-15
supersedes: 0011
superseded-by: null
---

## Context

ADR-0011 retains one in-memory projection per transcript path for the life of
the `IncrementalCollector`, with no upper bound on how many paths accumulate.
That is correct for the single growing session a cursor was designed around,
but a long-lived reader sees many distinct transcripts over its lifetime, and
every one it has ever read stays cached — even after a consumer discards its
session view, or the source file is deleted. An 80-transcript synthetic
reproduction retained 20.15 MiB and never released it through garbage
collection alone, because the collector itself still held the reference.

ADR-0011 already accepts rebuilding a file from byte zero in three cases: an
inode change, an observed truncation, and process restart. A bounded cache
adds a fourth: eviction under memory pressure. This is a cache-replacement
policy layered on an existing primitive, not a reopening of ADR-0011's core
trade-off (incremental reads beat the dependency's whole-file reparse for the
hot path); it still needed its own ADR because a future reader of ADR-0011
alone, which still states per-file retention with no bound, could plausibly
"restore" unbounded retention as a correctness fix and silently reintroduce
the leak this decision closes.

## Decision

The Claude Code reader keeps its per-path projections in an `OrderedDict`
used as an LRU: every read moves its path to the most-recently-used end. After
each read, while the cache holds more than one entry and either more than 128
files or more than 16 MiB of combined source bytes (summed from each
projection's committed offset, including an unfinished trailing record), the
least-recently-used entry is evicted. The file just read is never evicted by
this budget — only other, less-recently-used files are — so a single active
transcript larger than the byte budget on its own still stays cached rather
than being reprojected from byte zero on every call, which would otherwise
defeat the incremental read ADR-0011 exists to provide for exactly the
longest-running sessions.

An evicted path is not special-cased: the next read for it goes through the
same `setdefault`-and-build path ADR-0011 already uses for inode changes and
restarts, so it rebuilds from byte zero with the dependency's normal parse,
recovering the same tool results, partial-record framing, and span identity
as a cold read. Nothing about identity or outcome correctness depends on
whether a projection happened to still be cached.

## Alternatives considered

- **Keep ADR-0011's unbounded retention.** Rejected: a long-lived reader's
  cache grows with every transcript it has ever touched, not with its active
  working set, so memory is unbounded in the number of sessions the process
  sees over its lifetime rather than in the number it is concurrently
  tracking.
- **Bound by file count only.** Rejected: a small number of oversized
  transcripts can still exceed any memory budget worth setting.
- **Bound by byte count only.** Rejected: very many small transcripts could
  stay under a byte budget while still pinning an unbounded number of
  projection objects and their internal state.
- **Evict unconditionally, including the file just read.** Tried first and
  rejected after a maintainer review reproduced the regression: any active
  session larger than 16 MiB passes the byte budget on its own, so an
  unconditional eviction loop empties the cache on every single call for
  exactly the long-running sessions this decision exists to keep fast,
  silently falling back to a full reparse per tick.
- **A size-weighted or frequency-based eviction policy.** Rejected for this
  stage as unneeded complexity; recency already matches how consumers read
  sessions in practice (active sessions far more often than idle ones), the
  same assumption ADR-0011 relies on implicitly for its single-cursor design.

## Consequences

Retention is now bounded in both files and source bytes, at the cost of a
cold rebuild for whichever file the LRU evicts next. That rebuild has the
same cost and same correctness guarantees ADR-0011 already pays after an
inode change, a truncation, or a restart — this decision only makes it
reachable under normal operation, not a new code path.

The byte budget measures source bytes read per projection, not Python heap
usage, and not the memory held by the `Session` model objects already
returned to callers. It does not reduce the cold baseline-backfill peak for a
transcript seen for the first time, and it does not bound memory held outside
the collector by consumers that keep their own copies of returned sessions.

## When to revisit

If a measured workload needs a materially different file or byte budget,
make the bounds configurable instead of adjusting the hardcoded constants. If
LRU eviction itself becomes a measured bottleneck — for example, a working
set that thrashes against either bound — consider a size- or frequency-aware
policy instead of enlarging the budget to paper over it.
