---
name: New agent kind
about: Propose coverage for an agent whose sessions OpenAIDR does not read yet
labels: agent-kind
---

Please read
[Adding an agent kind](../../CONTRIBUTING.md#adding-an-agent-kind)
and
[Agent kind coverage](../../docs/specs/session-collection.md#agent-kind-coverage)
first — the second may already record what this kind would cost.

## Which agent

- Name and version:
- What it calls itself on disk (the `source` value):

## Where it writes session state

- Path(s):
- Format (JSONL, SQLite, other):
- Is the path documented by the vendor, or an undocumented cache?

## Identity

- Does a record carry its own session id, or does identity depend on the file path?
- Does the format have subagents / nested sessions? If so, do their records
  carry the parent's id?
- What could span identity be derived from that does **not** change as a
  session grows?

## Outcomes

- Does the format record how a tool call ended? Which of `ok`, `error`,
  `rejected`, `pending`, `interrupted` are actually recoverable?
- Are per-call timestamps available?

## Backing

- Does `adr-sensor` already parse this kind? If so, what does its event model
  drop that this model needs?
