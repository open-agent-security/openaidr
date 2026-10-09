---
id: 0014
title: Record usage once per provider response, from its last record
status: proposed
date: 2026-10-04
supersedes: null
superseded-by: null
---

## Context

Claude Code writes token usage on every `assistant` record, and it writes one
provider response as several records, one per content block, each repeating the
response's `message.id` and a `usage` object. Measured on 193 real transcript
files holding 13,011 responses (2026-10-06): 71% of responses span more than one
record (up to 13), so the 27,661 usage objects on disk describe 13,011
responses. In 457 responses the records disagree, and only in output.
`output_tokens` rises from record to record and the last record holds the
maximum every time. Only the final record carries `output_tokens_details`,
`server_tool_use` and `speed`, and only it has a non-null `stop_reason`. The two
coincide on every response. Ten responses consist only of thinking or empty
records, which upstream projects into no message, so no turn exists for them
(ADR-0001).

The final record is not always written. Every response in a main transcript
has one. In sub-agent transcripts, including workflow agents', 1,579 of 2,353
responses do not. Their last record is a streaming snapshot whose output is
usually 10 tokens or fewer, and nothing else on disk states the true figure.

A parent transcript also restates each sub-agent's last response on the
delegation result (`toolUseResult.usage`, `totalTokens`): equal to it on all 23
measured. The sub-agent's own transcript carries that response and every other.

## Decision

Usage is modelled per provider response, not per turn or per record. Within a
session, records sharing a `message.id` are one response, and every field of
that response is read from its last committed record in file order, with
nothing merged across records. The response is **final** when that record
carries a stop reason. Otherwise its output is a lower bound, and the model
says so rather than estimating the rest. A turn carries the id of the response
its record belongs to. A delegation summary on a tool result is never read as
usage.

A response still being written is reported from its newest committed record and
revised by a later read, so the response id is the key a consumer updates by.

## Alternatives considered

- **Usage on each turn, as the record states it.** Rejected: it counts a
  multi-record response once per record (about 2.1× on the measured corpus), and
  it loses every response that produced no turn.
- **The first record of a response.** It is what a deduplicating reader
  naturally keeps, and it is wrong: on the 457 streaming responses it
  undercounts output and omits the fields only the last record carries.
- **Merge fields across records, or take the maximum of each.** Matches today's
  data, but it reports a combination no single record stated, which this
  package does not do anywhere else (ADR-0007 resolves facts from one read).
- **Report no output, or drop the response, when it is not final.** Rejected:
  its input and cache counts are exact on any record, and those are most of
  what a response costs. Hiding them to avoid a lower bound loses true data.
- **Estimate the missing output, or fill it from the client's own totals.**
  Rejected: an estimate is not what the session recorded. The client's totals
  are per session, cover side calls too, and arrive only after the work, so
  apportioning them to responses is a consumer's reconciliation.
- **Key on `requestId`.** Missing on 6 measured records, and `message.id` is
  present on every usage-bearing record.
- **Deduplicate across sessions inside this package.** Not observed to be
  needed: no response id appeared in two sessions or two files. Doing it would
  also require deciding which session owns a response, which is a consumer's
  question. The id is carried so a consumer can.
- **Read the delegation summary as the sub-agent's usage.** It is one response
  the sub-agent's own transcript already carries, so reading it double counts
  that response and still says nothing about the rest.

## Consequences

Totals are correct by construction for any consumer that sums responses, except
for output on responses that are not final, which the model marks. Cost can be
attributed to tool calls through the turn link. The cost is a new level in the
model beside turns, and an update contract: a consumer that appends instead of
replacing by response id will double count a growing session's newest response.

A consumer that needs sub-agent output in full has to take it from elsewhere,
such as the client's own totals (ADR-0015). This package does not.

**Watch for:** a resumed session re-emitting an assistant record under a new
session id. Two sessions would then each report the same response.

## When to revisit

If Claude Code starts writing one record per response, starts writing the final
record in sub-agent transcripts, or records a response's usage somewhere other
than its records. If a response id is found in two sessions, decide ownership
in a new ADR. When another agent kind lands, measure its format first. Codex,
measured on five sessions, writes one usage record per `response_id` beside a
running `token_count` total, so this decision carries over with its own
collapse, reading the per-response record and never the total. Whether a Codex
record can be written before its response completes has to be measured for its
`final` rule.
