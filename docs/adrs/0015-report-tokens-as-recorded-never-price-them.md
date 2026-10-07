---
id: 0015
title: Report tokens as recorded; never price them
status: proposed
date: 2026-10-04
supersedes: null
superseded-by: null
---

## Context

Once the model carries token counts (ADR-0014), the next request is predictable:
add a price table and a cost field, as local usage tools for coding agents
already do. Every consumer of usage wants money in the end, so the suggestion
will keep coming back.

The client records one estimate of its own. Claude Code writes `cost-state`
records (134 in 110 of 115 measured main sessions, 2026-10-06) with a running
`totalCostUSD` and per-model tokens and dollars. The record has no timestamp and
follows the work it covers. Its figure covers more than the session's own
transcript: the session's sub-agents, its workflow agents, and side calls on a
small model that no transcript records. No response record carries money. Any
other cost reported here would be computed by this package, not read from the
session.

## Decision

OpenAIDR reports token counts, and the attributes that determine their price,
exactly as recorded: model, cache lifetime split, service tier, speed, inference
region and server-side tool requests. It computes no monetary value, ships no
price table and adds no cost of its own. The client's own running totals
(`cost-state`) are not read: a record of them is recognised and skipped. A
count the record does not state is `None`, never zero.

## Alternatives considered

- **Ship a price table and a cost field.** Rejected. Prices change and are
  versioned by date, and contracted rates, tiers and regions differ by customer.
  An open-source package's figure would be read as fact by every consumer that
  never checks it. Upgrading the package would also silently rewrite historical
  costs. Pricing is interpretation of what an agent did, and this package
  reports only what it did.
- **Carry the client's own estimate, labelled as the client's.** Rejected, though
  it was the first draft of this decision. Its dollar figures are a price, which
  this package does not report, whoever computed it. Its token counts add little:
  measured on 111 sessions, 13.4M tokens beyond the 2.49B their transcripts
  record, 0.54%, mostly side calls on a small model and unfinished sub-agent
  output (ADR-0014). And it has no time, is written only after the work, and is
  written by one client alone. A field that adds half a percent and cannot be
  placed in time is not worth a consumer depending on it.
- **Report a single "total tokens" number.** Rejected because input, cache-read,
  cache-write and output tokens are priced differently. A total discards
  exactly what a consumer needs in order to price it.

## Consequences

The model stays a faithful account of the session, and every consumer prices
under its own rates and its own version of the price list. Each consumer has to
maintain a price table, and figures from different consumers can disagree. That
disagreement is genuine, because their rates differ, and it is not this
package's to hide.

## When to revisit

If an agent kind starts recording a monetary value per response. Carry it as
recorded, which this ADR already permits; revisit only if that raises a question
of precedence over a consumer's own price.
