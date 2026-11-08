---
id: 0042
title: Canonical edit text comes from the call's own arguments
status: proposed
date: 2026-11-08
supersedes: null
superseded-by: null
amends: null
amended-by: null
---

## Context

A consumer that asks what a file edit removed and added had to parse each
agent's own format. Claude Code records `old_string` and `new_string`, and its
`MultiEdit` records a list of them. Codex and Cursor record a patch. Canonical
`edits` held only the added text, so consumers read the raw arguments.
ADR-0021 already puts this per-kind knowledge in this package.

Some removed text is not in the arguments. A `*** Delete File:` section of a
patch holds no lines. Claude Code's `NotebookEdit` input holds no old source,
but newer builds write `old_source` in the result.

## Decision

Each `edits` entry is `{path, kind, old_text, new_text}`. `old_text` and
`new_text` come only from the arguments of the call. Where the arguments do
not hold a text, that key is absent. This package does not fill it from the
result or from the file on disk.

## Alternatives considered

- **Leave consumers to parse each format**: rejected for the reason ADR-0021
  gives. Each consumer would rebuild the same parsers and drift.
- **Fill `old_text` from the result**, such as `NotebookEdit`'s `old_source`:
  rejected. A result can be withheld (ADR-0002) or not yet written, so the
  canonical arguments of one call would change with its status. The arguments
  say what the call asked for. The result says what happened.
- **Read the file from disk** for a deleted file or a notebook cell: rejected.
  The file is not session state, and it can change after the call. The text
  would be a guess that looks like a record.

## Consequences

A consumer reads one shape for every kind. `old_text` is absent for a patch's
deleted file and for `NotebookEdit`, and a consumer must handle the absence.
A tool whose argument keys are not confirmed gets no `edits` until they are.

## When to revisit

If an agent starts to record the removed text in its arguments, or a consumer
needs the result's copy beside the arguments' copy.
