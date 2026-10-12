---
id: 0026
title: A built-in file tool's structured result states its success, by a per-tool table
status: proposed
date: 2026-10-10
supersedes: null
superseded-by: null
---

## Context

The Claude Code reader takes `ok` and `error` from `is_error` on the result
block and reports `unknown` where it is absent. Claude Code writes `is_error` on
Bash results either way, but on Write, Edit and Read results only when the call
failed. Every successful file write therefore read `unknown`, and a consumer that
acts only on calls that worked never saw one.

Measured over every transcript on one machine (271 files): every Write, Edit and
Read result without `is_error` carried a `toolUseResult` object naming what the
call did (`filePath` for Write and Edit, `file` for Read), and every failure
carried `is_error: true` with a plain-text `toolUseResult`. No record held both
an object and `is_error: true`, and no record held more than one result.

## Decision

Where a result carries no `is_error`, the reader reads `toolUseResult` for the
tools in `_STATED_SUCCESS`, and only those: a Write or Edit whose object has
`filePath`, or a Read whose object has `file`, is `ok`. An explicit `is_error`
still decides first. The record-level object is read only when its record holds
exactly one result, since with two it does not say which it describes.

## Alternatives considered

- **Any structured `toolUseResult` is success.** Rejected: WebFetch records an
  object with no `is_error` for a 403 or a 404 as readily as for a 200, so the
  rule would report failed requests as `ok`. A tool joins the table only once its
  own records have been checked the same way.
- **Upstream's `success`, or a result's presence, is success.** Rejected for the
  reason `_status` already gives: it launders an absence of evidence into a claim.
- **Leave it `unknown` and let consumers relax their own checks.** Rejected:
  each consumer would have to learn this kind's record shape, which is the
  per-kind knowledge this package exists to hold, and a relaxed check that
  accepts `unknown` also accepts calls that never finished.

## Consequences

- Successful built-in file calls in main-session transcripts read `ok`.
- A sub-agent transcript records no `toolUseResult` at all (Claude Code 2.1.28
  on the measured corpus), so its file calls stay `unknown`.
- The table is a claim about Claude Code's record format and can go stale. A
  client that starts writing an object on failure would turn failures into `ok`
  for that tool; `is_error` taking precedence is what bounds it today.

## When to revisit

If Claude Code writes `is_error: false` on these tools' successes, the table is
unnecessary. If a failure is ever recorded with one of these objects and no
`is_error`, the affected tool leaves the table.
