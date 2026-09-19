# ADR Index

Architecture Decision Records for OpenAIDR. The index is loaded into every
session by a hook; full ADRs are read on-demand when an entry looks
related to the work at hand.

**When to read full ADRs:** before changing logic in the area an ADR
covers. Each entry's one-liner is a hook — if it sounds at all
adjacent to your task, click through.

**When to write a new ADR:** when you make a decision where the
rejected alternative is *plausible* and *likely to be re-suggested*,
and the reason isn't obvious from the code. See `TEMPLATE.md`.

**Supersession discipline:** ADRs are immutable once accepted. If a
later decision contradicts an existing ADR, write a NEW ADR with
`supersedes: NNNN` in its frontmatter, and update the old ADR's
frontmatter to `status: superseded` + `superseded-by: NNNN`. Never
edit an accepted ADR's body in place — old PRs need to be readable
against the rules in effect at the time.

## Active

- [0001](0001-session-turn-and-subagent-identity.md) — **Session, turn and span identity for file-backed transcripts.** A turn is one non-relay message record; a subagent file is its own path-namespaced session; a repeated provider call id demotes only the later occurrence and withholds outcomes for both. Read before changing how any identity is derived, or before assuming a provider call id is unique.
- [0002](0002-structural-duplicate-outcome-withholding.md) — **Withhold outcomes for structurally duplicate calls, not just duplicate ids.** The Claude Code parser can attach one call's result to another structurally-identical call regardless of provider id; this adapter cannot tell that mismatch apart from two calls that genuinely returned the same thing, so both are withheld alike. Read before changing `_compromised_results` or its test coverage.
- [0003](0003-mcp-connection-log-enrichment.md) — **Read Claude Code's MCP connection logs; withhold outcomes the ordinal join cannot place.** The transcript states no outcome for 129 of 130 MCP calls and never records the transport; the per-server cache logs carry both, joined by position within a tool's sequence and guarded by a count agreement. Read before changing `claude_code_mcp`, the guard, or anything that treats an absent transport as local.
- [0004](0004-recovery-key-scoping.md) — **Scope every recovery key to the identity that makes it unique.** A uuid is unique only within its session, a provider call id only within the session that issued it, and an MCP log's session id only within its project directory; occurrence is counted only over the records upstream projects into `chat_history`. Read before adding a lookup keyed on any single identifier, or before assuming one file holds one session.
- [0006](0006-incomplete-log-scoped-by-project.md) — **State the ordinal join's preconditions, with an incomplete read scoped by project.** A count agreement does not establish that the log's completion order is the transcript's invocation order; overlap, a lost `Calling` line and a partial read each withhold at the narrowest scope that covers them — and a partial read's scope is `(project, server)`, not server alone, since a server name is no more unique across projects than a session id is. Read before changing the MCP guard, treating a count match as sufficient, or adding an incompleteness marker keyed on less than the full identity.
- [0007](0007-evidence-precedence-across-snapshots.md) — **Resolve every fact from the freshest read that can identify the call.** One `collect()` pass holds three reads of the same growing state taken at different instants — the MCP log, upstream's parse, this reader's own recovery pass — and identity comes first, freshness second: a read that cannot identify the call contributes nothing, and among those that can, the newest wins per fact. Read before merging two sources' account of the same call, or before deriving a status from one read and patching it from another.
- [0008](0008-claimants-are-not-window-scoped.md) — **The window scopes which sessions are returned, never which files claim an id.** A transcript the `--since` window excluded is still a claimant of its raw session id, read from its filename rather than by opening it, so a copied twin outside the window cannot leave its survivor looking like the id's sole holder. Read before changing `_ambiguous_transcript_ids`, or before assuming the window bounds anything other than parse work.
- [0009](0009-discovery-completeness-precedes-uniqueness.md) — **A uniqueness fast path requires a complete discovery pass, and the unit of unread evidence sets the scope.** An unscanned directory yields no file names, so it withholds the whole enrichment for every session on that side, while an unreadable file — whose project and server are known — stays scoped to that pair; and a session whose log resolved to nothing matches incompleteness on the server name alone, since it has no cache project to scope against and cannot borrow the transcript's. Read before adding any check that trusts a single match, or before reporting an I/O failure without saying what it makes unknowable.
- [0010](0010-a-turn-carries-the-time-of-its-own-record.md) — **A turn carries the time of its own record; a call carries none.** Each record's timestamp is joined to the turn it projects to on `(session, uuid, occurrence)` — the unambiguous key — while a call carries no absolute time of its own: its start is its turn's, and its end follows only where the duration is the transcript's own round trip, not one the MCP connection log filled in (ADR-0003) — never through the bare-call-id join this package refuses. A session carries the newest record observed, named as last activity and never as an end. Read before adding a timestamp to any level of the model, before reaching for the call-id join to date something, or before interpolating a time for a record that carried none.
- [0011](0011-keep-append-cursors-in-memory.md) — **Keep append cursors in memory for growing transcripts.** A stateful incremental collector projects only appended, newline-terminated records and returns the current full session; truncation or inode replacement resets cold, while restart persistence and a delta model remain out of scope. Read before changing steady-state collection, cursor lifetime, partial-record handling, or the full-session return contract.

## Superseded

- [0005](0005-mcp-ordinal-join-preconditions.md) — superseded by [0006](0006-incomplete-log-scoped-by-project.md): its third precondition scoped an incomplete read by server name alone, which let one project's unreadable log withhold a same-named server's outcomes in an unrelated project.
