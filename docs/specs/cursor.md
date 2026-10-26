# OpenAIDR — Cursor

*Implemented (2026-10-21; the desktop app 2026-10-26). A fourth agent kind,
`cursor`, with an OpenAIDR-owned reader (`readers/cursor.py`,
`readers/cursor_log.py`, `readers/cursor_ide_log.py`). Extends
[Session collection](session-collection.md) and [Token usage](token-usage.md).
**The Claude Code reader is the reference design**: Cursor keeps one store for
each conversation, a sub-agent's included, as Claude Code keeps one transcript
for each session. Decisions: ADR-0037, ADR-0038 and ADR-0039.*

## At a glance

| | |
|---|---|
| Covers | The Cursor CLI (`agent`, `cursor-agent`) in the terminal, in print mode (`-p`) and over ACP. Its sub-agents. The local-inference build (`agent-local`) writes the same files. **The desktop app's agent panel** (3.x), its sub-agents included (see [The desktop app](#the-desktop-app)) |
| Not covered | **Cloud agents**, which run on Cursor's servers. Desktop conversations from before the agent store (no root), Tab and inline edits |
| Reads | `<config root>/chats/<md5(cwd)>/<id>/store.db` (read-only) and its `meta.json` for messages, calls, outcomes and times. The debug log `<tmp>/cursor-agent-logs-<uid>/session-*.log` for per-response tokens, the entrypoint, client errors and MCP connection failures. `<data root>/projects/<slug>/agent-transcripts/` for client errors. `<config root>/cli-config.json` for the account. For the desktop app: `<user data>/User/globalStorage/state.vscdb` (read-only) and its structured log `<user data>/logs/*/window*/exthost/*/Cursor Structured Logs.log` |
| Config root | `$CURSOR_CONFIG_DIR`, else `$XDG_CONFIG_HOME/cursor`, else `~/.cursor`. `default_readers(cursor_root=…)` overrides it |
| Data root | `$CURSOR_DATA_DIR`, else `~/.cursor`. Holds `projects/` |
| Backed by `adr-sensor` | No. Its Cursor parser reads the desktop's bubbles, keys turns by position, keeps no result bodies and opens the store read-write (ADR-0037) |
| Platforms | Linux and macOS. Windows deferred (ADR-0013) |
| Main gaps | No usage once the debug log is pruned (7 days or 50 files). The standard CLI's terminal UI writes no usage (C10). No MCP connection success, only failures. The desktop app has usage only where its agent loop runs on the machine (C11). Each has an entry in [Open facts](#open-facts) |

## Evidence

- **The shipped CLI, run against a mock model.** `agent-cli-local`
  `2026.10.19-6bbf2c4` (linux/x64) ran with `--authless` and `--base-url`
  against a local OpenAI-compatible mock, in an isolated home. The mock
  scripted shell calls that succeed, fail and are refused, a patch, a write, a
  string replace, reads, searches, MCP calls (success, a tool error, a refusal,
  an unknown tool, an unknown server), a delete that is refused, a todo update,
  a sub-agent, a retried 429 and a final 401. Two model families ran: `gpt-5.5`
  and `claude-sonnet-4-5`. Everything marked **Seen** comes from those files.
- **Source.** The standard CLI package `2026.10.19-6bbf2c4` and desktop
  `3.26.13`, downloaded and unpacked, not run. The protobuf field tables come
  from the CLI bundle. Marked **Source**.
- **Real stores, earlier.** Six desktop conversations, 2,165 bubbles and 1,448
  calls ([Session collection](session-collection.md#codex-and-cursor-are-the-intended-next-kinds)).
  Marked **Measured**.
- **The shipped desktop app, run against a mock model.** Cursor `3.27.12`
  (linux/x64, commit `df132ff`) ran under Xvfb in an isolated home, with its
  `localMode` build flag set, as Cursor's Private Inference build sets it.
  Its own agent loop then runs in its `cursor-agent-exec` extension against
  `CURSOR_LOCAL_AGENT_BASE_URL`. The mock scripted shell calls, writes, reads,
  MCP calls and a sub-agent. Everything marked **Seen (desktop)** comes from
  the `state.vscdb` and logs it wrote. The store code is the same in every
  build; only where the loop runs differs.
- **Not checked: a signed-in session.** The standard CLI and the standard
  desktop app need a Cursor account. The server then runs the agent loop and
  writes the same blobs. Real model names, real error wording and real cache
  counts are unconfirmed.

## The chat store

| | |
|---|---|
| Path | `<config root>/chats/<md5hex(resolved cwd)>/<agentId>/store.db`, one for each conversation. A sub-agent has its own store beside its parent's (Seen) |
| Schema | `blobs(id TEXT PRIMARY KEY, data BLOB)`, `meta(key TEXT PRIMARY KEY, value TEXT)`. WAL. Opened `mode=ro`; one read transaction for each pass (Source, Seen) |
| `meta['0']` | Lowercase hex of UTF-8 JSON: `agentId`, `latestRootBlobId`, `name` (default `New Agent`), `createdAt` (ms), `mode`, `approvalMode`, `isRunEverything`, `lastUsedModel`, `subagentInfo {parentAgentId, rootParentAgentId, toolCallId, typeName}` (Seen) |
| `meta.json` | Beside the store, for a main conversation only: `cwd`, `title`, `createdAtMs`, `updatedAtMs` (Seen) |
| Blobs | Content-addressed and insert-only. Every checkpoint root stays in `blobs`; `latestRootBlobId` names the current one (Seen) |
| Root | Protobuf `agent.v1.ConversationStateStructure`. Field 1 lists the blob ids of the **prompt messages**, in order; field 8 the **turns**; 9 the workspace URIs; 26 the start time (ms) (Source, Seen) |

**The prompt messages are the conversation.** Each is a JSON blob, the shape the
model received:

| `role` | What the reader takes |
|---|---|
| `system` | Nothing |
| `user`, string content | A context item: the harness's `<user_info>`, `<rules>`, `<agent_transcripts>` and similar blocks |
| `user`, parts | A user turn. The person's words are inside `<user_query>` |
| `assistant` | A turn: its text, and one call for each `tool-call` part `{toolCallId, toolName, args}`. `id` is Cursor's message id |
| `tool` | No turn. Each `tool-result` part `{toolCallId, toolName, result}` updates the call it names. `providerOptions.cursor.highLevelToolCallResult.output` is Cursor's typed outcome as JSON |

**The turns hold the times.** Each turn is an `AgentConversationTurnStructure`
whose steps are blobs: a `ToolCall` carries its `tool_call_id` (57),
`started_at_ms` (59) and `completed_at_ms` (60), and the oneof case that names
the tool; an `AssistantMessage` carries its text and `started_at_ms` (2); the
`UserMessage` carries `started_at_ms` (25). The reader decodes only these
fields. It needs no `.proto` file.

**Model-facing names depend on the model.** `gpt-5.5` sees `ReadFile`, `rg`,
`ApplyPatch` and `Subagent`; `claude-sonnet-4-5` sees `Read`, `Grep`, `Write`,
`StrReplace` and `Task` (Seen). The typed case is the same for both. The tool
table lists both spellings.

**Failures** are a `ReaderFailure`, never raised:

| Condition | Result |
|---|---|
| No chats directory | No sessions, and no failure |
| A store with no `meta` row or no root yet | No session for it. Cursor deletes an empty store when its process ends |
| Locked past a 2-second timeout | Failure: store busy |
| A required table missing | Failure: unsupported schema |
| A prompt message that is not JSON, or a root that does not decode | That item skipped, and reported |

## Identity

| Level | Identity |
|---|---|
| Main session | `cursor:<agentId>` |
| Sub-agent session | `cursor:<rootParentAgentId>:<agentId>`, from `meta.subagentInfo` (ADR-0001). Every turn and response is `is_sidechain` |
| Turn | `key` is the message's own `id` (`msg-…`), else its blob id. A key seen before in the same list takes `#<n>`. `position` is the index among turns |
| Span | `span_id(session_id, turn key, index of the tool-call part in the message)` |

- **The prompt list only grows**, except at compaction, when Cursor replaces
  older messages with a summary. A message keeps its id and its blob id, so a
  span does not move as the list grows.
- **Outcomes join on `toolCallId`.** A result that names no known call is not
  read.

## Tool calls

### Status

The status is Cursor's typed outcome, never the result text.

| `output` case | Status | Notes |
|---|---|---|
| `success` | `ok` | An MCP `success` with `isError: true` is `error` |
| `failure` | `error` | `aborted: true` is `interrupted` |
| `rejected` | `rejected` | `denial_kind` from the reason (below) |
| `permissionDenied` | `rejected` | `permission-rule` |
| `timeout`, `spawnError`, `error`, `fileNotFound`, `notFile`, `invalidFile`, `fileBusy`, `readPermissionDenied`, `writePermissionDenied`, `toolNotFound`, `serverNotFound` | `error` | |
| No result yet | `pending` | |
| A result with no typed outcome | `unknown` | |

| Reason | `denial_kind` |
|---|---|
| Starts `User rejected` | `user-rejected`. In print mode Cursor writes this for any call it does not approve |
| `permissions configuration`, `Blocked by …`, `cursorignore`, `Cursor internal file`, `denylist`, a hook | `permission-rule` |
| Empty, or anything else | None |

- **Exit code**: `exitCode` on a shell `success` (0 when absent, as protobuf
  JSON omits a zero) or `failure`.
- **Working directory**: `workingDirectory` on the shell outcome, when not empty.
- **Duration**: `completed_at_ms − started_at_ms` from the turn's step.
- **Result**: the `tool-result` text. Cursor's typed outcome is not copied into
  the result.

### Tool names

`tool_name` is the model-facing name. `CallMcpTool` is the exception: it is
named by its `toolName` argument, with `mcp_server` its `server` argument, the
identifier Cursor resolves.

| Names | Class | Canonical arguments |
|---|---|---|
| `Shell` | `shell` | `command` |
| `AwaitShell`, `WriteShellStdin` | `shell_control` | |
| `ReadFile`, `Read` | `file_read` | `path` |
| `Glob`, `rg`, `Grep`, `LS` | `file_search` | `path` |
| `Write` | `file_write` | `path`, `content` |
| `StrReplace`, `EditNotebook` | `file_edit` | `path`, `new_text` |
| `ApplyPatch` | `file_edit` | `edits`, one `{path, kind, new_text}` per file, in Codex's patch grammar |
| `Delete` | `file_write` | `path` |
| `WebSearch` | `web_search` | |
| `WebFetch`, `Fetch` | `web_fetch` | `url` |
| `Subagent`, `Task` | `delegate` | `agent` |
| `TodoWrite`, `AskQuestion`, `SwitchMode`, `CreatePlan`, `GetMcpTools`, `FetchMcpResource`, `ListMcpResources`, `GenerateImage` | `other` | |
| `CallMcpTool` | `mcp` | |

## Session fields

| Field | Source |
|---|---|
| `started_at` | Root field 26, else `meta.createdAt` |
| `last_activity_at` | The newest time read: a step, a usage line, `meta.json.updatedAtMs` |
| `model` | The last response's model, else `meta.lastUsedModel` |
| `working_directory` | `meta.json.cwd`, else the first workspace URI; a sub-agent takes its parent's |
| `entrypoint` | The debug log's `surface` on the session's first turn: `headless` (print mode), `interactive`, `acp` |
| `title` | `meta.json.title`, else `meta.name` unless it is `New Agent` |
| `initial_prompt` | The first user turn's `<user_query>` |
| `user`, `machine` | The local user and host |
| `subagents_recorded` | True |
| `compactions_recorded` | False. A compaction was not seen (C6) |

## Model responses

Cursor writes no usage to its store or its transcript. Its debug log states it
in one of two ways (ADR-0038). Where the agent loop runs in the CLI (the
local-inference build), it writes one line for each model response:

```
[<ISO time>] logger {"message":"Setting token details for client token ring",
  "metadata":{"modelName":"gpt-5.5","usedTokens":4647,"maxTokens":0,
  "inputTokens":4605,"outputTokens":42,"cacheReadTokens":2302,"cacheWriteTokens":0,
  "toolName":"TASK","toolId":"call_…"}}
```

| | |
|---|---|
| File | `<os tmp>/cursor-agent-logs-<uid>/session-<ISO>-<pid>-<n>.log`, one for each process, mode 0600, append-only. Cursor keeps the 50 newest and none older than 7 days. `CURSOR_AGENT_DISABLE_DEBUG_LOG` turns it off (Source, Seen) |
| Placement | An `agent_cli.turn.start` line names `conversation_id`, `request_id` and `surface`. A usage line belongs to the conversation of the turn in force. A line with `toolName: "TASK"` belongs to the sub-agent its `toolId` started: the child whose `subagentInfo.toolCallId` is that id |
| Identity | `response_id` is `<request_id>:<n>`, `n` counting that session's usage lines in the request. A sub-agent's is `<request_id>:<call id>:<n>`: its lines carry its parent's request, and the same id in two sessions reads as one response filed twice. The log only grows, so `n` does not move |
| Tokens | `inputTokens` **includes** cache reads and writes (Seen: 35,106 input with 35,100 read). `input_tokens` is `inputTokens − cacheReadTokens − cacheWriteTokens`; a cache write of 0 is `None`, as for Codex (its ADR-0031) |
| Context window | `maxTokens` when above 0 |
| Turn totals | Where the agent loop runs on Cursor's server (the standard CLI), no usage line is written. In print mode, the turn's `agent_cli.turn.outcome` line carries its totals as strings: `input_tokens` (already net of the cache), `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, and `model_intent`, the model the person chose (Source; Seen in the local-inference build, which writes both). A request with no usage line for the session is one response from its totals: `response_id` `<request_id>:turn`, input as stated, `model` from `model_intent`. A request with usage lines takes no response from its totals, which are the lines' sum (Seen: 5,492 input less 4,119 read is the stated 1,373) |
| Turn link | When a request has as many usage lines as the session has assistant messages in it, each message's `response_id` is the line at its position. Otherwise none is linked. A turn's totals count as one line |
| `final` | True |

## Client errors

A turn that ends in an error is a turn with role `error`, as for Codex. Its
text is the transcript's `{"type":"turn_ended","status":"error","error":…}`
row, Cursor's own message (Seen: `Unauthorized (HTTP 401): …`). Its time comes
from the debug log's `agent_cli.turn.outcome` with `outcome: "error"` for the
same conversation, paired by order. Its key is `e<n>`, `n` counting error rows.

## MCP connections

The debug log names a server that failed to load:
`{"message":"Failed to load MCP server","metadata":{"identifier","source","errorMessage"}}`
(Seen). Each is an `MCPConnection` with `connected: False`, the identifier as
`server`, the transport from the `MCP HTTP exchange failed` lines before it,
and a failure category from the error (`ECONNREFUSED` is `network`). A session
takes the failures of the process that served it. A successful connection
writes no line, so a server that connected is absent (C5).

`mcp_log_state` is `applied` when a log was placed, `no_log_for_session` when
no log names the session, and `no_log_root` when the log directory is absent.

## Who is signed in

`<config root>/cli-config.json` `authInfo {email, displayName, userId, authId,
teamId, teamName}` (Source). `account_id` is `userId`, else `authId`. No plan.
`sign_in_records("cursor")` is that file. Nothing is asked of Cursor's server.

## Growing sessions

| Rule | Detail |
|---|---|
| Unit | A session: one store and its `meta.json`, plus the log lines and transcript rows that name it |
| `collect_file(path)` | A store, its WAL or its `meta.json`: that session. A debug log: every session the log names. A transcript: its session. Any other path: nothing. Each session named is rebuilt, and returned when its digest differs from the one last returned (ADR-0018's comparison, scoped to the sessions the event names) |
| The invariant | After any event, a consumer that replaced the sessions returned holds what a cold read returns |
| Between reads | Responses replace by `(session, response_id)`. Turns replace wholesale: compaction can drop older ones |

## The desktop app

The desktop app's agent panel (a "composer" in Cursor's code) runs the same
agent loop and keeps the same blobs as the CLI, in one database for all of its
conversations (Seen (desktop)). The reader decodes them with the CLI's code;
only the container differs (ADR-0039).

### Where it lives

| | |
|---|---|
| User data | `$VSCODE_PORTABLE/user-data`, else `$VSCODE_APPDATA/Cursor`, else `$XDG_CONFIG_HOME/Cursor` or `~/.config/Cursor` on Linux, `~/Library/Application Support/Cursor` on macOS. This is the order Cursor resolves it (Source). `default_readers(cursor_user_data_root=…)` overrides it |
| Store | `<user data>/User/globalStorage/state.vscdb`, SQLite in WAL mode. Opened `mode=ro`; one read transaction for each pass |
| Table | `cursorDiskKV(key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)` |

### Keys read

| Key | Value |
|---|---|
| `composerData:<composerId>` | One conversation: JSON. Fields read are below |
| `agentKv:blob:<hex>` | The same content-addressed blobs as the CLI's `blobs` table: the root, the JSON prompt messages and the protobuf turns and steps |
| `agentKv:checkpoint:<composerId>` | Hex of the root blob id. Written before `composerData` data version 11; Cursor moves it into `conversationState` when it next loads the conversation (Source). Read only when `conversationState` is absent |

`bubbleId:<composerId>:<bubbleId>` rows are the panel's own projection of the
same conversation, with Cursor's tool names and result strings but not the
typed outcome. They are not read (ADR-0039).

### `composerData`

| Field | Read as |
|---|---|
| `conversationState` | The root: `~` and the base64 of the `ConversationStateStructure`, else its hex, as Cursor itself decodes it (Source). `~` alone, or an empty root, is no conversation yet |
| `composerId` | The agent id: the session is `cursor:<composerId>` |
| `subagentInfo` | `rootParentConversationId`, `parentComposerId` and `toolCallId`. A sub-agent is `cursor:<rootParentConversationId>:<composerId>`, as for the CLI |
| `name` | The title. Cursor names a conversation from its first message |
| `createdAt`, `lastUpdatedAt` | Milliseconds: the start when the root has none, and an activity time |
| `modelConfig.modelName` | The model, when no response states one. `default` is Cursor's Auto |
| `workspaceIdentifier.uri.fsPath` | The working directory, else the root's first workspace URI; a sub-agent takes its parent's |

The root, the prompt messages, the turns, the identity rules, the tool table,
the statuses and the call fields are the CLI's, exactly
([The chat store](#the-chat-store) to [Tool calls](#tool-calls)). The desktop
app's model-facing tool names are the `claude-*` family's (`Read`, `Write`,
`StrReplace`, `Task`) whatever the model (Seen (desktop)).

### Entrypoint

`ide`. The store holds only the desktop app's own conversations, and a person
starts each one there. Cursor records no surface for them. A conversation with
`createdFromBackgroundAgent` or `createdFromLocalScheduledTask` set has no
entrypoint: a program may have started it, and no such conversation was seen
(C12).

### Usage, client errors and times

The desktop app writes no token usage to the store: a bubble's `tokenCount` is
never filled, and the turn's totals (`turnTokenUsage`) are kept in memory only
(Source). Its structured log, one file for each window and launch, states what
the CLI's debug log states:

```
2026-10-26 12:49:46.050 [info] {"level":"debug","key":"agent_exec","message":"Setting token details for client token ring",
  "metadata":{"modelName":"default","usedTokens":"1240","maxTokens":"0","inputTokens":"1200","outputTokens":"40",
  "cacheReadTokens":"600","cacheWriteTokens":"0", …}}
```

| | |
|---|---|
| File | `<user data>/logs/<YYYYMMDDTHHMMSS>/window<N>/exthost/<extension>/Cursor Structured Logs.log`. Cursor keeps the current launch's folder and the nine before it (Source) |
| Line | `<local date> <local time>.<ms> [<level>] <JSON>`. The time has no zone: it is the machine's own. Counts are strings |
| Turn | `agent.turn.start` and `agent.turn.outcome` name `conversation_id` and `request_id`. A turn is open from its start to its outcome |
| Usage line | One model response, written when the response and the tools it called finish. It names no conversation. It belongs to the one turn open in that file. When two turns are open, or none, it is not placed |
| Sub-agent | The child runs between `subagent proxy: delegating to renderer` and `subagent proxy: renderer settled`, both with the starting call's `toolId`. A usage line inside one such span is the child's. Inside two at once, it is not placed |
| Identity | As the CLI's: `<request_id>:<n>`, and `<request_id>:<call id>:<n>` for a sub-agent |
| Tokens | As the CLI's: `inputTokens` includes the cache, so input is `inputTokens − cacheReadTokens − cacheWriteTokens` |
| Client error | `agent.turn.outcome` with `outcome: "error"` names its time and `error_text`. The transcript's `turn_ended` row, where there is one, gives the text, as for the CLI |
| `mcp_log_state` | `not_attempted`. The desktop app's MCP logs are not read |

The usage line is written by the agent loop. **The loop runs on the machine
only in the Private Inference (local) build and where Cursor's agent host
runs it.** In the standard app, the loop runs on Cursor's server, the line is
not written, and a desktop session has no responses (C11).

### Who is signed in

`ItemTable` in the same database, when `cli-config.json` names no one:
`cursorAuth/stripeMembershipAuthId` is the account id, `cursorAuth/cachedEmail`
the email and `cursorAuth/stripeMembershipType` the plan. The tokens beside
them are never read. When `cli-config.json` names the same `authId`, its
account takes the desktop app's plan.

### Growing sessions

| Rule | Detail |
|---|---|
| `collect_file(path)` | `state.vscdb` or its WAL: every desktop conversation whose `composerData` changed since the last read, and its sub-agents. A structured log: every desktop conversation. A transcript: its conversation, from either store |
| Cache | Each conversation's decoded store is kept against the SHA-256 of its `composerData` value, so an unchanged conversation reads no blobs |

## Coverage by agent kind

| Field | `cursor` |
|---|---|
| Session identity, start, last activity | Recorded |
| Model, per response | Recorded (debug log) |
| Working directory | Recorded |
| Agent version | Absent |
| Entrypoint | Recorded (debug log); `ide` for the desktop app |
| Git branch | Absent |
| Initial prompt | Recorded |
| Title | Partial: a name Cursor or the person set |
| Context items | Recorded: harness blocks, not split into parts |
| Compactions | Unconfirmed (C6) |
| Client errors | Recorded (role `error`) |
| MCP connections | Partial: failures only |
| Responses and tokens | Recorded while the debug log is kept: per response where the loop runs in the CLI, per turn in the standard CLI's print mode, absent in its terminal UI (C10); input normalised. The desktop app: per response from its structured log where its loop runs on the machine, else absent (C11) |
| Context window | Partial: when Cursor states `maxTokens` |
| Turn time | Recorded |
| Sub-agent sessions | Recorded |
| Permission mode, per turn | Absent: one mode for the session |
| Tool name, MCP server, arguments | Recorded |
| Tool class, canonical arguments | Recorded; patches as `edits` |
| Status and denial | Recorded |
| Exit code | Recorded |
| Per-call working directory | Partial: on refusals and non-empty shell outcomes |
| Duration | Recorded |
| Attributed skill, plugin | Absent |
| MCP transport per call | Absent |
| Signed-in account | Recorded (`cli-config.json`, else the desktop app's `ItemTable`, which also names the plan) |

## Open facts

**Fixtures are synthetic, built on the probe's files.** Real stores are never
committed.

| # | Question | Decides |
|---|---|---|
| C1 | A signed-in session against Cursor's server: model names, `routed_model_display_name`, cache counts, and whether usage lines still appear | Model and usage |
| C2 | Cursor's own wording for a usage limit, a rate limit and a content-policy stop, in `turn_ended` and `turn.outcome` | Block notices |
| C3 | The `surface` of an interactive session, and whether one process serves more than one conversation | Entrypoint, placement |
| C4 | A hook denial, an Auto-review denial and a user abort, end to end | `denial_kind`, `interrupted` |
| C5 | A line that records a successful MCP connection, and the identifier-to-name map in `projects/<slug>/mcps/` | `MCPConnection` |
| C6 | A compaction: `summary_archives` and the summary message | `Compaction` |
| C7 | The macOS layout against Linux, and `XDG_CONFIG_HOME` in practice | Platform coverage |
| C8 | Background sub-agents running at once in one process | Placement of their usage |
| ~~C9~~ | ~~A desktop `state.vscdb` from 3.x~~ Closed: 3.27.12 wrote one (Seen (desktop)) | Reading the desktop app |
| C10 | Whether the standard CLI's terminal UI and ACP sessions write turn totals anywhere. Its only `setTokenUsage` calls are in the print-mode runner (Source), so they are read as absent | Usage outside print mode |
| C11 | Whether the standard, signed-in desktop app writes usage lines: where its agent host is on (`cursor_agent_host`), the loop may run on the machine | Desktop usage |
| C12 | A conversation started by an automation or a background agent | The desktop entrypoint |

## Decisions

| ADR | Decision | Rejected |
|---|---|---|
| [0037](../adrs/0037-read-cursor-from-its-chat-store-prompt-messages.md) | An OpenAIDR-owned reader of the CLI's chat store. A prompt message is a turn; Cursor's typed outcome is the status; one kind for the CLI and the desktop app | `adr-sensor`'s parser; the transcript JSONL; the protobuf steps as the only source; two kinds |
| [0038](../adrs/0038-cursor-usage-from-its-debug-log.md) | Usage, the entrypoint, client error times and MCP failures come from Cursor's debug log, placed by the conversation each turn names | Context fill as input tokens; the `stop` hook; print mode's stdout; estimating from text |
| [0039](../adrs/0039-read-the-cursor-desktop-app-from-its-state-database.md) | The desktop app is a second container for the CLI's decoder: `composerData` and `agentKv:blob` rows in `state.vscdb`. Its usage comes from its structured log, placed only when one turn is open. Its entrypoint is `ide` | The bubbles; `adr-sensor`'s parser; a second kind; context fill as usage; placing a line by the turn last started |

## Non-goals

- Cloud agents, or any network collection.
- Configuration (MCP servers, skills, plugins, rules, hooks as configured);
  that is another component's concern.
- Running Cursor or installing a hook.
- Pricing tokens (ADR-0015).
- Windows (ADR-0013).

## References

- CLI package `2026.10.19-6bbf2c4`: `agent-kv` (store and meta), `agent-transcript`
  (transcripts), `./src/debug.ts` (debug log), `cursor-config` (paths, `cli-config.json`).
- Desktop `3.26.13`: `composerBlobStore.js`, `storageSizeBreakdown.js`.
- Community: codeburn's Cursor provider notes; toolpath-cursor.
