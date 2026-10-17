---
id: 0033
title: Read Codex against its own closed vocabularies, and bound what the reader accepts
status: proposed
date: 2026-10-16
supersedes: null
superseded-by: null
---

## Context

In seventeen review rounds on open-agent-security/openaidr#76, thirteen
findings were the same defect at different sites: a call item Codex 0.163.0
writes that the reader dropped, read only in part, or named wrong. Image
generation (hosted and standalone), `ImageView`, `DynamicToolCall`, sleep,
standalone web search, an interrupted collaboration call, and Multi-Agent V2's
namespace each came in turn. Eight more were one other defect: input the
decoders accept or reject in a way the reader did not expect. That was a
`RecursionError` at four sites, a timestamp overflowing UTC at two, a
repeated ordinal, and a log row's out-of-range time. Each fix covered the site
the reviewer cited.

Codex's own types make both closeable. `TurnItem` and `ExtensionItem` are
closed enums (`protocol/src/items.rs`, `ext/items/src/lib.rs`), a paginated
rollout persists every `TurnItem` (`rollout/src/policy.rs`), and
`CollabAgentTool` names every collaboration tool. Codex also writes JSON with
serde_json's default recursion limit of 128.

## Decision

**The reader's vocabulary is Codex 0.163.0's, written as one table.**
`_CALL_ITEMS` lists every completion item that records a call, by `type`, and
by `kind` for an `Extension` item. Each entry gives the built-in it stands for,
the item fields that are its arguments, and whether the item's completion alone
means success. `_NOT_CALL_ITEMS` lists the rest of `TurnItem`, each one not an
invocation. An item in neither is reported as a coverage gap, never dropped
silently and never guessed at. `tests/test_codex_vocabulary.py` copies each
enum from Codex's source and fails by name on a member the reader misses.

**A call is named by the most specific record that names it.** The first rule
that applies wins:

1. An `McpToolCall` item's own `server` and `tool`.
2. A `CollabAgentToolCall` or an `Extension` item's own tool. Its call's
   namespace can be configured, so the item decides (`wait` is `wait_agent`).
3. The namespace the call was made under: `mcp__<server>` for MCP, a fixed
   Codex namespace for its own built-ins (`clock.sleep`, `web.run`,
   `image_gen.imagegen`), and `multi_agent_v1` or `collaboration` for
   collaboration tools.
4. A flat legacy name, split by Claude Code's rule.

Any other namespace is kept as `<namespace>.<name>` with no server. Every
built-in name the reader can emit has a tool class; a dynamic tool, which the
app-server client defines, has none.

**One bound on input, at the boundary.** A rollout record that does not decode,
or that nests more than 256 containers deep, is malformed: its session is
withheld and the path and line reported. A call's JSON arguments that do not
decode, or nest past the same bound, are kept as written under `raw`. An
instant that does not parse or does not fit UTC is absent. Every decode site in
the Codex readers is listed in a test that fails when a new one appears, and
each site is fed the same hostile inputs.

## Alternatives considered

- **Read any item that has an `id` and a `status` as a call.** Rejected: it
  would name calls by guesswork and read a plan or a review-mode marker as a
  tool, a silent wrong answer where a reported gap is honest.
- **Name every collaboration call by its namespace.** Rejected: V2's namespace
  is configurable (`multi_agent_v2.tool_namespace`), so a renamed one would
  read as an unknown tool. Matching the bare tool name in any namespace was
  also rejected: a dynamic tool of the same name would be misnamed.
- **Catch each decoder's exceptions where it is called, as before.** Rejected:
  a deep object decodes without raising and fails later, when the session is
  built or a consumer walks it, outside any reader's catch. One such record
  cost every session in a collect pass.
- **Follow Codex past 0.163.0 as findings arrive.** Rejected: an open
  vocabulary invites one review round per new item. A later version is a new
  decision, made by updating the copied enums first.

## Consequences

An item Codex adds after 0.163.0 reaches the output as a reported gap, not a
silent loss, until the table names it. Records deeper than Codex itself can
write are refused. Collaboration calls in a custom namespace with no completion
item, as for one refused before it ran, keep `<namespace>.<name>`.

## When to revisit

When Codex adds a `TurnItem`, an `ExtensionItem` kind or a `CollabAgentTool`
member, or lifts its serde_json recursion limit.
