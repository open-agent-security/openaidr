---
id: 0024
title: The Devin agent kind is `devin`, not `devin-cli`
status: proposed
date: 2026-10-10
supersedes: null
superseded-by: null
---

## Context

0.5.0 named the Devin kind `devin-cli`, after the program the reader was
built against. Measured on a machine running both Devin CLI 3000.11.3 and
Devin Desktop, that name describes one way of starting the agent, not the
agent:

- Devin Desktop starts its local agent by spawning its own bundled copy of the
  same program (`Devin.app/…/extensions/windsurf/devin/bin/devin acp`, version
  3000.10.48), and labels it `[devin-cli]` in its own log.
- Both write one data root, `~/.local/share/devin/cli`: one `sessions.db` at
  one schema (migration 17), one `logs/`, one `session_locks/`. Ten sessions
  in that store were owned by the Desktop's process and the rest by CLI
  processes, and this reader read all of them the same way.
- A Desktop session is distinguishable only by a client tag in
  `sessions.metadata` (`client_meta.cognition.ai/requestingTabId`), not by
  format.

A Desktop user who never ran a CLI would see their sessions under
`devin-cli`. Devin Cloud is the one Devin that writes nothing to this store;
its sessions run on Cognition's machines.

## Decision

The kind is `devin`. `AGENT_KIND_BY_SOURCE` maps the store's self-name `devin`
to `devin`, so a main session is `devin:<sessions.id>` and a sub-agent
`devin:<sessions.id>:<agent_id>`. It covers every client of the local agent:
Devin CLI, Devin Desktop's local agent, and editors driving it over ACP. There
is no `devin-cli` alias. OpenACA renames its kind in the same release window,
so the two packages agree on the name a consumer joins them by.

## Alternatives considered

- **Keep `devin-cli`**: no churn — rejected because the name is wrong for every
  Desktop session and only gets more expensive to change. The kind shipped in
  0.5.0 and 0.5.1 a day earlier, and almost nothing has been recorded under it.
- **Accept `devin-cli` as an alias**: smooths a consumer's upgrade — rejected
  because a kind with two names splits every join keyed on it, and a filter
  naming the old one would silently match nothing anyway once sessions carry
  the new one.
- **`devin-local`**: Cognition's own word for the Desktop's default agent —
  rejected as a name a reader has to learn. `devin` reads as the product, and
  a future Devin Cloud reader, which would have a different source entirely,
  can take `devin-cloud`.

## Consequences

- Session ids and span ids for Devin change prefix, so a consumer that stored
  findings or cursors under `devin-cli:` sees them as new sessions. Only test
  data exists under the old name.
- Consumers that list kinds from `AGENT_KIND_BY_SOURCE` need no change.
- Accepted ADRs and the 0.5.0 release notes keep `devin-cli`: they describe
  what shipped then.
