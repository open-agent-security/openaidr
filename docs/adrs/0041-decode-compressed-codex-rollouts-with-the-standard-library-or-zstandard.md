---
id: 0041
title: Decode compressed Codex rollouts with the standard library or zstandard
status: proposed
date: 2026-11-02
supersedes: null
superseded-by: null
amends: null
amended-by: null
---

## Context

Codex can compress a rollout whose last write is 7 or more days old, as
`rollout-*.jsonl.zst` in one zstd frame. The reader reported each such file
as a failure and read none of them. A real machine with Codex CLI 0.166.0 had
621 compressed rollouts over seven months. Its sessions were absent, and a
consumer that needs a complete read of a window saw hundreds of failures for
files outside that window.

Python reads zstd from the standard library only from 3.14
(`compression.zstd`). This package supports Python 3.11 and later. Until now
it had one runtime dependency, `adr-sensor`.

## Decision

Read compressed rollouts through the same projection as plain ones. Decode
with `compression.zstd` where it imports, which is Python 3.14 and later
unless the build omits it. Otherwise decode with `zstandard`, a dependency
pinned exactly and installed only below 3.14 by an environment marker. The spec has the file format, the window rule
and the failure reasons:
[Compressed rollouts](../specs/codex.md#compressed-rollouts).

## Alternatives considered

- **Report compressed rollouts and skip them.** Rejected. The sessions stay
  absent, and every compressed file is a failure on every read, whatever the
  window.
- **Run the `zstd` command.** Rejected. Many machines do not have it, and a
  child process per file adds a failure mode that a library does not have.
- **A decoder written in Python.** Rejected. It is slow, and a decoder is code
  this package would then own and maintain.
- **`zstandard` on every Python version.** Rejected. Python 3.14 has a decoder
  in the standard library, so the dependency is weight there.
- **Require Python 3.14.** Rejected. It drops three supported versions for one
  file format.

## Consequences

Compressed sessions are read on every supported version, and a thread keeps
its identity when Codex compresses it. Below Python 3.14 this package has a
second runtime dependency. It is a compiled extension under BSD-3-Clause, with
wheels for the common platforms. A `zstandard` version change is a deliberate
change of the pin. The two decoders share one code path, so a difference
between them fails a test on one Python version only. CI runs Python 3.11
only, so CI does not run the standard-library path.

## When to revisit

When the oldest supported Python is 3.14: remove `zstandard`. When Codex
writes rollouts with another codec, or with a dictionary.
