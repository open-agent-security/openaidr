---
id: 0027
title: Who an agent is signed in as is read from its own sign-in record, per kind
status: proposed
date: 2026-10-11
supersedes: null
superseded-by: null
---

## Context

A consumer that attributes work or spend to a person needs to know who an agent
was signed in as. No transcript records it: Claude Code's session files carry no
account, and Devin's store has no user column. Each agent keeps its sign-in in
its own place and shape:

| Kind | Where the sign-in is | Account id | Name | Plan |
|---|---|---|---|---|
| `claude-code` | `oauthAccount` in Claude Code's configuration file (clause 3) | `accountUuid` | `displayName`, `emailAddress` | `billingType` |
| `devin` | nowhere on disk. `config.json` names only `org_id`, and `credentials.toml` holds the API key alone. Devin's own `devin auth status` asks Devin's server with that key and prints the user | `User ID` (`user-…`) | `Name`, `Email` | `Plan` |

Without a shared reader, every consumer learns each of these places itself, and
learns Claude Code's first and the others never.

## Decision

1. **Scope: the kinds whose sessions this package reads, `claude-code` and
   `devin`.** A kind's sign-in is read together with its sessions, because a
   consumer can only attribute work it can see. Every other kind (`codex`,
   `cursor`) and any unknown kind answers None from `signed_in` and names no
   files in `sign_in_records`.
2. **`openaidr.accounts.signed_in(kind)` returns an `Account` or None.** Its
   fields are `kind`, `account_id`, `display_name`, `email` and `plan`, read
   live with one read of the record per call. A sign-in that rewrites the file
   mid-read therefore cannot pair one account's id with another's name.
   `display_name` and `email` are `LOCAL_ONLY`. It never raises: a missing,
   unreadable or malformed record is no account.
3. **Claude Code is read from `oauthAccount`, where Claude Code itself looks.**
   This was checked against Claude Code 2.1.283's own locator,
   `join(CLAUDE_CONFIG_DIR || homedir(), ".claude.json")`.
   - When a configuration directory is named, by `CLAUDE_CONFIG_DIR` or by
     the caller, the file is `.claude.json` inside it. That holds even when it
     names `~/.claude`.
   - With none named, the file is `~/.claude.json`.
   - A legacy `.config.json` in the directory wins over either.
   - Whether a directory is named is the test, never whether its path equals
     the default.
4. **Devin is never run here. `sign_in_query("devin")` says how to ask it.**
   Sensing in this package is read-only, and only Devin's own `devin auth
   status` can say who is signed in, by calling Devin's server. So
   `signed_in("devin")` is None.
   - `sign_in_query` returns the command: Devin's binary and `auth status`.
     The binary is the one the CLI installs (`$XDG_DATA_HOME/devin/cli/
     _versions/current/bin/devin`), else `devin` on `PATH`, else Devin
     Desktop's bundled copy.
   - The query's `read` turns what the command printed into an `Account`
     from its `User` section (`User ID`, `Name`, `Email`) and its `Account`
     section (`Plan`). Output with no `User ID` (signed out, offline, or a
     layout this package does not know) reads as None.
   - A consumer that chooses to ask runs the command, bounded by a timeout
     of its own. This package never opens `credentials.toml`.
5. **`sign_in_records(kind)` names every file whose change can change the
   answer**, whether or not each exists yet. A consumer asks again when any
   of them is created, changed or removed. For Devin that keeps one call to
   its server per sign-in, not one per pass. Only the files' existence and
   times are a consumer's business, because some hold the credential.
   - `claude-code`: the legacy `.config.json` and `.claude.json`, found as
     clause 3 finds them. The legacy file appearing changes the answer
     without `.claude.json` changing.
   - `devin`: `credentials.toml` under `$XDG_DATA_HOME/devin`.
6. **The answer is the agent's record of its sign-in, never a check of the
   credential.** A consumer reads it as "the account this agent last signed
   in as", not "proved signed in now".
   - The credential is never opened. The macOS Keychain, Claude Code's
     `.credentials.json` and Devin's `credentials.toml` hold the secret. A
     record left after its credential was removed or switched therefore
     still names its account.
   - How one process authenticated is not seen: for example an API key in
     its environment. That is per process, and no file on the machine
     records it.
7. **Reading a sign-in adds no session coverage.**

## Alternatives considered

- **Leave each consumer to read the records itself.** Rejected. The shapes are
  per kind and only this package knows them. Each consumer would learn Claude
  Code's and stop there, which is how it went.
- **Carry the account on `Session`.** Rejected. No transcript records it, so a
  session-level field would either be empty or stamp today's account on past
  work, which names the wrong person on a shared machine.
- **Read Codex's sign-in now, ahead of its sessions.** Rejected for this
  change. No consumer can attribute Codex work until Codex sessions are read,
  so a Codex reader here has no use, and its edge cases have no real sessions
  to be tested against. Those edge cases are credential stores, auth modes and
  token decoding, and most of this change's review rounds were spent on them.
  The reader is kept on the `codex-sign-in-reader` branch, with every review
  fix, for the change that reads Codex sessions.
- **Read Devin's identity from disk.** Rejected. Its CLI keeps none. Devin
  Desktop caches the server's answer in an undocumented binary format, in the
  same record as the API key.
- **Ask Devin's server directly.** Rejected. That means holding Devin's
  credential. `auth status` is Devin's own supported way to say who is signed
  in.
- **Run `devin auth status` inside `signed_in`.** Rejected. Sensing here is
  read-only and ships nothing anywhere, and the command calls Devin's server.
  Whether to make that call is the consumer's decision. The per-kind knowledge
  of where the binary lives and how its answer reads stays here.
- **Check the record against the credential**, for example by confirming that
  Claude Code's Keychain item or `.credentials.json` exists. Rejected. On macOS
  that is a Keychain query, and on every platform it touches the store that
  holds the secret, which read-only sensing refuses. A platform-dependent half
  check would make one record answer differently on two machines.
- **Treat this as identity resolution.** It is not. Nothing is matched across
  kinds or looked up elsewhere. It reports the record the agent wrote, as a
  session is.

## Consequences

- A consumer names the person behind Claude Code and Devin work. Codex and
  Cursor work names no one until their sessions and sign-ins are read.
- Devin work names its person only for a consumer that asks Devin, and then
  only when Devin could reach its server since the last sign-in. A consumer
  keeps the last answer it got.
- A record that outlives its credential still names its account.
- `devin auth status` prints text, with no machine-readable form. A changed
  layout reads as no account, never as an error.
- Each kind's file format is a dependency on that agent. A changed shape reads
  as no account, never as an error.
