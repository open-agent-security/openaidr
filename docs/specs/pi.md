# Reading Pi sessions

**Status:** implemented
**Date:** 2026-11-17

## Context

Pi is a minimal terminal coding agent (`pi.dev`, command `pi`). Its npm
package is `@earendil-works/pi-coding-agent`, formerly
`@mariozechner/pi-coding-agent`. It has four default tools: `read`, `bash`,
`edit` and `write`. It can also enable `grep`, `find` and `ls`. Pi talks to
many providers, and it accepts any OpenAI-compatible endpoint through
`models.json`. Before this spec, OpenAIDR read no Pi session.

Pi writes one JSONL file per session. Pi documents the format in
`docs/session-format.md` of its package. This spec was checked against
version 0.73.1 and a real session file that it wrote.

Later versions add to the same format, still version 3. Pi 0.99.0 added MCP
servers and `codemode`, 1.1.0 a result's `durationMs`, and 1.5.0
`responseModel`. The design for these comes from Pi 1.5.2's package: its
`docs/session-format.md`, `docs/message-types.md` and `docs/mcp.md`, and the
code that writes the files. No session that Pi 0.99 or later wrote was read.

## Goals

- `openaidr sessions --agent-kind pi` lists each Pi session on the machine.
- Each tool call has a span, a status, a tool class and canonical arguments.
- An MCP call names its server as the configuration names it.
- The calls that a `codemode` script made are tool calls too.
- Each assistant response has its token usage.

## Non-goals

- Sign-in accounts. Pi keeps API keys in `auth.json`. A key is not an identity.
- Incremental reads. `collect_file` reads the whole file again.
- Shell commands that a person runs with `!` (`bashExecution`). These are not
  agent tool calls, and this reader does not emit them.
- Extension tools. Their names are not in the class table, so `tool_class` is
  `None`. Pi's own MCP and `codemode` tools are in the table.
- The `sessionDir` setting in `settings.json`. The reader follows the
  environment variables only.

## Decisions

| ADR | Decision |
|---|---|
| [0043](../adrs/0043-read-every-branch-of-a-pi-session-tree.md) | Read every branch of a Pi session tree, not only the active branch |
| [0044](../adrs/0044-a-pi-extension-records-the-calls-it-blocks.md) | A Pi extension records each call it blocks in an entry of its own, and the reader reads it |
| [0036](../adrs/0036-a-code-mode-cell-is-a-script-and-its-tools-are-their-own-calls.md) | The calls a script made are a turn of their own, keyed by the record that holds them. Pi's `codemode` follows it |

## Design

### Where sessions live

The agent directory is `PI_CODING_AGENT_DIR`, else `~/.pi/agent`. The session
directory is `PI_CODING_AGENT_SESSION_DIR`, else `<agent dir>/sessions`.
Sessions are `<session dir>/--<cwd>--/<timestamp>_<uuid>.jsonl`. The reader
walks the session directory with `os.walk(onerror=...)` and reads each
`.jsonl` file. `OPENAIDR_PI_ROOT` overrides the agent directory for the CLI.
A root given to `PiReader` is the agent directory, and its `sessions` holds
the sessions, whatever the environment says.

### The file

The first line is the header:
`{"type":"session","version":3,"id":"<uuid>","timestamp":"<iso>","cwd":"<dir>"}`.
The reader reads only version 3. It reports any other version as a failure and
does not read the file. Every later line is an entry with an 8-hex `id`, a
`parentId` and an ISO `timestamp`.

| Entry `type` | What the reader does |
|---|---|
| `message`, role `user` | A `user` turn. The first one is `initial_prompt`. |
| `message`, role `assistant` | An `assistant` turn. Its `toolCall` blocks are its tool calls. Its `usage` is a `ModelResponse`, keyed by `responseId`, else by the entry `id`. With `stopReason` `error` and an `errorMessage`, an `error` turn follows, keyed `<entry id>.error`. |
| `message`, role `toolResult` | The result of the call with the same `toolCallId`. Its `nestedCalls` are a turn of their own (see [A `codemode` script](#a-codemode-script)). |
| `message`, role `custom` | A `ContextItem` with source `injected` and its `customType` as its name. |
| `message`, role `system` | Its `skills` section is a `ContextItem` with source `skill_listing`, and its `mcp_servers` section one with source `mcp_instructions`. Pi's own sections are not read. |
| `compaction` | A `Compaction` with trigger `unknown` and `pre_tokens` from `tokensBefore`. Its `usage` is a `ModelResponse`. |
| `branch_summary`, `usage` | Their `usage` is a `ModelResponse`, keyed by the entry `id`. |
| `custom_message` | A `ContextItem` with source `injected` and the extension's `customType` as its name. |
| `session_info` | The session title. The last one wins. |
| `custom`, `customType` `openaidr.tool-call-blocked` | The call named by `data.toolCallId` was blocked. See [Status](#status). |
| Other types | Not read. |

### Identity

- The session id is `pi:<header id>`.
- The turn key is the entry `id`. Pi never rewrites an entry, so the key is stable.
- The span is `span_id(session id, entry id, index of the toolCall block)`.

### Status

| Evidence | Status |
|---|---|
| Two or more results with the same call id | `unknown` |
| A block entry, and no result or an error result | `rejected`, `denial_kind` `extension-blocked` |
| No result | `pending` |
| One result, `isError: false` | `ok` |
| One result, `isError: true`, text `Tool execution was blocked` | `rejected`, `denial_kind` `extension-blocked` |
| One result, `isError: true`, text `Operation aborted` or `aborted`, or a last paragraph `Command aborted` | `interrupted` |
| One result, `isError: true`, anything else | `error` |

The rows apply in this order. Pi writes an extension's block as an error
result, and the result text is the extension's reason. Nothing in that result
shows that it is a block. So an extension that blocks a call records it with
`pi.appendEntry("openaidr.tool-call-blocked", {"toolCallId": "<id>"})`. The
reader joins the entry to its call through the `parentId` chain, as it joins a
result. A result that says the call succeeded outweighs a block entry. Without
the entry, a blocked call reads as `error`, unless the extension gave no
reason. Pi then writes `Tool execution was blocked`, and the call is
`rejected`.

`exit_code` is for `bash` and `powershell` only. It is `0` when `isError` is
false, because Pi makes a non-zero exit an error. Otherwise it is the number in
the last line `Command exited with code N`, and `None` without that line.

`duration_ms` is the result's `durationMs`, Pi's own measure of the tool's run
(1.1.0 and later). Without it, `duration_ms` is the result's `timestamp` minus
the assistant message's `timestamp`. Both are Unix milliseconds that Pi
writes.

`truncated` is true when the result's `details` names a `truncation` or a
`fullOutputPath`.

### Tool classes and canonical arguments

| Tool | Class | Canonical arguments |
|---|---|---|
| `bash`, `powershell` | `shell` | `command` |
| `read` | `file_read` | `path` |
| `grep`, `find`, `ls` | `file_search` | `path` |
| `write` | `file_write` | `path`, `content` |
| `edit` | `file_edit` | `path`, `edits`, `new_text` |
| `codemode` | `script` | |
| `tool_search`, `list_mcp_resources`, `list_mcp_resource_templates`, `read_mcp_resource` | `other` | |
| `mcp__<server>__<tool>` | `mcp` | |

`edit` takes `edits: [{oldText, newText}]`. Pi also accepts a legacy top-level
`oldText` and `newText`, and appends them last. The reader does the same. Pi
also repairs `edits` sent as a JSON string or as one bare object before the
call runs. The recorded arguments are the model's, so the reader reads both.

### MCP servers

Pi names an MCP tool `mcp__<server>__<tool>`. It writes each character of the
server's name that is not a letter, digit or `_` as `_`. The configured name
`dev-radius` becomes `dev_radius`.

1. The reader reads the server names under `mcpServers` in
   `<agent dir>/mcp.json` and in `<cwd>/.pi/mcp.json`.
2. A configured name whose written form, between `mcp__` and `__`, starts the
   tool name splits it. `mcp_server` is the configured name.
3. Two configured names that fit leave the call unsplit, with no server.
4. No configured name that fits: the call splits at the first `__`, as for
   Claude Code. `mcp_server` is the written name.

### A `codemode` script

`codemode` runs JavaScript that calls other tools. Pi records those calls on
the script's own result as `nestedCalls.calls`: `{id, name, arguments?,
argumentsBytes?, status, durationMs?, error?}`. They are a turn, keyed by the
result's entry `id`, role `assistant`, with one call each. The span of a nested
call is `span_id(session id, result entry id, index in calls)`.

| `status` | Status |
|---|---|
| `ok` | `ok` |
| `error` | `error`, with `error_text` from `error` |
| `unfinished` | `unknown` |

A nested call has no result body, because Pi records none. When Pi leaves out
the arguments for their size, it records `argumentsBytes`. The call is then
`truncated`, with empty arguments.

### Usage

`pi-ai` stores `input` without the cache buckets for every provider. The four
counts map to `input_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens` and `output_tokens` directly. `reasoning` is the
part of `output` spent thinking, and maps to `reasoning_tokens`.
`cacheWrite1h` is the part of `cacheWrite` kept for an hour, and the rest is
the 5-minute part.

`responseId` is optional. Without it, the entry `id` keys the response. A
response's model is `responseModel`, the model the provider says answered, else
`model`.

## Failure behavior

- A line that is not a JSON object withholds the session. The failure names the
  line number, never its content.
- A file without a session header is not read.
- A last line without a newline is a record that Pi still writes. The reader
  ignores it until the next read.
- Two files with the same session id: the reader keeps the first and reports the
  second.
- An `mcp.json` that cannot be read is a failure that names the file, never its
  content. MCP calls then keep their written server names.

## Verification

`tests/test_pi.py` reads a fixture that has the shape of a real Pi 0.73.1
session. It also tests branches, statuses, edits, compaction and malformed
lines. For Pi 0.99 and later it tests the MCP split, a `codemode` script's
nested calls, the error turn, usage without `responseId`, exit codes, Pi's own
block and abort words, `durationMs`, truncation, the session directory and the
context sections. It also tests a call id that a provider reuses in each
response.
