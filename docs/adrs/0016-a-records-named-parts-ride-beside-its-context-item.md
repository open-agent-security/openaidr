---
id: 0016
title: A record's named parts ride beside its context item, never as items of their own
status: proposed
date: 2026-10-08
supersedes: null
superseded-by: null
---

## Context

Two attachment types describe several things at once. A `skill_listing`
record lists every skill the agent can call, one `- name: description` entry
per skill (`- name` alone once the listing is over its budget), and names them
in order in `names`. An `mcp_instructions_delta` record carries several MCP
servers' instruction blocks, pairing `addedNames[i]` with `addedBlocks[i]`, as
the client itself reads the record back. Each becomes one `ContextItem` whose
text is the whole body and whose `name` is none, so a consumer asking how much
one skill's or one server's material weighs, or whether a given server's
instructions arrived at all, has to read the text for names. That is the
format-parsing this package exists to do once, per kind, so no consumer has to.

## Decision

`ContextItem` gains `parts: tuple[ContextPart, ...]`, empty by default. A
`ContextPart` is a `name` and the `text` of that part exactly as it sits in the
item's text. The Claude Code reader fills it for these two types, and only
where the record pairs every part with its name for certain: for MCP
instructions, names and blocks of equal length; for a skill listing, one entry
per name, each entry's first line holding its name. Anything else has no
parts. The item stays one item per record.

## Alternatives considered

- **One `ContextItem` per part.** Rejected: a context item's span is its
  position in the session's context list (`span_id(session, "context",
  index)`), so splitting a record into several items moves the span of every
  item after it. A consumer that cited a span before would land on a different
  item after the upgrade, silently.
- **Find the parts by reading the text for skill or server names.** Rejected:
  a name that also appears in another part's description pairs with the wrong
  text. The record states the names; the text only confirms them.
- **Pair by order alone, whatever the counts.** Rejected: a description holding
  its own `- ` line makes one entry too many, and every pair after it is
  wrong. A part named wrongly is worse than none (absence is not falsehood).

## Consequences

- Additive: an existing consumer reading `source`, `name` and `text` sees no
  change, and no span moves.
- A listing whose description holds a `- ` line, or a record whose names do not
  pair, has no parts. A consumer gets no per-part figure for it rather than a
  wrong one.
- Other kinds fill `parts` only when their records name each part the same way.

## When to revisit

If the client starts writing one record per skill or per server, the parts
become items of their own, and this decision is superseded.
