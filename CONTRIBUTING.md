# Contributing to OpenAIDR

OpenAIDR is plumbing: it reads the session state AI coding agents already write
to disk and normalises it into one model. **Adding an agent kind is the
contribution this project is shaped around**, and it is deliberately possible
without touching anything that interprets the result. That guide is
[below](#adding-an-agent-kind).

## Before you start

- For a bug or a coverage gap, open an issue first. For a security
  vulnerability, do **not** open an issue — see [`SECURITY.md`](SECURITY.md).
- For a new agent kind, open an issue naming the kind before you write the
  reader. Most of the cost is in deciding what the format can and cannot
  support, and that conversation is cheaper before the code exists than after.

## Project setup

```bash
uv sync                                    # install / update deps
bash scripts/install-hooks.sh              # one-time, install the pre-push gate
```

The gates, which are exactly what CI runs:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
```

`scripts/install-hooks.sh` points `core.hooksPath` at `scripts/git-hooks/`, so
the same gates run before every push. If a push is failing and you are tempted
by `--no-verify`, fix the gate instead — the hook exists so CI is never the
first place a contributor learns something is broken.

To run the CLI against transcripts somewhere other than the default root, set
`OPENAIDR_CLAUDE_ROOT`. That is also how the end-to-end tests drive it.

### A note on the committed agent hooks

This repo commits `.claude/settings.json` and `.codex/hooks.json`, which
register `SessionStart`, `PreCompact` and `SessionEnd` hooks. All three do the
same harmless thing: `cat` a documentation file (`docs/adrs/INDEX.md` or
`docs/adrs/HOOK-PROMPT.md`) into the agent's context so ADRs get read before
logic changes. No network, no writes, no execution of anything else.

We are stating that explicitly because a committed hook file *does* mean that
opening this repo in a coding agent runs a command from it, and a project in
this problem space should not ask you to take that on trust. Read them before
you run them — and do the same in every other repo you open.

## The invariants

These hold everywhere in the codebase and are the fastest way to predict
whether a change will be accepted.

- **Plumbing, never judgement.** No scoring, no severity, no confidence, no
  findings, no identity resolution. If a change would make OpenAIDR take a
  position on what a session *means*, it belongs in a consumer.
- **No upload path, ever.** OpenAIDR reads, normalises and returns a model. It
  opens no sockets. A consumer's boundary is the consumer's to state.
- **Read-only sensing.** Transcripts and caches are opened read-only. No
  process hooks, no proxy, no injected agent, no listener.
- **Absence is not falsehood.** An unmapped agent kind is reported as unmapped;
  a truncated value is shown as truncated; an unreadable file becomes a
  reported failure. Never a silent zero.
- **A silent wrong answer is worse than a stated gap.** This is why tool-name
  splitting is per kind and never one global pattern: applied to the wrong
  kind, a general pattern does not fail to find a server, it finds the *wrong*
  one, which then resolves to the wrong component downstream.
- **`LOCAL_ONLY` is enforced, not conventional.** Turn text, initial prompts,
  context items, tool arguments, results, error text, machine, user, and an MCP
  server's own failure words serve correlation and local rendering only. They
  do not enter `--format json`. See `LOCAL_ONLY` in
  [`src/openaidr/model.py`](src/openaidr/model.py).
- **Nothing proprietary.** No import, config key, URL or mention of any closed
  product. OpenAIDR must install and run correctly for someone who has never
  heard of any consumer of it. CI enforces this with a grep.
- **The dependency stops at the reader.** `adr-sensor` types convert at the
  reader boundary and appear nowhere else — not in the model, collector,
  renderer or CLI.

## Adding an agent kind

The contract is one protocol, and a kind is wired in at four points. Nothing
downstream of the reader needs to change.

### 1. Map the agent's on-disk self-name

Agents name themselves in their own vocabulary; consumers use agent kinds. Add
the row in [`src/openaidr/kinds.py`](src/openaidr/kinds.py):

```python
AGENT_KIND_BY_SOURCE = {
    "claude": "claude-code",
    "your-agent-self-name": "your-agent-kind",
}
```

Adding a row here **is a coverage claim**. Only add it once a reader can
actually read the kind. An unmapped source is not an error — `map_source`
returns `None`, the session is still collected and still counted, it simply
resolves less.

### 2. Split tool names for that kind

If the kind prefixes or namespaces MCP tool names, add a branch to
`split_tool_name` in [`src/openaidr/toolnames.py`](src/openaidr/toolnames.py).
Add a *branch*, never a general fallback pattern — see the invariant above. If
the kind has no such convention, do nothing: an unknown kind correctly returns
the name whole with no server.

### 3. Write the reader

A reader implements the `Reader` protocol in
[`src/openaidr/readers/base.py`](src/openaidr/readers/base.py):

```python
class Reader(Protocol):
    agent_kind: str

    def collect(self, window: Window) -> tuple[list[Session], list[ReaderFailure]]: ...
```

Take an optional `root: Path | None` in `__init__` so tests can point it
somewhere else. Return failures, never raise past this boundary: `collect_from`
catches an escaping exception as a last resort, but a reader that anticipated
the failure should report it with the path or record it concerns, because only
the reader knows what the failure made unknowable.

Getting these right is most of the work, and each one is a mistake someone has
already made here:

- **Walk the tree yourself if identity depends on the path.** The Claude Code
  reader calls upstream's parser one file at a time rather than using its
  whole-tree walk, because every record in a subagent transcript carries the
  *parent's* `sessionId` — parsed as a tree, a parent and its subagents collapse
  into one identity, and the file path is the only thing that distinguishes
  them. Check whether your format has the same shape before trusting any
  id carried in a record.
- **Span identity must survive re-reads of a growing session.** A consumer
  draws a call when parsed and attaches enrichments later by naming the same
  span. Derive it only from what cannot change as the session grows — which
  session, which turn, which call within that turn. Anything positional in a
  mutable list is disqualified. This fails *silently* when wrong: enrichments
  land on the wrong row. See
  [ADR 0001](docs/adrs/0001-session-turn-and-subagent-identity.md).
- **Session ids must be unique across the whole collection.** The collector
  treats a repeat as a collision and reports it rather than overwriting.
  Namespace by path where the format does not guarantee uniqueness.
- **Scope every recovery key to the identity that makes it unique.** A uuid is
  unique within its session, a provider call id within the session that issued
  it, a log's session id within its project directory. See
  [ADR 0004](docs/adrs/0004-recovery-key-scoping.md).
- **Map outcomes into the six statuses, and withhold rather than guess.** The
  model has `ok`, `error`, `rejected`, `pending`, `interrupted`, `unknown`. If
  the format does not record an outcome, that is `unknown` — do not infer
  success from the absence of an error. If two calls are structurally
  indistinguishable and you cannot tell whose result is whose, withhold both.
  See [ADR 0002](docs/adrs/0002-structural-duplicate-outcome-withholding.md).
- **Honour `LOCAL_ONLY`.** Populate those fields if the format has them, but
  never route them to a surface that withholds them.
- **State what a failure made unknowable.** If an unreadable directory means an
  enrichment cannot be trusted for a whole set of sessions, withhold at the
  narrowest scope that actually covers the gap, and report it. See
  [ADR 0009](docs/adrs/0009-discovery-completeness-precedes-uniqueness.md).

Whether your reader is backed by `adr-sensor` or written from scratch is a
per-kind choice made on measured fidelity, and it reaches no consumer. If you
back it with the dependency, convert upstream types at the reader boundary —
they must not appear in the model, collector, renderer or CLI.

### 4. Register it

Add it to `default_readers` in
[`src/openaidr/collector.py`](src/openaidr/collector.py). The `--agent-kind`
filter, the collector, the renderer and the CLI need no other change.

### 5. Test it

Build fixtures rather than committing real transcripts — see
[`tests/fixtures/`](tests/fixtures/) for the existing builders. **Never commit
a real transcript**: they contain prompts, paths, and sometimes credentials.

Cover, at minimum: the happy path; a subagent or nested session if the format
has one; an unreadable and a malformed file (both must become
`ReaderFailure`, not an exception and not a silent skip); the window boundary;
span identity stability across a re-read after the session grew; and an
assertion that no `LOCAL_ONLY` value reaches `--format json`.

### Measuring the cost first

[Agent kind coverage](docs/specs/session-collection.md#agent-kind-coverage)
records what each candidate kind would cost, measured rather than assumed —
including why Cursor cannot be added dependency-only without breaking span
identity. Read it before starting; it may already answer your question.

## Architecture Decision Records

Durable design notes live in [`docs/adrs/`](docs/adrs/INDEX.md).

**Read before changing.** If an ADR covers the area you are touching, read the
full ADR first. The index one-liners say when.

**Write one** when the rejected alternative is *plausible*, *likely to be
re-suggested*, and the reason is not obvious from the code. Most decisions do
not clear that bar. Use [`docs/adrs/TEMPLATE.md`](docs/adrs/TEMPLATE.md).

**Supersede, never edit.** Accepted ADRs are immutable. A later contradicting
decision gets a NEW ADR with `supersedes: NNNN`, and the old one's frontmatter
becomes `status: superseded` + `superseded-by: NNNN`. Old PRs need to stay
readable against the rules in force at the time.

## Commits and PRs

- One logical change per commit. Commit messages say **why** — the diff already
  says what.
- Write the failing test first for non-trivial logic.
- PRs go through review even on a solo repo. CI must be green.
- Match the surrounding style. Don't reformat adjacent code, and don't delete
  pre-existing dead code in an unrelated PR — mention it instead.

## What does not belong here

- Anything that interprets a session: scoring, severity, findings, identity
  resolution, detection rules.
- Any upload, telemetry, or network call.
- Any mention of, or coupling to, a closed product.
- Real captured transcripts, in tests or docs.

## Code of conduct

Treat contributors and maintainers with respect. Disagree on technical merits,
not on people. When a factual dispute turns on how a third party behaves —
a format, an API, a tool's real output — settle it against the actual source
rather than by argument, and say plainly when you could not verify something.
