---
id: 0031
title: Codex usage is one response per token_usage_record, with cached input taken out of input
status: proposed
date: 2026-10-16
supersedes: null
superseded-by: null
---

## Context

Codex writes two usage records. `token_usage_record` is written once per
completed model response with its `response_id`: 73 records and 73 distinct ids
in a measured corpus, and 11 of 11 in the mock-model run. `token_count` reports
a running total after each response, repeats after a compaction with a
synthetic total, leaves out compaction requests, and reports a context-full
estimate with zero input after an overflow.

Codex's `input_tokens` includes its cached tokens (the Responses API's meaning;
measured, cached never exceeds input). This model's input excludes cache reads,
as Claude Code's and Devin's do, and every consumer sums the four token kinds
as disjoint. Codex also writes `cache_write_input_tokens` as 0 where the
provider reported nothing (`codex-api/src/sse/responses.rs`).

## Decision

One `ModelResponse` per `token_usage_record.response_id` (ADR-0014), never from
`token_count`. `input_tokens` is `input_tokens − cached_input_tokens`;
`cache_read_input_tokens` is `cached_input_tokens`; `cache_creation_input_tokens`
is the recorded value when above 0, else `None`; `reasoning_tokens` is
`reasoning_output_tokens`, and output still includes it. The response carries
the model's `context_window` from the turn's `task_started`, a new additive
field. A turn takes the `response_id` of the next usage record in its Codex
turn, since a thread has one response in flight at a time.

## Alternatives considered

- **Read `token_count`.** Rejected: it counts compactions wrongly in both
  directions and reports synthetic totals.
- **Carry `input_tokens` as recorded.** Rejected: a consumer summing input and
  cache reads would count the cached tokens twice, in every total, context
  size and price.
- **Carry a 0 cache write as 0.** Rejected: Codex writes 0 for "not reported",
  so 0 would state a measurement the record does not hold.

## Consequences

Codex usage is comparable with the other kinds' token for token. A provider
that does report cache writes through Codex has them carried. A consumer that
needs a model's window no longer needs a table by model name for Codex.

## When to revisit

If Codex changes what `input_tokens` holds, or starts writing a usage record
before a response completes.
