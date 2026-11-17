# OpenAIDR — OpenCode

**Status:** implemented
**Date:** 2026-11-17

*A sixth agent kind, `opencode`, with an OpenAIDR-owned reader
(`readers/opencode.py`). Extends [Session collection](session-collection.md)
and [Token usage](token-usage.md). OpenCode is the open-source coding agent
published as the npm package `opencode-ai`.*

## Context

OpenCode 1.x keeps every session of every project in one SQLite database. A
row holds each session, message and message part, and the `data` column of a
message or a part is its own JSON. `adr-sensor` does not parse OpenCode, so
OpenAIDR reads the database itself, read-only, on the Cursor reader's design.

## Goals

- `openaidr sessions --agent-kind opencode` lists each OpenCode session, a
  sub-agent's included, with its turns, tool calls, outcomes, usage and
  compactions.
- A tool call has a span that does not move as the session grows.
- Each call has a tool class and canonical arguments from OpenCode's own tool
  table.
- An MCP call names its server as the configuration names it, or names none.

## Non-goals

- OpenCode 0.x, which kept JSON files under `storage/`. OpenCode 1.x moves
  them into the database.
- The newer `session_message` table. OpenCode 1.19.5's own agent loop writes
  and reads `message` and `part` (ADR-0044).
- Who OpenCode is signed in as. `auth.json` holds provider credentials, not a
  person.
- Windows (ADR-0013).

## Decisions

| ADR | Decision |
|---|---|
| [0044](../adrs/0044-read-opencode-from-its-message-and-part-tables.md) | Read the `message` and `part` tables, read-only, one session per `session` row |
| [0045](../adrs/0045-split-an-opencode-mcp-tool-against-its-configured-servers.md) | Split an MCP tool name against the configured server names, and leave it whole when none or several fit |

## Evidence

- **Source.** OpenCode's repository at tag `v1.19.5`, the version published
  to npm on 2026-10-28, read and not run: the tables
  (`packages/core/src/session/sql.ts` and its migrations), the projector that
  writes them (`packages/core/src/session/projector.ts`), the message and part
  schema (`packages/schema/src/v1/session.ts`), the agent loop
  (`packages/opencode/src/session/processor.ts`), usage
  (`Session.getUsage`), the permission errors
  (`packages/core/src/v1/permission.ts`), the tools
  (`packages/opencode/src/tool/`) and MCP naming
  (`packages/opencode/src/mcp/catalog.ts`). Everything in this spec comes from
  these.
- **Not checked: a recorded session.** The OpenCode binary was not run.
  Real provider error wording and a real database are unconfirmed.

## Design

### Where the database is

| | |
|---|---|
| Data directory | `$XDG_DATA_HOME/opencode`, else `~/.local/share/opencode`, on Linux and macOS. `default_readers(opencode_root=…)` overrides it |
| Database | `opencode.db`. A build from another channel writes `opencode-<channel>.db`. `$OPENCODE_DB` names another file, absolute or relative to the data directory. The reader reads all of them |
| Configuration | `$XDG_CONFIG_HOME/opencode` (`config.json`, `opencode.json`, `opencode.jsonc`), `$OPENCODE_CONFIG`, `$OPENCODE_CONFIG_DIR`, `~/.opencode`, and `opencode.json(c)` and `.opencode/opencode.json(c)` in the session's directory and each folder above it |
| Access | `mode=ro`, a 2-second busy timeout, one read transaction for each pass. OpenCode uses WAL |

### The tables

| Table | Columns the reader reads |
|---|---|
| `session` | `id` (`ses_…`), `parent_id`, `directory`, `title`, `version`, `model` (JSON), `time_created`, `time_updated` (ms) |
| `message` | `id` (`msg_…`), `session_id`, `time_created`, `time_updated`, `data`: a user or an assistant message |
| `part` | `id` (`prt_…`), `message_id`, `session_id`, `time_created`, `time_updated`, `data`: one part |

Message and part ids start with the time they were made, so they sort in the
order they were made. OpenCode updates a part in place as its call runs.

| Part `type` | What the reader takes |
|---|---|
| `text` | The message's text. A `synthetic` one, text OpenCode adds itself, is a context item, source `injected`. An `ignored` one is not read |
| `tool` | A call |
| `step-finish` | One model response |
| `compaction` | A compaction, trigger `auto` or `manual` |
| `reasoning`, `file`, `agent`, `subtask`, `step-start`, `snapshot`, `patch`, `retry` | Nothing |

### Identity

| Level | Identity |
|---|---|
| Main session | `opencode:<session id>` |
| Sub-agent session | `opencode:<root session id>:<session id>`, the root found through `parent_id` (ADR-0001). Every turn and response is `is_sidechain` |
| Turn | The message's `id`. A message's error turn is `<message id>.error` |
| Span | `span_id(session_id, message id, index of the tool part among the message's tool parts)` |

A user message with no text adds no turn.

### Tool calls

#### Status

| `state.status` | Status | Notes |
|---|---|---|
| `pending`, `running` | `pending` | |
| `completed` | `ok` | |
| `completed`, a `bash` call whose `metadata.exit` is not 0 | `error` | OpenCode completes the call. Its own exit status says it failed, as Codex's does |
| `error`, error starts `The user rejected permission to use this specific tool call` | `rejected` | `denial_kind` `user-rejected`. The same with feedback |
| `error`, error starts `The user has specified a rule which prevents you from using this specific tool call` | `rejected` | `denial_kind` `permission-rule` |
| `error`, error `Tool execution aborted`, or `metadata.interrupted` | `interrupted` | |
| `error`, anything else | `error` | An MCP result with `isError` is here |

- **Exit code**: `metadata.exit` on a `bash` call.
- **Duration**: `state.time.end − state.time.start`.
- **Result**: `state.output`, or `state.error`.
- **Working directory**: `bash`'s `workdir`, else the message's `path.cwd`.
- **Truncated**: `metadata.truncated`.
- **Skill**: a `skill` call's `name` is its `attributed_skill`.

#### Tool names

| Names | Class | Canonical arguments |
|---|---|---|
| `bash` | `shell` | `command` |
| `read` | `file_read` | `path` (`filePath`) |
| `glob`, `grep`, `list` | `file_search` | `path` |
| `write` | `file_write` | `path`, `content` |
| `edit` | `file_edit` | `path`, `new_text`, and `edits`: one from `oldString` and `newString`. An empty `oldString` creates the file, so its kind is `add` |
| `apply_patch` | `file_edit` | `edits`, one for each file, from `patchText` in Codex's patch grammar |
| `webfetch` | `web_fetch` | `url` |
| `websearch` | `web_search` | |
| `task` | `delegate` | `agent` (`subagent_type`) |
| `skill` | `skill` | `skill` (`name`) |
| `execute` | `script` | |
| `todowrite`, `todoread`, `question`, `lsp`, `plan_exit`, `invalid`, `list_mcp_resources`, `list_mcp_resource_templates`, `read_mcp_resource` | `other` | |
| `<server>_<tool>` | `mcp` | |

#### MCP servers

OpenCode names an MCP tool `sanitize(server) + "_" + sanitize(tool)`, where
`sanitize` writes each character that is not a letter, digit, `_` or `-` as
`_`. A server and a tool can both contain `_`, so the name alone cannot be
split (ADR-0045).

1. A name in the tool table is a built-in and is never split.
2. The reader reads the `mcp` object, or `mcp.servers`, from every
   configuration file above.
3. Exactly one configured server whose written name and `_` start the tool
   name splits it. `mcp_server` is the configured name.
4. None, or more than one: the call stays whole, with no server and no class.

A configuration file that cannot be read is a `ReaderFailure`. JSONC comments
and trailing commas are read.

### Session fields

| Field | Source |
|---|---|
| `started_at` | `session.time_created` |
| `last_activity_at` | The newest of `session.time_updated`, each message's `time.created` and `time.completed`, and each part's `time_created` |
| `model` | The last response's model, else `session.model.id` |
| `working_directory` | `session.directory` |
| `agent_version` | `session.version`, the OpenCode version that made the session |
| `generated_title` | `session.title`, unless it is OpenCode's placeholder `New session - <ISO time>` or `Child session - <ISO time>`. OpenCode writes the title itself |
| `initial_prompt` | The first user message's text |
| `user`, `machine` | The local user and host |
| `compactions_recorded`, `subagents_recorded` | True |
| `entrypoint`, `git_branch`, `title` | None. OpenCode does not record them |

### Model responses

One for each `step-finish` part. A step is one request to the provider.

| Field | Source |
|---|---|
| `response_id` | The part's `id` |
| `model` | The message's `modelID` |
| `occurred_at` | The part row's `time_created` |
| `final` | True |
| Usage | `tokens.input` is uncached input: OpenCode takes the cache reads and writes out of it. `cache.read` and `cache.write` as stated. OpenCode takes reasoning out of `tokens.output`, so `output_tokens` is `output + reasoning` and `reasoning_tokens` is `reasoning` |

A turn names its response only when its message has one step.

An assistant message with an `error` is also an `error` turn with
`error.data.message`. `MessageAbortedError`, a person who stopped the agent, is
not.

### Collection

- **Cold**: every database. With a window, only sessions with a session,
  message or part row whose `time_updated` is in the window are read. A
  session's last activity then decides.
- **Incremental**: a change to a database or its `-wal` file reads the
  sessions with a row whose `time_updated` is at or after the newest time the
  last read saw, less 5 seconds. A session is returned only when it differs
  from the last one returned for it.
- **Locations**: the data directory.

## Failure behavior

| Condition | Result |
|---|---|
| No data directory | No sessions, and no failure |
| The database is locked past the timeout | A failure: database busy |
| No `session`, `message` or `part` table | A failure: unsupported schema |
| A message or part whose `data` is not a JSON object | A failure. That row is skipped |
| A configuration file that cannot be read | A failure. Its servers do not split calls |

## Verification

`tests/test_opencode.py` builds databases in OpenCode 1.19.5's shape
(`tests/fixtures/opencode_db.py`) and checks each table above: identity, the
placeholder title, statuses, exit codes, canonical edits, the MCP split
(JSONC, the newer layout, two servers that fit, a built-in), responses, a
sub-agent, a compaction, a provider error, an incremental change, a broken
schema, a broken part, the window and a channel database.

## Open facts

| | Fact | Effect |
|---|---|---|
| O1 | The reader did not read a database that a running OpenCode wrote | The shapes come from OpenCode's source. A provider's own error wording is unconfirmed |
| O2 | An MCP tool's server is read from the configuration as it is now | A server removed from the configuration leaves its calls unsplit, with no class |
| O3 | OpenCode records no entrypoint: the terminal UI, `opencode run`, the server and the desktop app write the same rows | A consumer cannot tell whether a person was present |
| O4 | `opencode import` writes a part's `time_created` as the time of the import | An imported session's responses are dated by the import |
| O5 | The experimental `execute` tool runs other tools. Their record is not read | Its calls are one `script` call |
| O6 | OpenCode keeps provider credentials, not a person | No account is read |
