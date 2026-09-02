---
id: 0008
title: The window scopes which sessions are returned, never which files claim an id
status: accepted
date: 2026-09-02
supersedes: null
superseded-by: null
---

## Context

`docs/specs/session-collection.md` applies the collection window to a session
file's *modification time*, before anything is parsed, so that a recurring pass
costs only the files that changed. That decision is about bounding **parse
work**, and it is right.

ADR-0004 established the other half of this reader's MCP join: a cache log found
under exactly one project is that session's log whatever the directory is
called, because the two directory manglings cannot be compared. That fast path
holds only while exactly one transcript claims the raw session id, which is why
`_ambiguous_transcript_ids` exists.

The two met in `collect()`. A file outside the window was `continue`d before
`_parse_file` ran, so it never entered `parsed`, and `parsed` is the only thing
`_ambiguous_transcript_ids` counted. A copied or restored transcript whose twin
had not been touched recently was therefore invisible to the collision check:
the surviving file looked like the id's sole claimant, ADR-0004's single-match
fast path fired, and it received a log that may have been the excluded file's.
`--since` defaults to `14d`, so this was the default path of `openaidr
sessions`, not a `--since`-only edge case — and unlike every other MCP state,
its failure mode is a confident wrong answer rather than a stated gap.

Measured on one machine's 608 real transcripts: 437 are non-subagent, and for
**all 437 the file's name is its own raw session id** — Claude Code files a
transcript as `<session id>.jsonl`. The 171 files whose name disagrees are all
subagent transcripts, which carry a *parent's* id (ADR-0001) and are already
excluded from the claimant set by directory. A full open-and-scan of every
non-subagent file cost 74 ms on that corpus.

## Decision

**The window decides which sessions are returned. It does not decide which
files claim a raw session id.** `collect()` collects the paths the window
excluded and passes them to `_ambiguous_transcript_ids`, which counts them as
claimants alongside the parsed files.

An excluded file is never parsed and never read. Its claimed id is taken from
its filename, and its subagent status from its parent directory — both already
in hand from the glob that listed it. The spec's decision is untouched: no file
outside the window is opened, and the bound on parse work is exactly what it
was.

## Alternatives considered

- **Read every discovered file's session id on every pass** (the literal fix
  the review round proposed). Rejected as buying very little for a real
  reversal: it would open every historical transcript on every recurring call,
  which is the unbounded steady-state cost mtime-windowing exists to prevent,
  and it closes only the renamed-copy shape below. The measurement above says
  the price today is 74 ms — small, but paid on every pass forever, against
  zero occurrences of the shape it would catch.
- **Document the gap and leave it** — "absence is not falsehood", as the other
  MCP states do. Rejected because that convention covers states that *say* they
  do not know: `session_id_collision`, the incomplete-log states. This gap says
  nothing. It emits an enrichment that is silently another session's, which is
  the failure this reader spent PR #6 eliminating everywhere else, and the
  repo's own rule is that a silent wrong answer is worse than a gap.
- **Key the out-of-window claimant on its project directory rather than the
  file.** Rejected for the reason ADR-0004 already gives one layer up: a
  restored copy lands beside its original under the *same* project just as
  easily as under a different one, and keying on project collapses both into
  one entry and hides exactly the collision being looked for.

## Consequences

The gap is closed for the shape that occurs: a transcript copied or restored
under another project directory, or beside its original under a different name
*that is still a session id*, is counted however the window falls.

One shape remains open, deliberately. A copy **renamed in place** to something
that is not its session id — `cp <id>.jsonl <id>-restored.jsonl` — states an id
in its records that its name no longer agrees with. While both twins are in the
window this is caught, because both are parsed and the check keys on the file
rather than the name (there is a test for it). While one is outside the window
it is not: the name is all there is, and it lies. Closing it is the 74 ms
per-pass read above, and the option stays open.

The reverse error is possible and is the safe direction: a file *named* like
another session's id, whose contents are a third session, manufactures a
collision that is not one. The result is a withheld enrichment and a stated
`session_id_collision` — a visible gap, which is the failure this reader
prefers.

## When to revisit

If a transcript is ever found in the wild whose name is not its session id and
which is not a subagent file, the filename premise is dead and the choice is
between the full read and accepting a wider gap; re-run the measurement in
Context before deciding. If Claude Code writes the transcript's own path into
the MCP log record, this whole class of collision becomes an exact join and
both this ADR and ADR-0004's fast path become unreachable.
