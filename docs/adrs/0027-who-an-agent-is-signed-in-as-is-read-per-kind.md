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
| `codex` | the identity token in `~/.codex/auth.json` (or `CODEX_HOME`), when signed in with ChatGPT and `cli_auth_credentials_store` keeps the sign-in in that file | `chatgpt_user_id`, else `user_id`, as Codex reads its own token; neither is no account | `name`, `email` (else the profile claim's) | `chatgpt_plan_type` |
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
   in with an API key there is no person, and the answer is None. The payload
   is decoded exactly as Codex's own decoder (`URL_SAFE_NO_PAD`'s default
   config) would: no character outside the base64 alphabet, never dropped by
   a lenient decoder that reads the rest anyway; no embedded padding, since
   base64url-no-pad never has any; and no non-canonical final symbol, whose
   unused bits a lenient decoder discards instead of rejecting, so two
   different tokens would read as the same account.
5. **Codex is read where its configured store keeps the sign-in.** This was
   checked against Codex's own auth storage. The boundary:
   - `cli_auth_credentials_store` in Codex's `config.toml` chooses the store.
     `file`, the default, and `auto` use `auth.json`. `auto` tries the
     keyring, then the file, and deletes the file on every keyring save, so
     a file that is there is the one in use.
   - Within `auth.json`, the tokens name a person only when Codex signs in
     with ChatGPT, as its `resolved_mode` decides. An explicit `auth_mode`
     wins, and only Codex's exact `chatgpt` and `chatgptAuthTokens` sign in
     with ChatGPT. Codex refuses a value its `AuthMode` does not define, so
     any other value, an unknown or misspelt one included, is no person. Without one, a key field that is set (an API key, a personal
     access token, a Bedrock key) means a key, and the answer is None. A
     key field is checked for presence, never read.
   - `keyring`, `ephemeral` and any other value keep the sign-in off disk,
     and a file left behind is an earlier one, so the answer is None. Only a
     missing `config.toml` leaves the default: Codex's own loader treats
     that as `Ok(None)` and falls back to it. An existing file it cannot
     read or parse is an error there, not a default, so the answer is None
     rather than a guess at the store.
   - The reader mirrors Codex's own code (`resolved_mode`, `AuthMode`,
     `parse_chatgpt_jwt_claims`) and its closed vocabularies. A value Codex
     would refuse reads as no sign-in, never as the nearest known one.
   - Not seen, deliberately: how any one Codex process was started. `codex
     exec` prefers a `CODEX_API_KEY` in its environment and the interactive
     Codex ignores one; `-c` overrides and profiles choose per run. That is
     per-process state no file on the machine records. Also not seen:
     managed configuration layers, and the rare `auto` case of an older
     keyring entry outranking a newer fallback file, which needs the
     keyring, which holds the credential.
6. **Devin is never run here; `sign_in_query("devin")` says how to ask it.**
   Sensing in this package is read-only, and only Devin's own `devin auth
   status` can say who is signed in, by asking Devin's server with Devin's
   credential. So `signed_in("devin")` is None. `sign_in_query` returns the
   command: the binary where the CLI installs itself
   (`$XDG_DATA_HOME/devin/cli`), else on `PATH`, else in Devin Desktop's
   bundle, with `auth status`. Its `read` turns what the command printed into
   an `Account`. A consumer that chooses to ask runs it, bounded by its own
   timeout. This package never opens `credentials.toml`.
7. **`sign_in_records(kind)` names every file whose change can change the
   answer**, whether or not each exists yet. A consumer asks again when any
   of them is created, changed or removed. For Devin, that keeps one call to
   its server per sign-in rather than one per pass.
   - Claude Code: the legacy `.config.json` and the file it falls back to,
     found as clause 2 finds them. The legacy file winning is itself a
     change the fallback file need not show.
   - Codex: `config.toml` and `auth.json`, whatever the store. The store
     setting can move the sign-in on or off `auth.json` without that file
     changing, in either direction, and a sign-in under `auto` can create
     the file.
   - Devin: `credentials.toml`.
   Only the files' existence and times are a consumer's business; some hold
   the credential.
8. **A kind that cannot say answers None, never a guess.** An unknown kind
   answers None too.
9. **The answer is the agent's record of its sign-in, never a check of the
   credential.** This holds for every kind. What is returned is what the
   agent's own record says, read where the agent itself keeps it, which for
   Claude Code is inside `CLAUDE_CONFIG_DIR` whenever it is set.
   - The credential is never opened: the macOS Keychain, Claude Code's
     `.credentials.json`, Codex's keyring and Devin's `credentials.toml`
     hold the secret. So a record left after the credential was removed or
     switched still names its account, and a consumer reads "the account
     this agent last signed in as", not "proved signed in now".
   - How one process authenticated is not seen either: an API key in its
     environment, or a `-c` override. That is per process, and no file on
     the machine records it.
   - What the record itself says about where the sign-in lives is honoured,
     because it is on disk: Codex's credential store (clause 5).
10. **Reading a sign-in record is not reading sessions.** `codex` is read here
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
- **Check the record against the credential**, for example by confirming
  that Claude Code's Keychain item or `.credentials.json` exists. Rejected.
  On macOS that is a Keychain query, and on every platform it touches the
  store that holds the secret, which read-only sensing refuses. A
  platform-dependent half check would make the same record answer
  differently on two machines.
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
- A record that outlives its credential still names its account. A consumer
  that needs proof of a live credential has to get it another way.
- `devin auth status` prints text, with no machine-readable form. A changed
  layout reads as no account, never as an error.
- Each kind's file format is a dependency on that agent. A changed shape reads
  as no account, never as an error.
