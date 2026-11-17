---
id: 0043
title: Read every branch of a Pi session tree
status: accepted
date: 2026-11-17
supersedes: null
superseded-by: null
---

## Context

A Pi session file is a tree. Each entry has a `parentId`. A person can go back
to an earlier entry with `/tree` and continue from there. The old branch stays
in the file. Pi builds the model's context from one path, from the current leaf
to the root. Detail: [`docs/specs/pi.md`](../specs/pi.md#the-file).

## Decision

The Pi reader reads every `message` entry in the file, in file order, on every
branch. A tool call on a branch that the person left still ran.

## Alternatives considered

- **Read only the active branch, as Pi's own `buildSessionContext` does**:
  rejected because the calls on an abandoned branch ran on the machine. A
  consumer that asks what an agent did must see them.
- **Emit one session for each branch**: rejected because the branches share
  their first entries. The same call would then have two spans.

## Consequences

The turn order is the file order, not one conversation path. A consumer that
rebuilds the conversation the model saw must follow `parentId` itself. The model
does not carry `parentId` today.

## When to revisit

Revisit if the model gains a parent link for turns, or if a consumer needs the
active path.
