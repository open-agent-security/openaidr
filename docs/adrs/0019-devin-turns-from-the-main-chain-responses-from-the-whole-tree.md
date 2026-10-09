---
id: 0019
title: Devin turns come from the main chain; its responses from the whole tree
status: proposed
date: 2026-10-09
supersedes: null
superseded-by: null
---

## Context

Devin stores a conversation as a tree. Regenerating or editing a turn forks it,
and `sessions.main_chain_id` names the current leaf. Retries are sibling nodes
with the same `request_id` and identical metrics. A sub-agent's chain is its own
tree in the same session's forest, named by `subagent_heads`.

## Decision

**One ownership rule divides the forest between sessions.** A node belongs to
the session whose declared chain holds its nearest ancestor-or-self: the main
chain (`main_chain_id` to the root) claims first, then each sub-agent's chain
(`subagent_heads.chain_node_id` to the root), as `devin-cli:<session>:<agent_id>`
(ADR-0001). A node below no declared chain -- an abandoned regeneration, a tree
no chain reaches -- follows the nearest claimed node above it, and belongs to
the main session when there is none.

Everything a session reports is drawn from the nodes it owns, through that one
map: its turns are its declared chain restricted to them; its responses are one
per `request_id` across all of them, branches included, ordered by first
appearance (a node with metrics and no `request_id` is keyed `node-<node_id>`);
its start and last activity are their times, plus the row's own columns for the
main session only. So no request counts in two sessions, and a sub-agent's
turns, usage and times cannot disagree about which nodes are its.

## Alternatives considered

- **Every branch as turns**: rejected because it interleaves an abandoned
  conversation with the current one, and a consumer cannot tell them apart.
- **Responses from the main chain only**: rejected because a regenerated
  request was a real request and consumed real tokens.
- **Counting every retry node**: rejected; ADR-0014 records usage once per
  provider response.
- **Ownership by case** (a separate tree owns everything in it; a head on the
  main tree owns only its own path; turns, usage and times each filtered on
  their own): rejected after review found, in turn, a parent's turns repeated
  in a sub-agent, a sub-agent's activity and start taken from the parent row,
  and a sub-agent's abandoned regeneration billed to the parent. Each was a
  place the cases disagreed; one rule leaves nothing to disagree. A randomised
  test holds every session to the rule over generated forests.

## Consequences

Turns can disappear between reads when a turn is regenerated, so a consumer
replaces a `devin-cli` session's turns wholesale rather than appending. Spans
are never reused: `node_id` is unique per session.

## When to revisit

If Devin starts recording sub-agent requests on the parent's chain as well, or
a retry with different metrics under one `request_id` is observed.
