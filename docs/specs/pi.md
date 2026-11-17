# OpenAIDR — Pi

**Status:** implemented
**Date:** 2026-11-17

*A fifth agent kind, `pi`, with an OpenAIDR-owned reader (`readers/pi.py`).
Extends [Session collection](session-collection.md) and [Token
usage](token-usage.md). Pi is the coding agent published as
`@earendil-works/pi-coding-agent` (formerly `@mariozechner/pi-coding-agent`).*

## Context

Pi keeps every session in one append-only JSONL file. Each line after the
header is an entry with its own `id` and `parentId`, and the entries form a
tree. Usage is on each assistant message. `adr-sensor` does not parse Pi, so
OpenAIDR reads the files itself, on the Claude Code reader's design.

## Goals

- `openaidr sessions --agent-kind pi` lists each Pi session with its turns,
  tool calls, outcomes, usage and compactions.
- A tool call has a span that does not move as the file grows.
- Each call has a tool class and canonical arguments from Pi's own tool
  table.
- An MCP call names its server as the configuration names it.

## Non-goals

- Who Pi is signed in as. Pi keeps provider keys, not a person (see [Open
  facts](#open-facts)).
- Sessions that Pi keeps in memory only (`--no-session`, the SDK without a
  session manager). Nothing is written.
- Windows (ADR-0013).

## Decisions

| ADR | Decision |
|---|---|
| [0043](../adrs/0043-read-pi-from-its-session-files-every-branch-included.md) | Read every entry of the file, an abandoned branch included, and make a `codemode` script's calls a turn of their own |

## Evidence

- **Source.** The npm package `@earendil-works/pi-coding-agent` 1.5.2 and its
  dependencies, unpacked and not run. Its `docs/session-format.md`,
  `docs/message-types.md`, `docs/mcp.md` and `docs/cli.md`, and the code that
  writes the files: `dist/core/session-manager.js`,
  `pi-agent-core/dist/agent-loop.js`, `dist/core/nested-tool-calls.js` and the
  tool modules in `dist/core/tools/`. Everything in this spec comes from
  these.
- **Not checked: a recorded session.** The Pi binary was not run against a
  model. Real error wording from a provider, and a real `nestedCalls` record,
  are unconfirmed.

## Design

### Where the files are

| | |
|---|---|
| Session directory | `$PI_CODING_AGENT_SESSION_DIR`, else `<agent dir>/sessions`. `default_readers(pi_root=…)` overrides it |
| Agent directory | `$PI_CODING_AGENT_DIR`, else `~/.pi/agent`. It holds `mcp.json` |
| A file | `<session dir>/--<cwd>--/<ISO time>_<session id>.jsonl`, where `<cwd>` has its leading separator removed and each `/`, `\` and `:` written as `-`. A session directory that a person sets with `--session-dir` holds the files directly. The reader reads `.jsonl` files at both depths |
| Writing | Pi writes nothing until the first assistant message. It then writes the buffered entries and appends one line for each new entry |

### The file

The first line is the header: `{"type": "session", "version": 3, "id",
"timestamp", "cwd", "parentSession"?}`. Every later line is an entry with
`type`, `id` (8 hex characters), `parentId` and an ISO `timestamp`.

| Entry `type` | What the reader takes |
|---|---|
| `message`, role `user` | A user turn. The first one is `initial_prompt` |
| `message`, role `assistant` | A turn: its text, a call for each `toolCall` part, and one model response from `usage` |
| `message`, role `toolResult` | No turn. It completes the call its `toolCallId` names. Its `nestedCalls` become a turn of their own (below) |
| `message`, role `bashExecution` | A user turn: a command the person ran with `!`, and its output unless `excludeFromContext` is set |
| `message`, role `custom` | A context item, source `injected` |
| `message`, role `system` | Its `skills` section is a context item, source `skill_listing`. Its `mcp_servers` section is one, source `mcp_instructions`. Other sections are Pi's own and are not read |
| `custom_message` | A context item, source `injected`, named by `customType` |
| `compaction` | A compaction, trigger `unknown`, with `tokensBefore`. Its `usage` is a model response |
| `branch_summary`, `usage` | Their `usage` is a model response |
| `session_info` | `title`, the latest `name` |
| `model_change`, `thinking_level_change`, `label`, `custom`, `context_edit` | Nothing |

### Identity

| Level | Identity |
|---|---|
| Session | `pi:<header id>` |
| Turn | The entry's `id`. A version 1 entry has no `id` and is keyed `L<line>`, its line in the file |
| Span | `span_id(session_id, turn key, index of the toolCall part in the message)` |
| A nested call | `span_id(session_id, the tool result's entry id, index in nestedCalls.calls)` |

- **The file only grows.** Pi appends each entry. Pi rewrites a file only
  when it migrates an older version. The reader then reads the file again
  from the start, because the header changed.
- **Every branch is read.** `/tree` leaves the entries of the old branch in
  the file. Those calls ran, so they are in the session, in file order.
- **Outcomes join on `toolCallId`.** A result that names no known call is not
  read. A second result for the same call is not read.

### Tool calls

#### Status

| Result | Status | Notes |
|---|---|---|
| None yet | `pending` | |
| `isError: false` | `ok` | |
| `isError: true`, text `Tool execution was blocked` | `rejected` | `denial_kind` `permission-rule`. Pi writes this when an extension blocks a call and gives no reason |
| `isError: true`, text `Operation aborted` or `aborted`, or a last paragraph `Command aborted` | `interrupted` | |
| `isError: true`, anything else | `error` | An extension that blocks with its own reason reads as `error`. The text does not say that it was a block |

- **Exit code**: `bash` and `powershell` only. `0` when `isError` is false,
  since Pi makes a non-zero exit an error. Otherwise the number in the last
  line `Command exited with code N`.
- **Duration**: the result's `durationMs`.
- **Working directory**: the session's `cwd`, for a shell call. Pi runs every
  command there.
- **Truncated**: the result's `details` names a `truncation` or a
  `fullOutputPath`.

#### Tool names

| Names | Class | Canonical arguments |
|---|---|---|
| `bash`, `powershell` | `shell` | `command` |
| `read` | `file_read` | `path` |
| `grep`, `find`, `ls` | `file_search` | `path` |
| `write` | `file_write` | `path`, `content` |
| `edit` | `file_edit` | `path`, `new_text` (the new texts, joined), and `edits`: one `{path, kind: update, old_text, new_text}` for each `{oldText, newText}`. Pi takes `edits` as a list, a JSON string or one object, and the reader reads all three |
| `codemode` | `script` | |
| `tool_search`, `list_mcp_resources`, `list_mcp_resource_templates`, `read_mcp_resource` | `other` | |
| `mcp__<server>__<tool>` | `mcp` | |

#### A `codemode` script

`codemode` runs JavaScript that calls other tools. Pi records those calls on
the script's own result as `nestedCalls.calls`: `{id, name, arguments?,
argumentsBytes?, status, durationMs?, error?}`. The reader makes them a turn,
keyed by the result's entry id, role `assistant`, with one call each, as Codex's
Code Mode cells are read (ADR-0036).

| `status` | Status |
|---|---|
| `ok` | `ok` |
| `error` | `error`, with `error_text` from `error` |
| `unfinished` | `unknown` |

A nested call has no result body; Pi does not record one. When Pi left out the
arguments for their size, it records `argumentsBytes`, and the call is
`truncated` with empty arguments.

#### MCP servers

Pi names an MCP tool `mcp__<server>__<tool>`, and writes each character of
the server's name that is not a letter, digit or `_` as `_`. The configured
name `dev-radius` becomes `dev_radius`.

1. The reader reads the server names in `<agent dir>/mcp.json` and in
   `<cwd>/.pi/mcp.json`, under `mcpServers`.
2. A configured name whose written form, between `mcp__` and `__`, starts the
   tool name splits it. `mcp_server` is the configured name.
3. Two configured names that fit leave the call unsplit, with no server.
4. No configured name that fits: the call splits at the first `__`, as for
   Claude Code. `mcp_server` is the written name.

A configuration file that cannot be read is a `ReaderFailure`. Names then stay
written.

### Session fields

| Field | Source |
|---|---|
| `started_at` | The header's `timestamp` |
| `last_activity_at` | The newest entry `timestamp` |
| `model` | The last response's model |
| `working_directory` | The header's `cwd` |
| `title` | The latest `session_info.name` |
| `initial_prompt` | The first user message |
| `user`, `machine` | The local user and host |
| `compactions_recorded` | True |
| `subagents_recorded` | False. Pi has no built-in sub-agent |
| `entrypoint`, `agent_version`, `git_branch` | None. Pi does not record them |

### Model responses

One for each assistant message with `usage`, and one for each `usage`,
`compaction` and `branch_summary` entry with `usage`.

| Field | Source |
|---|---|
| `response_id` | The message's `responseId`, else its entry id |
| `model` | `responseModel`, else `model`. None on a compaction or a branch summary |
| `occurred_at` | The entry's `timestamp` |
| `final` | True. Pi writes no message while it streams |
| Usage | `input` is uncached input. `cacheRead`, `cacheWrite` and `output` as stated. `reasoning` is part of `output`. `cacheWrite1h` is part of `cacheWrite`, and the 5-minute part is the rest |

A message with `stopReason: "error"` and an `errorMessage` is also an `error`
turn, keyed `<entry id>.error`, with that text.

### Collection

- **Cold**: each `.jsonl` file is read. A file whose last change is older than
  the window is not opened, since no line in it can be newer.
- **Incremental**: the reader keeps each file's read offset and header. A
  changed file is read on from the offset. A line with no newline yet is read
  when it is complete. A session is returned only when it differs from the
  last one returned for it.
- **Locations**: the session directory.

## Failure behavior

| Condition | Result |
|---|---|
| No session directory | No sessions, and no failure |
| A directory that cannot be listed | A failure, and the rest is read |
| A line that is not a JSON object | A failure. The other lines are read |
| A file with no header, or a header with no id | A failure. No session |
| An `mcp.json` that cannot be read | A failure. Servers keep their written names |

## Verification

`tests/test_pi.py` builds session files in Pi 1.5.2's shape
(`tests/fixtures/pi_session.py`) and checks each table above: identity,
statuses, exit codes, the nested calls, the MCP split with and without
configuration, usage, context items, the error turn, a growing file, a
rewritten file, a broken line and the window.

## Open facts

| | Fact | Effect |
|---|---|---|
| P1 | The reader did not read a file that a running Pi wrote | The shapes come from Pi's source and docs. A provider's own error wording is unconfirmed |
| P2 | A fork (`/fork`, `--fork`, `/clone`) copies the path's entries into a new file, with their ids | A forked session repeats its parent's calls under its own session id |
| P3 | An extension that blocks a call can give any reason | Only Pi's default reason reads as `rejected` |
| P4 | Pi records no entrypoint: the terminal UI, print mode, JSON mode and RPC mode write the same file | A consumer cannot tell whether a person was present |
| P5 | `sessionDir` in `settings.json` can move the session directory, relative to a project | The reader follows the environment variables only |
| P6 | Pi keeps provider credentials (`auth.json`), not a person | No account is read |
