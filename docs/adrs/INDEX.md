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
- [0005](0005-mcp-ordinal-join-preconditions.md) — **State the ordinal join's preconditions and withhold when they are unproven.** A count agreement does not establish that the log's completion order is the transcript's invocation order; overlap, a lost `Calling` line and a partial read each withhold at the narrowest scope that covers them. Read before changing the MCP guard or treating a count match as sufficient.

## Superseded

(none yet)
