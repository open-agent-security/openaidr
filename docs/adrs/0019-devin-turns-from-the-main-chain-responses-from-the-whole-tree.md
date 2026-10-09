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

A session's turns are the path from its leaf to the root. Its responses are one
per `request_id` across every node of its tree, branches included, ordered by
first appearance; a node with metrics and no `request_id` is keyed
`node-<node_id>`. Each sub-agent tree is a session of its own,
`devin-cli:<session>:<agent_id>` (ADR-0001), and owns every node in that tree,
so no request counts in two sessions.

## Alternatives considered

- **Every branch as turns**: rejected because it interleaves an abandoned
  conversation with the current one, and a consumer cannot tell them apart.
- **Responses from the main chain only**: rejected because a regenerated
  request was a real request and consumed real tokens.
- **Counting every retry node**: rejected; ADR-0014 records usage once per
  provider response.

## Consequences

Turns can disappear between reads when a turn is regenerated, so a consumer
replaces a `devin-cli` session's turns wholesale rather than appending. Spans
are never reused: `node_id` is unique per session.

## When to revisit

If Devin starts recording sub-agent requests on the parent's chain as well, or
a retry with different metrics under one `request_id` is observed.
