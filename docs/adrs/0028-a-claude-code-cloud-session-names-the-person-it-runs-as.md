---
id: 0028
title: A Claude Code cloud session is signed in as the account it names in its environment
status: proposed
date: 2026-10-13
supersedes: null
superseded-by: null
---

## Context

ADR-0027 clause 3 reads Claude Code's sign-in from `oauthAccount` in its
configuration file, and clause 6 says how one process authenticated, set in its
environment, is not seen.

A Claude Code cloud session runs in a container that several people can start
sessions in from one environment. A read-only probe of one such session on
2026-10-13 found:

- `CLAUDE_CODE_REMOTE=true`.
- `oauthAccount` in `/root/.claude.json` with `accountUuid`, `emailAddress` and
  `organizationUuid`, and no `displayName`.
- `CLAUDE_CODE_ACCOUNT_UUID` and `CLAUDE_CODE_USER_EMAIL` in the environment of
  the session's processes.

The probe reported the variables by name and did not print their values.

The configuration file belongs to the environment, so it can record whoever
set the environment up. The variables are set for the session. A consumer that
reads the file alone can name the wrong person for a session in a shared
environment.

## Decision

1. **In a Claude Code cloud session, the session's variables are the
   account.** When this package's environment has `CLAUDE_CODE_REMOTE=true` and
   a non-blank `CLAUDE_CODE_ACCOUNT_UUID`, `signed_in("claude-code")` returns
   that id as `account_id`, and `CLAUDE_CODE_USER_EMAIL` as `email` (None when
   blank or unset).
2. **The file adds only what it says about the same account.** `displayName`
   and `billingType` come from `oauthAccount` only when its `accountUuid` is
   the session's. Another account's name and plan are not taken.
3. **Everywhere else, ADR-0027 stands.** With `CLAUDE_CODE_REMOTE` unset or
   any value but `true`, or no account id, the file decides, as on a laptop.
   `sign_in_records` is unchanged: the variables do not change during a
   process, so they add no file to watch.
4. **The variables are read from this package's environment,** as clause 3
   reads `CLAUDE_CONFIG_DIR`. A consumer started inside the session, such as
   by a hook Claude Code runs, sees them. One started outside it does not, and
   gets the file's answer.

This extends ADR-0027 clause 3 with a second source for cloud sessions, and
narrows clause 6: a cloud session's account, which the session states in its
environment, is seen. Everything else in ADR-0027 stands.

## Alternatives considered

- **Keep reading the file alone.** Rejected. In an environment several people
  use, the file can name someone other than the person who started the
  session, which is the wrong-person answer this package treats as worse than
  a gap.
- **Prefer the file, falling back to the variables.** Rejected for the same
  reason: a file that names someone else is still an answer, so the fallback
  would never run where it matters.
- **Read the variables whenever they are set, cloud or not.** Rejected. Only a
  cloud session is known to set them. Reading them elsewhere would let any
  process's environment override the agent's own record on a laptop.
- **Take the file's name and plan whatever account it names.** Rejected. They
  are another account's, and would name the session after someone else.

## Consequences

- A cloud session's work names the person who started it, even in a shared
  environment. Its name comes from the file only when the file is theirs.
  Otherwise a consumer has the email and no name.
- What the variables mean is read from their names and from where they
  appeared. The probe did not print their values. A consumer should check one
  real session's answer before relying on it.
- If Claude Code stops setting the variables, cloud sessions fall back to the
  file, as before this decision.
