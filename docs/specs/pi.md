# Reading Pi sessions

**Status:** implemented
**Date:** 2026-11-17

## Context

Pi is a minimal terminal coding agent (`pi.dev`, npm
`@mariozechner/pi-coding-agent`, command `pi`). It has four default tools:
`read`, `bash`, `edit` and `write`. It can also enable `grep`, `find` and `ls`.
Pi talks to many providers, and it accepts any OpenAI-compatible endpoint
through `models.json`. Before this spec, OpenAIDR read no Pi session.

Pi writes one JSONL file per session. Pi documents the format in
`docs/session-format.md` of its package. This spec was checked against
version 0.73.1 and a real session file that it wrote.

## Goals

- `openaidr sessions --agent-kind pi` lists each Pi session on the machine.
- Each tool call has a span, a status, a tool class and canonical arguments.
- Each assistant response has its token usage.

## Non-goals

- Sign-in accounts. Pi keeps API keys in `auth.json`. A key is not an identity.
- Incremental reads. `collect_file` reads the whole file again.
- Shell commands that a person runs with `!` (`bashExecution`). These are not
  agent tool calls, and this reader does not emit them.
- Extension tools. Their names are not in the class table, so `tool_class` is
  `None`.

## Decisions

| ADR | Decision |
|---|---|
| [0043](../adrs/0043-read-every-branch-of-a-pi-session-tree.md) | Read every branch of a Pi session tree, not only the active branch |

## Design

### Where sessions live

The agent directory is `PI_CODING_AGENT_DIR`, else `~/.pi/agent`. Sessions are
`<agent dir>/sessions/--<cwd>--/<timestamp>_<uuid>.jsonl`. The reader walks
`sessions/` with `os.walk(onerror=...)` and reads each `.jsonl` file.
`OPENAIDR_PI_ROOT` overrides the agent directory for the CLI.

### The file

The first line is the header:
`{"type":"session","version":3,"id":"<uuid>","timestamp":"<iso>","cwd":"<dir>"}`.
The reader reads only version 3. It reports any other version as a failure and
does not read the file. Every later line is an entry with an 8-hex `id`, a
`parentId` and an ISO `timestamp`.

| Entry `type` | What the reader does |
|---|---|
| `message`, role `user` | A `user` turn. The first one is `initial_prompt`. |
| `message`, role `assistant` | An `assistant` turn. Its `toolCall` blocks are its tool calls. Its `responseId` and `usage` are a `ModelResponse`. |
| `message`, role `toolResult` | The result of the call with the same `toolCallId`. |
| `compaction` | A `Compaction` with trigger `unknown` and `pre_tokens` from `tokensBefore`. |
| `custom_message` | A `ContextItem` with source `injected` and the extension's `customType` as its name. |
| `session_info` | The session title. The last one wins. |
| Other types | Not read. |

### Identity

- The session id is `pi:<header id>`.
- The turn key is the entry `id`. Pi never rewrites an entry, so the key is stable.
- The span is `span_id(session id, entry id, index of the toolCall block)`.

### Status

| Evidence | Status |
|---|---|
| One result, `isError: false` | `ok` |
| One result, `isError: true` | `error` |
| No result | `pending` |
| Two or more results with the same call id | `unknown` |

Pi writes an extension's block as an error result. The result text is the
extension's reason. The file does not show which results are blocks, so a
blocked call reads as `error`, never as `rejected`. Pi writes the exit code of
`bash` only inside the result text, so `exit_code` is `None`.

`duration_ms` is the result's `timestamp` minus the assistant message's
`timestamp`. Both are Unix milliseconds that Pi writes.

### Tool classes and canonical arguments

| Tool | Class | Canonical arguments |
|---|---|---|
| `bash` | `shell` | `command` |
| `read` | `file_read` | `path` |
| `grep`, `find`, `ls` | `file_search` | `path` |
| `write` | `file_write` | `path`, `content` |
| `edit` | `file_edit` | `path`, `edits`, `new_text` |

`edit` takes `edits: [{oldText, newText}]`. Pi also accepts a legacy top-level
`oldText` and `newText`, and appends them last. The reader does the same.

### Usage

`pi-ai` stores `input` without the cache buckets for every provider. The four
counts map to `input_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens` and `output_tokens` directly.

## Failure behavior

- A line that is not a JSON object withholds the session. The failure names the
  line number, never its content.
- A file without a session header is not read.
- A last line without a newline is a record that Pi still writes. The reader
  ignores it until the next read.
- Two files with the same session id: the reader keeps the first and reports the
  second.

## Verification

`tests/test_pi.py` reads a fixture that has the shape of a real Pi 0.73.1
session. It also tests branches, statuses, edits, compaction and malformed
lines.
