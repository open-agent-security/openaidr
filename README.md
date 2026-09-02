# OpenAIDR

Session collection for AI coding agents.

OpenAIDR reads the session state AI coding agents already write to disk — Claude
Code, Cursor, Codex, opencode, Claude Desktop and others — and normalises it into
one session model with stable span identity.

It answers **what an agent did**, never what that means. No scoring, no identity
resolution, no findings, no upload path. Interpreting a session is a consumer's
job; OpenAIDR takes no position on what a consumer concludes.

## Why it is separate

Reading an agent's session log and normalising it is *plumbing*, and it runs on
one machine — so it is open. Every new agent format is coverage a contributor can
add without touching anything that interprets the result.

Session parsing itself comes from [Uber's ADR project](https://github.com/uber/ADR)
(`adr-sensor`, Apache-2.0), confined behind an adapter: upstream types convert at
the boundary and never appear elsewhere. Any one agent kind can instead be backed
by an OpenAIDR-owned reader on the same contract — per kind, not all-or-nothing.

## Install and run

```bash
uv tool install openaidr
```

That installs an `openaidr` command:

```console
$ openaidr --version
openaidr 0.0.1
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

[`docs/specs/session-collection.md`](docs/specs/session-collection.md) — the
session model, span identity, per-agent-kind normalisation, and the collection
interface. Decisions in [`docs/adrs/`](docs/adrs/).

## Status

**Pre-alpha.** Session collection works for `claude-code`, backed by a reader of
our own over Claude Code's JSONL transcripts: sessions, turns, tool calls, and
span identity. The model has six outcome statuses; this reader emits five of
them (`unknown`, `rejected`, `pending`, `ok`, `error`) from Claude Code's own
transcripts and, for MCP calls, its per-server connection logs — `interrupted`
is modelled but not emitted, since neither source records it. One cold pass,
held in memory.

Not yet built: incremental collection over a watermark, and the six further
agent kinds that `adr-sensor` also parses — for `claude-code` the dependency's
parser is already in use, called one file at a time; the other six are simply
not read yet. Why this reader walks the tree and calls that parser itself,
rather than going through `adr-sensor`'s own whole-tree walk, is recorded in
[`docs/specs/session-collection.md`](docs/specs/session-collection.md#what-the-dependency-cannot-carry).

## Licence

Apache-2.0. See [LICENSE](LICENSE).
