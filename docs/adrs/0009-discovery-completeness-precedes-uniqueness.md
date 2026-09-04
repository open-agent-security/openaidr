---
id: 0009
title: A uniqueness fast path requires a complete discovery pass, and the unit of unread evidence sets the scope
status: accepted
date: 2026-09-02
supersedes: null
superseded-by: null
---

## Context

Two fast paths carry this reader's whole MCP join, and both are uniqueness
claims:

- ADR-0004: a cache log filed under exactly one project **is** that session's
  log, whatever the directory is called, because the two directory manglings
  cannot be compared.
- ADR-0008: a raw session id claimed by exactly one transcript file names one
  session, so that log can be handed to it.

Each holds only if the pass *enumerated every claimant*. Both trees are walked
with `os.walk` rather than globbed, because `Path.glob()` suppresses every
`OSError` raised while scanning as of Python 3.13 and a directory this process
cannot list would vanish with no trace at all. `onerror` recovers the name of
the directory that failed — and until now that name became a `ReaderFailure`
and nothing else.

A reported failure is not a withheld answer. With one project directory
unscannable, a transcript inside it claiming the same raw session id as a
readable one is invisible: the readable twin is left looking like the id's sole
claimant, ADR-0008's check passes, ADR-0004's fast path fires, and the session
receives connections and outcomes that may be the hidden file's. The same holds
one tree over: an unscannable cache subtree can hold a second project that filed
a log under this id, and `for_session` reports the one it *could* see as a
unique match.

This is the third review round to land on this shape at a different call site —
after the per-file `stat()` probes and the window check, each of which had to
keep a file it could not read in the claimant set. The pattern is that **every
I/O failure has two obligations, and only the first is obvious**: report it, and
say what it makes unknowable.

Measured on one machine, to size both the hazard and the guard:

| | Measured |
|---|---|
| Non-subagent transcripts / distinct raw ids / ids claimed by more than one file | 462 / 462 / **0** |
| Cache log files / session ids in them / ids filed under more than one project | 1,744 / 636 / **0** |
| Cache project directories / transcript project directories / exact-name overlap | 145 / 51 / 46 |

So the collision itself is unobserved, and the third row is why it cannot be
cheaply detected by comparing names: 99 of 145 cache directories have no
counterpart under `~/.claude/projects`, because the cache truncates a long
project path and appends a hash suffix while the transcript root does not.

## Decision

**A uniqueness fast path may only fire on a discovery pass known to be
complete.** An unscanned directory withholds the whole enrichment — connections
included — for every session on the side it occurred, and says so in its own
state: `transcript_discovery_incomplete` or `log_discovery_incomplete`.

Connections go too, which a count mismatch does not do. The distinction is
already drawn in ADR-0003's cache-side-collision branch: a count disagreement
leaves identity intact, so a transport is still a property of a connection this
session certainly had, whereas here it is the *identity* that is in doubt and
every fact in the entry may belong to another session.

**The unit of unread evidence sets the scope, and the dividing line is whether a
name was seen.**

- An unreadable or torn **file** was named by the directory holding it, so both
  its project and its server are known. ADR-0006 stands unchanged: the loss is
  scoped to `(project, server)`, and a same-named server's bad file in an
  unrelated project withholds nothing here.
- An unscanned **directory** yields no names at all. Any project, any server and
  any session id can be inside it, so there is no narrower scope to be had than
  the tree it happened in.

**A session whose log resolved to nothing has no cache project to scope
against**, so the incompleteness check there matches on the server name alone.
It cannot borrow the transcript's project name: by the third row above the two
manglings disagree for 99 of 145 directories, so comparing them could only ever
fail to match, and the guard written that way never fired — a log that was
merely unread was reported as `no_log_for_session`, which the renderer states as
a pruned cache.

## Alternatives considered

- **Report the walk failure and change nothing else** — what was there. Rejected:
  the failure list says a directory could not be read; it does not stop the
  readable twin from being treated as unique. Every other unreadable-file path
  in `collect()` already does both, and this one is the only shape where no
  filename survives for ADR-0008 to fall back to.
- **Scope a failed `mcp-logs-<server>` directory to `(project, server)`**, since
  its own name states both. Rejected: that states the completeness half of the
  loss and leaves the identity half — the stronger claim — unstated. The hidden
  files can carry any session id, including this one, under that project.
- **Withhold globally for an unreadable *file* too**, on the same identity
  argument. Rejected: this is ADR-0005's mistake at one remove. An unreadable
  log file is the ordinary case the corpus actually contains, and a guard that
  fires on it would lose every session's enrichment on the machine over one bad
  file, to close a collision measured at zero. An unscanned directory is not
  ordinary, so the same guard costs nothing in the common case.
- **Match incompleteness by the transcript's project name** — what the
  unresolved-log branch did. Rejected as measurably unable to fire: 99 of 145
  cache directories have no transcript-side counterpart, which is the very
  reason ADR-0004 breaks ties on the name rather than requiring it to match.
- **Read every discovered file's session id, so a hidden twin is at least
  counted.** Rejected for the reason ADR-0008 gives: the hidden file was never
  listed, so there is nothing to open. No amount of reading recovers a name the
  walk never yielded.

## Consequences

A single unscannable directory now costs every session on that side its MCP
enrichment for that pass, stated rather than silent. That is a wide blast radius
for a narrow event, and it is the deliberate direction: the alternative is a
confident wrong answer, and the event is a permission-restricted directory, a
broken mount or a tree being deleted mid-walk — none of which the corpus
contains.

The two states are reported by `_MCP_LOG_STATE_EXPLANATIONS`, whose
completeness test made adding them a deliberate act rather than an omission.
That table is the one place in this codebase where a class of defect was closed
structurally instead of one site at a time, and it is the model this ADR follows:
the guard belongs at the point where the *precondition* is established, not at
each site that assumes it.

## When to revisit

If Claude Code writes the transcript's own path into the MCP log record, both
fast paths become exact joins and this ADR, ADR-0004's fast path and ADR-0008
all become unreachable. If a raw session id is ever observed claimed by two
transcript files, or filed under two cache projects, re-run the table in Context:
the guard's cost is unchanged but its value stops being hypothetical.
