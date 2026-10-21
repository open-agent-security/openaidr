# OpenAIDR — Cursor

*Implemented (2026-10-21). A fourth agent kind, `cursor`, with an
OpenAIDR-owned reader (`readers/cursor.py`, `readers/cursor_log.py`). Extends
[Session collection](session-collection.md) and [Token usage](token-usage.md).
**The Claude Code reader is the reference design**: Cursor keeps one store for
each conversation, a sub-agent's included, as Claude Code keeps one transcript
for each session. Decisions: ADR-0037 and ADR-0038.*

## At a glance

| | |
|---|---|
| Covers | The Cursor CLI (`agent`, `cursor-agent`) in the terminal, in print mode (`-p`) and over ACP. Its sub-agents. The local-inference build (`agent-local`) writes the same files |
| Not covered | **The desktop app's own chats.** They live in `state.vscdb`, in a different envelope (see [The desktop app](#the-desktop-app)). **Cloud agents**, which run on Cursor's servers |
| Reads | `<config root>/chats/<md5(cwd)>/<id>/store.db` (read-only) and its `meta.json` for messages, calls, outcomes and times. The debug log `<tmp>/cursor-agent-logs-<uid>/session-*.log` for per-response tokens, the entrypoint, client errors and MCP connection failures. `<data root>/projects/<slug>/agent-transcripts/` for client errors. `<config root>/cli-config.json` for the account |
| Config root | `$CURSOR_CONFIG_DIR`, else `$XDG_CONFIG_HOME/cursor`, else `~/.cursor`. `default_readers(cursor_root=…)` overrides it |
| Data root | `$CURSOR_DATA_DIR`, else `~/.cursor`. Holds `projects/` |
| Backed by `adr-sensor` | No. Its Cursor parser reads the desktop's bubbles, keys turns by position, keeps no result bodies and opens the store read-write (ADR-0037) |
| Platforms | Linux and macOS. Windows deferred (ADR-0013) |
| Main gaps | No usage once the debug log is pruned (7 days or 50 files). No MCP connection success, only failures. The desktop app. Each has an entry in [Open facts](#open-facts) |

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
- **Not checked: a signed-in session.** The standard CLI needs a Cursor
  account. The server then runs the agent loop and writes the same blobs. Real
  model names, real error wording and real cache counts are unconfirmed.

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

Cursor writes no usage to its store or its transcript. Its debug log writes one
line for each model response (ADR-0038):

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
| Turn link | When a request has as many usage lines as the session has assistant messages in it, each message's `response_id` is the line at its position. Otherwise none is linked |
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

The desktop app (3.26.13) keeps its chats in
`<user data>/globalStorage/state.vscdb`, table `cursorDiskKV`:
`agentKv:checkpoint:<composerId>` names the root blob, and `agentKv:blob:<hex>`
holds the same protobuf root and JSON prompt messages (Source). Its usage is
`composerData.turnTokenUsage`, the last turn only (Source). It is not read yet:
no desktop session ran here. One decoder serves it once a real store confirms
the envelope (C9).

## Coverage by agent kind

| Field | `cursor` |
|---|---|
| Session identity, start, last activity | Recorded |
| Model, per response | Recorded (debug log) |
| Working directory | Recorded |
| Agent version | Absent |
| Entrypoint | Recorded (debug log) |
| Git branch | Absent |
| Initial prompt | Recorded |
| Title | Partial: a name Cursor or the person set |
| Context items | Recorded: harness blocks, not split into parts |
| Compactions | Unconfirmed (C6) |
| Client errors | Recorded (role `error`) |
| MCP connections | Partial: failures only |
| Responses and tokens | Recorded while the debug log is kept; input normalised |
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
| Signed-in account | Recorded (`cli-config.json`) |

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
| C9 | A desktop `state.vscdb` from 3.x | Reading the desktop app |

## Decisions

| ADR | Decision | Rejected |
|---|---|---|
| [0037](../adrs/0037-read-cursor-from-its-chat-store-prompt-messages.md) | An OpenAIDR-owned reader of the CLI's chat store. A prompt message is a turn; Cursor's typed outcome is the status; one kind for the CLI and the desktop app | `adr-sensor`'s parser; the transcript JSONL; the protobuf steps as the only source; two kinds |
| [0038](../adrs/0038-cursor-usage-from-its-debug-log.md) | Usage, the entrypoint, client error times and MCP failures come from Cursor's debug log, placed by the conversation each turn names | Context fill as input tokens; the `stop` hook; print mode's stdout; estimating from text |

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
