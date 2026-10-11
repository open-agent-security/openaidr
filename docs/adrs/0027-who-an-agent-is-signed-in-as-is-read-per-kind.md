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
Each agent keeps its sign-in record in its own configuration, in its own shape,
except Devin, which keeps none on disk and reports one through its own command:

| Kind | Record | Account id | Name | Plan |
|---|---|---|---|---|
| `claude-code` | `oauthAccount` in `~/.claude.json`, or inside `CLAUDE_CONFIG_DIR`; a legacy `.config.json` wins | `accountUuid` | `displayName`, `emailAddress` | `billingType` |
| `codex` | the identity token in `~/.codex/auth.json` (or `CODEX_HOME`), when signed in with ChatGPT and `cli_auth_credentials_store` keeps the sign-in in that file | `chatgpt_user_id`, else `sub` | `name`, `email` | `chatgpt_plan_type` |
| `devin` | none on disk: `config.json` names only `org_id`, and `credentials.toml` holds the API key alone. `devin auth status` asks Devin's server with that key and prints the user | `User ID` (`user-…`) | `Name`, `Email` | `Plan` |

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
5. **Codex is read only where it keeps the sign-in.** Its `config.toml`
   `cli_auth_credentials_store` chooses `file` (the default), `keyring`,
   `auto` (the keyring when the OS has one) or `ephemeral`. Under any but
   `file`, `auth.json` is not the store and may hold an earlier sign-in, so
   the answer is None, never a possibly stale account. The keyring is never
   read: it holds the credential.
6. **Devin is never run here; `sign_in_query("devin")` says how to ask it.**
   Sensing in this package is read-only, and only Devin's own `devin auth
   status` can say who is signed in, by asking Devin's server with Devin's
   credential. So `signed_in("devin")` is None. `sign_in_query` returns the
   command: the binary where the CLI installs itself
   (`$XDG_DATA_HOME/devin/cli`), else on `PATH`, else in Devin Desktop's
   bundle, with `auth status`. Its `read` turns what the command printed into
   an `Account`. A consumer that chooses to ask runs it, bounded by its own
   timeout. This package never opens `credentials.toml`.
7. **`sign_in_record(kind)` names the file a new sign-in rewrites.** A
   consumer asks again when it changes. For Devin, that keeps one call to its
   server per sign-in rather than one per pass. Only the file's existence and
   time are a consumer's business; Devin's holds the credential.
8. **A kind that cannot say answers None, never a guess.** An unknown kind
   answers None too.
9. **Reading a sign-in record is not reading sessions.** `codex` is read here
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
- **Read Devin's identity from disk.** Rejected. Its CLI keeps none. Devin
  Desktop caches the server's answer in an undocumented binary format, in the
  same record as the API key. Decoding the API key's own token would mean
  reading the credential.
- **Ask Devin's server directly.** Rejected. That means holding Devin's
  credential. `auth status` is Devin's own supported way to say who is signed
  in.
- **Run `devin auth status` inside `signed_in`.** Rejected. Sensing here is
  read-only and ships nothing anywhere, and the command asks Devin's server.
  Whether to make that call is the consumer's decision. The per-kind
  knowledge of where the binary lives and how its answer reads stays here.
- **Read Codex's keyring, or `auth.json` whatever the store.** Rejected. The
  keyring holds the credential. A file the store does not use can name the
  account signed in before, and a wrong account is worse than none.
- **Treat this as identity resolution.** It is not: nothing is matched across
  kinds or looked up elsewhere. It reports the record the agent wrote, as a
  session is.

## Consequences

- A consumer attributes Codex work to a person the moment Codex sessions are
  read, with no change of its own.
- Devin work names its person only for a consumer that asks Devin, and then
  whenever Devin could reach its server since the last sign-in. A consumer
  keeps the last answer it got.
- A Codex user who keeps the sign-in in the keyring names no person.
- `devin auth status` prints text, with no machine-readable form. A changed
  layout reads as no account, never as an error.
- Each kind's file format is a dependency on that agent. A changed shape reads
  as no account, never as an error.
