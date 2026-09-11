# OpenAIDR

[![PyPI](https://img.shields.io/pypi/v/openaidr.svg)](https://pypi.org/project/openaidr/)
[![Python](https://img.shields.io/pypi/pyversions/openaidr.svg)](https://pypi.org/project/openaidr/)
[![Licence](https://img.shields.io/badge/licence-Apache--2.0-blue.svg)](https://github.com/open-agent-security/openaidr/blob/main/LICENSE)
[![CI](https://github.com/open-agent-security/openaidr/actions/workflows/ci.yml/badge.svg)](https://github.com/open-agent-security/openaidr/actions/workflows/ci.yml)

Session collection for AI coding agents.

OpenAIDR reads the session state AI coding agents already write to disk and
normalises it into one session model with stable span identity.

> **Status:** beta, on [PyPI](https://pypi.org/project/openaidr/). One
> agent kind reads today (`claude-code`). Start with
> [Install and run](#install-and-run), then
> [What OpenAIDR reads, and what it emits](#what-openaidr-reads-and-what-it-emits)
> if you want to know what leaves your machine before you run it. (Nothing does.)

**Today it reads one kind: Claude Code.** Codex and Cursor are the intended next
kinds and are not read yet — a session from either is absent from the output
entirely, not collected and marked. The parsing dependency supports more formats
than this package instantiates, which is a distinction worth holding onto; what
each additional kind would cost is measured in
[Agent kind coverage](https://github.com/open-agent-security/openaidr/blob/main/docs/specs/session-collection.md#agent-kind-coverage).

It answers **what an agent did**, never what that means. No scoring, no identity
resolution, no findings, no upload path. Interpreting a session is a consumer's
job; OpenAIDR takes no position on what a consumer concludes.

## What OpenAIDR reads, and what it emits

OpenAIDR reads files the agent already wrote — Claude Code's JSONL transcripts
and its per-server MCP connection logs — opened **read-only**. No process hooks,
no proxy, no injected agent, no listener, and nothing is written back.

**There is no upload path at all.** OpenAIDR reads, normalises and returns a
model. It opens no network connection and ships nothing anywhere. What a
consumer does with the model is the consumer's decision and the consumer's
boundary to state.

Transcripts contain a person's own words, the arguments they passed to tools,
and the paths they work in. The session model separates that from the structural
account of what happened, and the separation is enforced in code, not by
convention — [`LOCAL_ONLY`](https://github.com/open-agent-security/openaidr/blob/main/src/openaidr/model.py)
names every field that serves correlation and local rendering only.

Neither CLI surface prints the conversation:

| | Text (`--detail`) | `--format json` |
|---|---|---|
| Session id, agent kind, start, turn count | shown | emitted |
| Tool name, MCP server, status, result size, duration, truncation | shown | emitted |
| Span identity | — | emitted |
| Model, source, working directory | — | emitted |
| MCP transport, endpoint, advertised name/version, failure *category* | aggregate counts | emitted per connection |
| **Turn text, initial prompt, context items** | never | never |
| **Tool arguments, tool results** | never | never |
| **Machine, user** | never | never |
| **MCP failure detail** (the server's own words) | never | never |
| Tool error text | shown | withheld |

`--format json` is the shape most likely to be redirected to a file, piped into
another program, or pasted somewhere, so it is the narrower of the two: it
carries the structural account and drops even the error text the local text
surface shows. There are tests asserting that a prompt and an `sk-`-shaped
token present in a transcript reach neither surface.

One honest caveat: the boundary described above is the boundary of the **CLI**.
The in-memory `Session` model does carry the `LOCAL_ONLY` fields, because a
consumer embedding OpenAIDR as a library may legitimately need them on the
machine that produced them. If you embed it, `Session.public_descriptor()` is
the deliberately narrow shape intended to travel outward, and drawing your own
boundary is your call to state.

## What OpenAIDR adds on top of `adr-sensor`

Session parsing itself comes from [Uber's ADR project](https://github.com/uber/ADR)
(`adr-sensor`, Apache-2.0) — it owns the on-disk formats, and it is OpenAIDR's
only runtime dependency. It is confined behind an adapter: upstream types
convert at the reader boundary and never appear elsewhere. Any one agent kind
can instead be backed by an OpenAIDR-owned reader on the same contract — per
kind, not all-or-nothing.

What the adapter adds is not a thin wrapper. Upstream's unified event model was
measured against this one rather than assumed
([the full table](https://github.com/open-agent-security/openaidr/blob/main/docs/specs/session-collection.md#what-the-dependency-cannot-carry)):

| | `adr-sensor` supplies | OpenAIDR adds |
|---|---|---|
| **Discovery** | A whole-tree walk keyed on the `sessionId` *field* | A per-file walk. Every record in a subagent transcript carries the **parent's** `sessionId`, so upstream's own walk collapses a parent and its subagents into one identity. OpenAIDR keeps the file path, which is the only thing distinguishing them. |
| **Span identity** | No call identifier below the session | An identity for one tool call that survives re-reads of a growing session, derived only from what cannot change as it grows |
| **Outcomes** | `success` or `unknown` | A six-status model — `ok`, `error`, `rejected`, `pending`, `interrupted`, `unknown` — with five emitted from Claude Code's own records |
| **Timing** | No timestamp below the session | Per-call wall clock, from the transcript's record of both ends of a call |
| **MCP** | Nothing | Transport, the server's advertised identity, connection outcome and per-call outcome, read from the per-server connection logs — the only place any of it is written down |
| **Tool names** | The raw name | A split into `(server, tool)` **per kind**, never one global pattern: applied to the wrong kind a general pattern does not fail to find a server, it finds the wrong one |
| **Agent kinds** | An on-disk self-name | A mapping to an agent kind, where an unmapped kind is reported as unmapped rather than guessed |
| **Failures** | A per-file error printed to stdout and swallowed | That narration captured and turned into a reported `ReaderFailure`, so an unreadable file is a stated gap rather than a silent absence |
| **Privacy boundary** | None | `LOCAL_ONLY`, above |

The pin is exact (`adr-sensor==1.0.0`, not `>=`): the adapter reads upstream
internals as well as its public entry point, and a test asserts the exact error
string upstream prints, so a version bump is a deliberate re-check rather than
silent drift.

## Install and run

```bash
uv tool install openaidr
```

That installs an `openaidr` command:

```console
$ openaidr --version
openaidr 0.1.0
```

`openaidr sessions` prints what each agent on this machine actually did —
sessions, turns, tool calls, and how each one ended. Per-call timings come from
the transcript's own record of both ends of a call, not from the parsing
dependency's event schema, which carries no timestamp below the session.

For Claude Code it also reads the per-server MCP connection logs, which is the
only place the transport, the server's advertised identity, and the outcome of
an MCP call are written down. That enrichment is best-effort — the path is an
undocumented cache — and every way it can fall short is reported rather than
passed off as an absence.

```console
$ uv run openaidr sessions --since 2d --detail
claude-code:96a1d0d4-d106-4cd1-9031-1e174593f8d0  [claude-code]  2026-08-29T05:11:38+00:00  86 turns
                   142c  Read
                    79c  Bash
    rejected        41c  github/create_issue
```

`--format json` emits the same account machine-readably, `--agent-kind` selects
one kind, and `--since` bounds the window. Nothing is resolved to a component
and nothing is judged: what a consumer concludes is the consumer's concern.

## Design

[`docs/specs/session-collection.md`](https://github.com/open-agent-security/openaidr/blob/main/docs/specs/session-collection.md)
— the session model, span identity, per-agent-kind normalisation, and the
collection interface. Decisions in
[`docs/adrs/`](https://github.com/open-agent-security/openaidr/blob/main/docs/adrs/INDEX.md),
and a doc index in
[`docs/`](https://github.com/open-agent-security/openaidr/blob/main/docs/README.md).

## Status

**Beta.** Session collection works for `claude-code`: sessions, turns, tool
calls, and span identity, read by an OpenAIDR-owned reader that calls
`adr-sensor`'s Claude Code parser one file at a time and normalises everything
upstream's shared event schema cannot express. The model has six outcome
statuses; this reader emits five of them (`unknown`, `rejected`, `pending`,
`ok`, `error`) from Claude Code's own transcripts and, for MCP calls, its
per-server connection logs — `interrupted` is modelled but not emitted, since
neither source records it.

Not yet built, in the order they matter:

- **Two further agent kinds, Codex and Cursor.** What each would cost is
  measured in [Agent kind coverage](https://github.com/open-agent-security/openaidr/blob/main/docs/specs/session-collection.md#agent-kind-coverage)
  rather than assumed — including why Cursor cannot be added dependency-only
  without breaking span identity.
- **Incremental collection.** Every run today is one cold pass: the whole
  window re-walked and re-parsed from scratch, held in memory. That is
  affordable once and not on every change. Because sessions are append-only, a
  steady-state path can instead re-read only the files whose modification time
  moved, replacing each session with its longer self — and span identity is
  derived the way it is precisely so that every span already emitted survives
  that re-read rather than shifting under a consumer.

Why this reader walks the tree and calls `adr-sensor`'s parser per file, rather
than going through upstream's own whole-tree walk, is recorded in
[the spec](https://github.com/open-agent-security/openaidr/blob/main/docs/specs/session-collection.md#what-the-dependency-cannot-carry).

## Contributing

Reading an agent's session log and normalising it is *plumbing*, and every new
agent format is coverage a contributor can add without touching anything that
interprets the result. That is the contribution this project is shaped around,
and [`CONTRIBUTING.md`](https://github.com/open-agent-security/openaidr/blob/main/CONTRIBUTING.md)
walks the reader contract end to end — the four wiring points, then the traps
that cost us an ADR each.

## Security

For a vulnerability in OpenAIDR's own code, see
[`SECURITY.md`](https://github.com/open-agent-security/openaidr/blob/main/SECURITY.md)
— report privately via a GitHub security advisory rather than a public issue.

## Licence

Apache-2.0. See
[`LICENSE`](https://github.com/open-agent-security/openaidr/blob/main/LICENSE).

OpenAIDR depends on `adr-sensor` from [Uber's ADR project](https://github.com/uber/ADR),
Copyright Uber Technologies, Inc., also Apache-2.0, consumed unmodified from
PyPI with no source vendored here. Attribution is in
[`NOTICE`](https://github.com/open-agent-security/openaidr/blob/main/NOTICE),
which ships inside both the wheel and the sdist.
