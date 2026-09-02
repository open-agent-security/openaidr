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

That is the whole CLI today. `openaidr sessions`, which prints what each agent
actually did, arrives with the collector.

## Status

**Pre-alpha.** The design is settled; the implementation is not. What exists is
the package skeleton, the `adr-sensor` dependency, and `--version` — enough to
install and pin, not yet enough to collect anything.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
