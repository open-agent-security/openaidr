---
id: 0040
title: Preserve native client metadata without changing runtime identity
status: proposed
date: 2026-10-21
supersedes: null
superseded-by: null
amends: null
amended-by: null
---

## Context

Different desktop surfaces write Codex rollouts with the same `source` and different `originator` values.
The collector currently retains the entrypoint and discards the recorded client name.
Consumers cannot distinguish these clients without parsing the native files themselves.

## Decision

Append optional native originator metadata to the session model.
Preserve it as recorded without changing agent kind, entrypoint, session identity or span derivation.
Keep it local-only and interpret no product category or authorization from it.
See [Native client metadata](../specs/codex.md#native-client-metadata) for the contract.

## Alternatives considered

- **Create an agent kind for each client name.** Rejected. A shared runtime format does not establish a different collection contract.
- **Replace the entrypoint with the originator.** Rejected. The source already describes how the runtime is driven.
- **Interpret known clients inside collection.** Rejected. The collector records native facts; consumers decide how to present them.
- **Add arbitrary originators to the public descriptor.** Rejected. A client-controlled string can contain private content.

## Consequences

Consumers can distinguish recorded clients without a second native parser.
Unknown clients remain usable without a package update.
Existing identity and token accounting remain unchanged.

## When to revisit

Revisit when a native client name identifies an incompatible runtime format or the vendor supplies a dedicated surface field.
