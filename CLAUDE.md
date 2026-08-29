# OpenAIDR — open session collection for AI coding agents

## What this repo is

OpenAIDR reads the session state AI coding agents already write to disk — Claude
Code, Cursor, Codex, opencode, Claude Desktop and others — and normalises it into
one session model with stable span identity. It answers *what an agent did*, never
what that means: no scoring, no identity resolution, no findings, and no upload
path.

Apache-2.0, and deliberately plumbing rather than judgement. Every new agent
format is coverage a contributor can add without touching anything that
interprets the result. The proprietary `stacktrace` package is one consumer;
nothing in this repository may import, reference or be shaped by it.

## Common commands

```bash
uv sync                                    # install / update deps
bash scripts/install-hooks.sh              # one-time, install pre-push gate
uv run ruff check .                        # lint
uv run ruff format --check .               # format check
uv run pyright                             # type check
uv run pytest -v                           # run tests

uv run openaidr sessions                   # print collected sessions
```

## Architecture

OpenAIDR turns each agent kind's on-disk session state into one model, without
letting kind-specific quirks — or the dependency that parses them — leak past
that boundary.

**Session parsing is not ours.** [`adr-sensor`](https://github.com/uber/ADR)
(Apache-2.0, Uber's ADR project) reads the seven agent formats it supports, and
is confined behind an adapter: upstream types convert at the boundary and never
appear elsewhere in the codebase. Any one agent kind can instead be backed by an
OpenAIDR-owned reader on the same contract — per kind, not all-or-nothing — which
is what bounds dependency risk to a single module.

**Three nested levels.** A session is one agent conversation; a turn is one
exchange; a tool call is one invocation and its outcome. Normalisation happens
once, at collection, using per-kind rules: the agent's on-disk self-name maps to
an agent kind, a tool name splits into `(server, tool)`, and a result shape maps
to one of five statuses. Downstream sees one uniform model.

**Span identity is the load-bearing contract.** It names one tool call, and it
must survive re-reads of a growing session — a consumer draws a call when parsed
and attaches outcome and enrichments later by naming the same span. It is derived
only from things that do not change as a session grows: which session, which
turn, which call within that turn. Anything positional in a mutable list is
disqualified. A change to how it is derived is a breaking change for every
consumer, and it fails silently — enrichments land on the wrong row.

Full design: `docs/specs/session-collection.md`. Decisions: `docs/adrs/`.

## Repo conventions

- **Plumbing, never judgement.** No scoring, no identity resolution, no findings,
  no severity, no confidence. If a change would make OpenAIDR take a position on
  what a session *means*, it belongs in a consumer instead.
- **No upload path, ever.** OpenAIDR reads, normalises and returns a model. It
  ships nothing anywhere. A consumer's boundary is the consumer's to state.
- **Nothing proprietary.** No import, config key, URL or mention of any closed
  product. The property to hold: OpenAIDR installs and runs correctly for someone
  who has never heard of Stacktrace. That is one grep in CI.
- **Per-kind rules, never one global pattern.** A general tool-name pattern
  mis-splits any server name containing the delimiter, and the result is not a
  missing server but the *wrong* one. A silent wrong answer is worse than a gap.
- **Absence is not falsehood.** An unmapped agent kind is reported as unmapped; a
  truncated value is shown as truncated.
- **Read-only sensing.** The JSONL and SQLite caches agents already write, opened
  read-only. No process hooks, no proxy, no injected agent, no listener.

---

## Behavioral guidelines

**Tradeoff:** these bias toward caution over speed. For trivial tasks,
use judgment.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Test: would a senior engineer say this is overcomplicated? If yes,
simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: every changed line should trace directly to the user's
request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
```

### 5. Verify Before Claiming Done

**Evidence before assertions, always.**

- Run the tests; don't say "should pass" — say "passed" only after the
  command exited green.
- If you can't run the verification (no UI access, no test infra), say
  so explicitly. Don't claim success based on type-checks alone.
- Adversarial review before declaring done: re-read the diff and ask
  "what did I miss? what did I assume? does this test the problem
  space or just my implementation?"

## Architecture Decision Records (ADRs)

Durable design notes belong in `docs/adrs/`.

**When to write a new ADR:** when you make a decision where (a) the
rejected alternative is *plausible*, (b) it's *likely to be
re-suggested* (by a future you, a teammate, or another agent), and
(c) the reason isn't obvious from the code alone.

The bar matters. Most decisions don't clear it. The test: would a
future reviewer or agent, looking only at the code, plausibly suggest
the alternative we rejected? If yes, write the ADR.

**Read.** Before changing logic in an area an ADR covers, read the
full ADR. The cost of one Read is much smaller than the cost of
re-deriving or re-litigating a decision.

**Supersede, never edit.** Accepted ADRs are immutable. If a later
decision contradicts an existing ADR, write a NEW ADR with
`supersedes: NNNN` in its frontmatter, and update the old one's
frontmatter to `status: superseded` + `superseded-by: NNNN`. Old PRs
need to remain readable against the rules in effect at the time —
silently editing an accepted ADR breaks that contract.

## Commits and PRs

- Frequent commits, one logical change per commit.
- Commit messages: focus on WHY (the motivation, the constraint), not
  WHAT (the diff already shows that).
- Push to remote at logical points; don't hoard local commits.
- Only create commits when the user requests one. If unclear, ask.
- Only push to a remote when the user requests it.
- **Finishing a feature branch: default to push + open a PR.** PR
  review is the merge path even on solo repos. Exceptions only when
  the user explicitly says "merge locally," "keep as branch," or
  "discard."

## TDD for business logic

For non-trivial business logic, write the failing test first, then
make it pass, then refactor. The bite-sized red/green/refactor/commit
cadence keeps the loop tight and the diff reviewable.

Skip TDD discipline for: throwaway scripts, exploratory spikes,
obvious one-line fixes, infrastructure config (Dockerfiles, CI YAML,
shell scripts where tests cost more than the change is worth).

## Verifying claims about external behavior

When a review comment, design decision, or bug report turns on how a
*third party* behaves — an API contract, a file or lockfile format, a
tool's actual output — verify against ground truth before accepting or
rejecting it:

- **Fetch the authoritative source.** Use `WebFetch` for the docs/spec,
  `WebSearch` to find it, or run a script against the real API / a real
  sample.
- **In-repo ADRs, plans, and tests are NOT evidence for an external
  claim.** They record what *we chose*, not what the third party
  requires. A test written by the same author who holds an assumption is
  self-referential: it confirms the assumption rather than falsifying it.
- **Distinguish "I verified this is false" from "I could not disprove
  it."** Absence of in-repo disproof is not disproof. If you lack the
  access to verify (no network, no real sample, a denied tool), say so
  explicitly and **defer** — flag the uncertainty and escalate to a human
  rather than confidently pushing back on a bare citation. A factual
  dispute about external behavior you cannot settle is a stop-and-ask,
  not a win-the-argument.

This applies to every agent — the interactive assistant, a review bot, a
subagent — not just one surface.

## Risky / hard-to-reverse actions

Carefully consider reversibility and blast radius. Local + reversible
(file edits, running tests) — fine to do directly. Hard-to-reverse,
shared-state, or visible-to-others — confirm first:

- Destructive: `rm -rf`, dropping tables, killing processes,
  overwriting uncommitted changes, force-deleting branches.
- Hard-to-reverse: force-pushing, `git reset --hard`, amending
  published commits, removing/downgrading dependencies.
- Visible to others: pushing code, creating/closing/commenting on PRs
  or issues, sending messages (Slack, email), modifying shared
  infrastructure or permissions.
- Uploading to third-party tools (diagram renderers, pastebins,
  gists) — the content gets indexed/cached even if later deleted.

When you encounter an obstacle, don't use destructive actions as a
shortcut to make it go away. Identify the root cause; fix the
underlying issue rather than bypassing safety checks (e.g.,
`--no-verify`).

If you discover unexpected state — unfamiliar files, branches,
configuration — investigate before deleting or overwriting. It may
represent the user's in-progress work.

