# OpenAIDR docs

## Start here

- [Session collection and the session model](specs/session-collection.md) — the
  design document. The session model, stable span identity, per-agent-kind
  normalisation, the collection interface, and what the parsing dependency can
  and cannot carry.
- [Adding an agent kind](../CONTRIBUTING.md#adding-an-agent-kind) — the reader
  contract, end to end.

## Reference

- [Agent kind coverage](specs/session-collection.md#agent-kind-coverage) — what
  each candidate agent kind would cost, measured rather than assumed, including
  why Cursor cannot be added dependency-only without breaking span identity.
- [What the dependency cannot carry](specs/session-collection.md#what-the-dependency-cannot-carry)
  — why the reader walks the tree and calls `adr-sensor`'s parser per file
  rather than using upstream's whole-tree walk.
- [The session model](specs/session-collection.md#the-session-model) — session,
  turn and tool call, with the local-only boundary marked per field.
- [Stable span identity](specs/session-collection.md#stable-span-identity) —
  the load-bearing contract, and what disqualifies a candidate derivation.

## Decisions

- [ADR index](adrs/INDEX.md) — architecture decision records. Read the full ADR
  before changing logic in an area one covers; the index one-liners say when.
- [ADR template](adrs/TEMPLATE.md)

## Releases

- [Release notes](releases/README.md) — one file per release, which is also the
  body of the GitHub Release. There is no `CHANGELOG.md` by design.

## Plans

- [`plans/`](plans/) — implementation plans. Historical: they record how a
  piece of work was sequenced, not what the code does now. The spec and the
  ADRs are the current account.
