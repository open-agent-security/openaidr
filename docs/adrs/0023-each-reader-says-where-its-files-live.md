---
id: 0023
title: Each reader says where its session files live; the consumer watches
status: proposed
date: 2026-10-10
supersedes: null
superseded-by: null
---

## Context

`IncrementalCollector.collect(kind, path)` follows a growing session once a
consumer names the file that changed, but nothing says which files to watch.
A long-lived consumer therefore hard-coded each kind's layout — Claude Code's
`~/.claude/projects`, with its sub-agents under `<session>/subagents/` — and so
watched only the kinds whose layout it had learned. Devin CLI made the gap
plain: its sessions are rows in one `sessions.db`, beside run logs and session
locks, so a watcher built on one file per session cannot follow it at all, and
none of that layout is the consumer's to know (ADR-0018).

## Decision

1. Every `IncrementalReader` has `locations()`: the directories its session
   files live under. Claude Code returns its projects root; Devin CLI its data
   root.
2. `collect_file` already ignores a file its reader does not read, so the
   contract is: hand it any file that changed beneath a location, and it
   returns the sessions that file changed, a sub-agent's included.
3. `IncrementalCollector.locations()` returns them per kind, and the collector
   takes `devin_root` beside `root` so a caller can place both.
4. OpenAIDR still watches nothing. It says where to look; the consumer chooses
   how — events, polling or neither.

## Alternatives considered

- **A watcher inside OpenAIDR.** Rejected: a listener is outside read-only
  sensing, and the consumer already owns scheduling and backoff.
- **A per-kind glob of session files.** Rejected: Devin's run logs and locks
  change sessions without being session files, and a glob is a second copy of
  what `collect_file` already decides.
- **Leave layouts to consumers.** Rejected: each consumer re-learns every kind,
  and a new kind reaches none of them until each is changed.

## Consequences

A consumer follows every kind this package reads without knowing a layout, and
a new kind's locations reach it with the release that adds the kind. A third-party
`IncrementalReader` must now implement `locations()`.

Claude Code's MCP connection logs are not a location: they are re-read whenever
a transcript is, so a change to a log alone surfaces at that session's next
read, as before.

## When to revisit

If a kind's sessions live somewhere a directory cannot name, or a consumer needs
to know which file under a location is worth reading before it reads it.
