---
id: 0013
title: Defer native Windows support
status: accepted
date: 2026-09-21
supersedes: null
superseded-by: null
---

## Context

Session collection combines format parsing with host filesystem discovery and
incremental file tracking. Supporting another host platform requires verifying
those operations together; a portable Python package or a permissive dependency
is not evidence that native Windows collection works. The host support boundary
needs to be explicit before adding compatibility paths.

## Decision

OpenAIDR targets macOS and Linux hosts. Native Windows execution is deferred
across the library, CLI, session discovery, incremental collection and development
tooling. New work may use the facilities of those target hosts without adding
Windows substitutes.

A session record naming Windows paths is still data. Existing reader contracts
determine whether it can be parsed on a target host; Windows provenance alone
must not cause rejection. This neither promises native Windows session discovery
nor introduces Windows path translation or additional agent-format coverage.

Windows-only compatibility failures do not block a release within this scope.
Record them as deferred rather than adding adapters, fallback implementations or
Windows CI jobs as incidental fixes. A defect that also affects a target host,
input validation or a security boundary remains actionable.

WSL and other compatibility environments have no separate support guarantee.
This decision neither certifies nor prohibits their use.

## Alternatives considered

- **Maintain native Windows parity now.** Rejected because each platform path
  needs maintained tests and operational coverage, beyond the cost of its code.
- **Add compatibility opportunistically.** Rejected because isolated fixes imply
  support without verifying the installation-to-execution path.
- **Ban all Windows-related data or remove every compatibility branch now.**
  Rejected because a host support policy is not a data restriction, and deleting
  working code requires its own justification and regression checks.

## Consequences

Designs can stay focused on the target hosts. Native Windows users have no
installation or runtime guarantee, and Windows-specific work remains deferred
without a promised release date. Existing compatibility code may remain; this
ADR does not require an operating-system rejection check or authorize broad
cleanup. macOS/Linux targeting does not promise every distribution, architecture
or optional integration works; their existing requirements still apply.

## When to revisit

Revisit when a concrete user or deployment requirement needs native Windows and
there is ownership for its implementation, CI coverage and ongoing maintenance.
Define the supported workflows and their end-to-end verification in a new ADR
before expanding the support promise.
