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
was signed in as. No transcript records it. Claude Code's session files carry no
account, Devin's store has no user column, and Codex's rollouts carry none.
Each agent keeps its sign-in record in its own configuration, in its own shape:

| Kind | Record | Account id | Name | Plan |
|---|---|---|---|---|
| `claude-code` | `oauthAccount` in `~/.claude.json`, or inside `CLAUDE_CONFIG_DIR`; a legacy `.config.json` wins | `accountUuid` | `displayName`, `emailAddress` | `billingType` |
| `codex` | the identity token in `~/.codex/auth.json` (or `CODEX_HOME`), when signed in with ChatGPT | `chatgpt_user_id`, else `sub` | `name`, `email` | `chatgpt_plan_type` |
| `devin` | none: `~/.config/devin/config.json` names only `org_id` | — | — | — |

Without a shared reader every consumer learns each of these files itself, and
learns them for Claude Code first and the others never.

## Decision

1. **`openaidr.accounts.signed_in(kind)` returns an `Account` or None.** The
   fields are `kind`, `account_id`, `display_name`, `email` and `plan`. They are
   read live, now, from the kind's own record. The caller can pass the agent's
   configuration directory when it is not the default.
2. **One read of the record per call.** Fields used together come from one
   snapshot, so a sign-in that rewrites the file mid-read cannot pair one
   account's id with another's name.
3. **`display_name` and `email` are `LOCAL_ONLY`.** They name a person. The
   account id tells people apart without naming them; what a consumer sends
   anywhere is its own decision.
4. **Codex is read from the identity token's payload alone.** The same file
   holds the access token, the refresh token and any API key, and none of them
   is read into what this returns. The signature is not checked: this reports
   what Codex holds on this machine, not a proof of who the person is. Signed
   in with an API key there is no person, and the answer is None.
5. **A kind with no local record answers None, never a guess.** Devin today.
   An unknown kind answers None too.
6. **Reading a sign-in record is not reading sessions.** `codex` is read here
   ahead of a Codex session reader. This adds no session coverage: the package
   still reads sessions for `claude-code` and `devin` only.

## Alternatives considered

- **Leave each consumer to read the files itself.** Rejected: the shapes are
  per kind and only this package knows them, so each consumer would learn
  Claude Code's and stop there, which is how it went.
- **Carry the account on `Session`.** Rejected: no transcript records it, so a
  session-level field would either be empty or be today's account stamped on
  past work, which names the wrong person on a shared machine.
- **Verify Codex's token, or read the access token for a fuller answer.**
  Rejected: verification proves nothing a local reader needs, and the access
  token is a credential this package has no reason to hold.
- **Treat this as identity resolution.** It is not: nothing is matched across
  kinds or looked up elsewhere. It reports the record the agent wrote, as a
  session is.

## Consequences

- A consumer attributes Codex work to a person the moment Codex sessions are
  read, with no change of its own.
- Devin work names no person until Devin keeps a local record of one.
- Each kind's file format is a dependency on that agent. A changed shape reads
  as no account, never as an error.
