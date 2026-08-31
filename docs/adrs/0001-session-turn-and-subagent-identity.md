---
id: 0001
title: Fix session, turn and span identity for file-backed agent transcripts
status: accepted
date: 2026-08-29
supersedes: null
superseded-by: null
---

## Context

The session model publishes three identities a consumer addresses rows by: which
session, which turn, and which tool call. Span identity is the load-bearing one —
a consumer draws a call when parsed and attaches outcome, component and findings
later by naming the same span, so identity that shifts as a session grows lands
every enrichment on the wrong row, silently and with no error to notice.

Deriving those identities from a real agent's on-disk state forced six choices
that a later contributor could reasonably re-suggest, and the reasoning behind
each is not visible in the code that results. Three were forced by the structure
of Claude Code's own transcripts, measured on a working machine rather than
assumed:

- **Subagent transcripts reuse the parent's session id.** 157 subagent files
  under `~/.claude/projects/<project>/<sessionId>/subagents/agent-<id>.jsonl`
  were inspected; in every one sampled, each record's `sessionId` field equalled
  the *parent* session's id. Only the file's own path distinguishes them. Keying
  a session on the `sessionId` field alone therefore mints duplicate session
  identities, and duplicate span namespaces beneath them.
- **The parsing dependency does not emit the provider's call id at all**, so a
  span cannot be keyed on it. What survives is `sequence_id`, the record's own
  key — which real transcripts also repeat, so it is not unique either.
- **Records carry no ordering guarantee beyond append.** Nothing in the format
  states that existing records are never rewritten, reordered or removed, and
  the reader cannot detect it if they are.

## Decision

1. **A turn is one `user` or `assistant` message record**, in file order — except
   a record whose content is entirely `tool_result` blocks, which is a wire-format
   relay carrying an earlier call's outcome rather than a new exchange, and is not
   counted as a turn.

2. **A subagent's transcript file is its own `Session`**, identified by its own
   path — `<agent kind>:<parent sessionId>:<file stem>` — rather than by the
   collided `sessionId` field, with every turn in it marked sidechain regardless
   of the record's own marker.

3. **A tool call's identity is `session:turn_key:call_index`**, where the index
   is within its own turn, never a position in the whole session.

4. **A repeated turn key is disambiguated by occurrence, and only the later
   one.** The first keeps the plain key, so a span already emitted for it never
   changes when a duplicate appears later in the file. A later duplicate must not
   reach backwards.

5. **Positional and index-based fallbacks are valid identity only under an
   append-only source**: existing records and content blocks are never rewritten,
   reordered or removed, only followed by new ones.

6. **A session identity already held by a session from another source is a
   collision to report**, never a duplicate to silently replace.

## Alternatives considered

- **A turn is a merged multi-role exchange** (a user message plus the assistant's
  reply as one unit) — rejected because the merge point is a judgement about
  conversation structure that the file does not state, and it would make turn
  count depend on our inference rather than on what the agent recorded.
- **Fold a subagent's turns into its parent's turn list** — rejected because it
  makes a subagent's activity indistinguishable from the parent's own at the row
  level, and it discards the one thing the file structure does tell us plainly:
  which work was delegated.
- **A raw positional span** (`session:index-in-session`) — rejected because
  appending is not the only way a session changes shape while being read; a
  skipped malformed record shifts every subsequent index, and the shift is
  invisible.
- **Demote every occurrence of a repeated key, including the first** — rejected
  because revising a span already emitted for the first occurrence is exactly the
  instability stable span identity exists to prevent.
- **Assume `sequence_id` is unique, since repeats are rare** — rejected on
  measurement: repeats are rare enough never to appear in testing and common
  enough to occur in production, and the failure is silent collision rather than
  an error.
- **Last-writer-wins on a session identity collision** — rejected because nothing
  here can verify that Claude Code's session ids stay globally unique across
  copies, restores, or independently rooted project directories, and silently
  replacing one session with another is data loss presented as success.

## Consequences

Consumers get identities that survive re-reads of a growing session, which is
what makes incremental enrichment correct rather than merely convenient. The
subagent decision also makes delegated work countable — a property the parent's
own turn list cannot express.

The costs are real. Session counts are higher than a user might expect, because
each subagent transcript is its own session rather than part of the conversation
that spawned it; whether a consumer wants them joined is a consumer's question,
and this decision deliberately leaves the join to them rather than pre-empting
it. Withholding outcomes for every occurrence of a duplicated call id means a
small number of calls report `pending` when a result does exist in the file —
absence, chosen over the risk of attaching the wrong result. And identity
derived from a file's path means moving or renaming a transcript changes the
identity of everything in it.

**Watch for:** a source that compacts or rewrites its transcripts, which would
invalidate decisions 3 and 5 without any signal that it had.

## When to revisit

Revisit the fallback identities if a supported source is ever found to rewrite,
reorder or remove existing records rather than only append to them — the
append-only assumption is what makes a positional fallback safe, and it is an
assumption rather than a guarantee. Revisit decision 2 if an agent kind starts
minting a distinct session id for a subagent, which would make the path
namespace redundant. Revisit decision 4 if a provider documents its call ids as
unique, or if measured duplicate rates rise enough that withholding outcomes
becomes the larger loss.
