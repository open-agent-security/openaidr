# OpenAIDR — Codex

*Implemented (2026-10-16). A third agent kind, `codex`, with an OpenAIDR-owned
reader (`readers/codex.py`, `readers/codex_log.py`). Extends
[Session collection](session-collection.md) and [Token usage](token-usage.md).
**The Claude Code reader is the reference design**: Codex writes one append-only
file per thread, as Claude Code writes one transcript per session, so the
reader follows it, never [Devin](devin-cli.md)'s one-store rebuild. Supersedes
the design of the unmerged reader in open-agent-security/openaidr#35 and reuses
its record handling. Takes the account reader from the `codex-sign-in-reader`
branch (ADR-0027). Decisions: ADR-0030 to ADR-0035.*

## At a glance

| | |
|---|---|
| Covers | Codex CLI in the terminal (`codex`), `codex exec`, and the IDE extension and Codex app, which drive the same agent through `codex app-server`. Sub-agent threads of all of them. One harness, one `CODEX_HOME` |
| Not covered | **Codex Cloud** tasks. They run in OpenAI's containers, so reading them needs a network client, which read-only sensing rules out |
| Reads | Under the data root: `sessions/**/rollout-*.jsonl` for messages, calls, outcomes and per-response tokens; the newest `logs_<n>.sqlite` (read-only) for MCP connections; `session_index.jsonl` for a thread's name; `auth.json` for the account |
| Data root | `$CODEX_HOME`, else `~/.codex`. `OPENAIDR_CODEX_ROOT` overrides it for the CLI; `default_readers(codex_root=…)` for callers. `archived_sessions/` is a second location |
| Backed by `adr-sensor` | No. Its Codex parser drops `custom_tool_call` (where `apply_patch` lives), marks every returned call successful and truncates bodies at 1,000 characters (measured: 619 of 4,205 calls read, all `ok`) |
| Platforms | Linux and macOS. Windows deferred (ADR-0013) |
| Main gaps | MCP connections under `codex exec` (it writes no log rows). Exit codes in pre-0.16x rollouts (text only). Skill attribution. Compaction trigger. Each has an entry in [Open facts](#open-facts) |

## Evidence

- **Binary and a real session through a mock model.** Codex CLI 0.163.0 from npm
  ran in an isolated `CODEX_HOME` against a local mock of the Responses API.
  The mock scripted every call a session can make: a command that succeeds, one
  that fails, an `rm -rf`, an `apply_patch`, two MCP calls (one `isError`), a web
  search and a spawned sub-agent. Other runs added a forbidden command, a
  read-only sandbox, approval, decline and cancel through `codex app-server`,
  six upstream errors, and manual and automatic compaction. MCP servers were a
  working stdio server, a stdio server whose binary is missing, and an HTTP
  server that refuses connections. Everything marked **Seen** comes from those
  files.
- **Source.** openai/codex at `rust-v0.163.0` (`83086188`, 2026-10-15). Marked
  **Source**, with the path under `codex-rs/`.
- **Real rollouts, earlier.** 242 rollouts on one machine
  ([Session collection](session-collection.md#codex-and-cursor-are-the-intended-next-kinds)),
  five measured for usage ([Token usage](token-usage.md#a-second-kind-codex)),
  and eight read by #35's reader with zero failures and every call accounted
  for. Marked **Measured**.
- **Not checked: a session against OpenAI's own model.** Real `rate_limits`,
  real cache-write counts, and how the hosted model orders search items are
  unconfirmed.

## The rollout

| | |
|---|---|
| Path | `<root>/sessions/YYYY/MM/DD/rollout-<YYYY-MM-DDTHH-MM-SS>-<uuidv7>.jsonl`, one file per thread, sub-agents included (Seen) |
| Envelope | One JSON object per line: `timestamp`, `ordinal` (monotonic per file), `type`, `payload` (Seen) |
| Writing | Append-only, newline-terminated (Source `rollout/src/recorder.rs`). Rollback starts a new file, `<thread>_<rollout>` (Source) |
| Compression | `.jsonl.zst` for files older than 7 days, behind a feature that is off by default (Source `rollout/src/compression.rs`). Reported, not read (C8) |
| History mode | `session_meta.history_mode`. **`paginated`** for every new thread in 0.163.0 (Seen). Older threads are legacy, with fewer outcome records (below) |

**Record types** (Seen unless marked):

| `type` | What the reader takes |
|---|---|
| `session_meta` | First line. `id`, `session_id` (the root thread), `parent_thread_id`, `timestamp`, `cwd`, `originator`, `cli_version`, `source`, `thread_source`, `agent_nickname`, `model_provider`, `base_instructions`, `history_mode`, `git {commit_hash, branch, repository_url}` |
| `turn_context` | Per turn: `turn_id`, `cwd`, `model`, `approval_policy`, `approvals_reviewer`, `sandbox_policy.type`, `effort` |
| `response_item` | `message` (`developer`, `user`, `assistant`), `reasoning`, `function_call`, `function_call_output`, `custom_tool_call`, `custom_tool_call_output`, `tool_search_call`, `tool_search_output`, `web_search_call`, `image_generation_call`. `local_shell_call` (Source) |
| `event_msg` | `item_completed` with a typed item ([Completion items](#completion-items)). Also `task_started`, `task_complete` (with `error` on failure), `turn_aborted`, `token_count` |
| `token_usage_record` | One per model response: `response_id`, `turn_id`, `usage` |
| `compacted` | One per compaction |

**What Codex never writes to the rollout** (Source `rollout/src/policy.rs`):
`ExecCommandBegin/End`, approval requests and decisions, `McpStartupUpdate`,
`McpStartupComplete`, hook events, `ModelReroute`, `Error`, `StreamError`. In
legacy mode `item_completed` is kept only for a few item types, while
`PatchApplyEnd` and `McpToolCallEnd` are kept. So a **legacy** rollout has
patch and MCP outcomes as records, and a command's exit code only in its output
text.

## Identity

| Level | Identity |
|---|---|
| Main session | `codex:<session_meta.id>`. A UUIDv7, unique per thread |
| Sub-agent session | `codex:<session_meta.session_id>:<session_meta.id>`. `session_id` names the **root** thread at every depth (Seen), so a run is one root and its descendants (ADR-0001's `<kind>:<parent>:<stem>`). Every turn and response is `is_sidechain` |
| Turn | `key` is `o<ordinal>`, else `l<line>` for a record with no `ordinal`; `position` is the index among turns |
| Span | `span_id(session_id, turn key, 0)`. Each call record is its own turn, so the index is always 0 |

- **A record is a turn.** Each `message` (user or assistant) and each call
  record is one turn. Outputs and completion items update a call; they never add
  a turn. This is #35's rule. Merging calls into the preceding message would give
  a call its neighbour's time and make one turn's content grow.
- **Outcomes join on `call_id`, within the thread.** An output or a completion
  item with the same id updates that call, and the span does not move. A
  `call_id` used twice, or two outputs for one call, withholds the result,
  status and duration rather than guessing a pair.
- **A completion with no call record is its own turn**, keyed by its record:
  an execution nested inside another tool has its own id. It is never attached
  to an outer call by position.
- **A record belongs to the turn it names** (`turn_id` on the event, or on the
  response item's metadata), and to the turn in force only when it names none.
  Codex completes a call late, after a newer turn began, and names its own
  turn (ADR-0035).
- **Child threads start from their own first line.** A child that inherits its
  parent's history marks where its own begins; copied parent records are not
  the child's.

## Completion items

`TurnItem` and `ExtensionItem` are closed enums in 0.163.0 (Source
`protocol/src/items.rs`, `ext/items/src/lib.rs`), and a paginated rollout keeps
every one (Source `rollout/src/policy.rs`). Each is read as a call or
deliberately not, and anything else is reported (ADR-0033).

| Item (`type`, or an `Extension`'s `kind`) | The call | Arguments | Result |
|---|---|---|---|
| `CommandExecution` | `shell` | `command` | `aggregated_output` |
| `FileChange` | `apply_patch` | `changes` | `stdout` |
| `McpToolCall` | its `server` and `tool` | its `arguments` | `result.content` |
| `DynamicToolCall` | its `tool`, with no class (the client defines it) | its `arguments` | `content_items` |
| `CollabAgentToolCall` | its `tool`; `wait` is `wait_agent` | — | — |
| `WebSearch`, `web.search` | `web_search` | `action` | `results`, kept whole as JSON (ADR-0035) |
| `ImageView` | `view_image` | `path` | — |
| `ImageGeneration`, `image_gen.generation` | `image_generation` | `revised_prompt` | `result` |
| `clock.sleep` | `sleep` | `duration_ms` | — |

The result is that one field and no other (ADR-0034). Present, even as `""`
or `[]`, it is what the call returned; missing or `null`, there is none.

Status is the item's own: `completed` `ok`, `failed` `error`, `declined`
`rejected` (`user-rejected`), `interrupted` `interrupted`. `WebSearch`,
`ImageView`, sleep and standalone web search carry no status; their completion
is the call succeeding.

**Not calls:** `UserMessage` (the initial prompt), `AgentMessage`, `Plan`,
`Reasoning`, `HookPrompt`, `SubAgentActivity`, `EnteredReviewMode`,
`ExitedReviewMode`, `ContextCompaction`, and `FunctionCallOutput`, an output
the client supplied as turn input, beside the `function_call_output` response
item that is read.

## Session fields

| Field | Source |
|---|---|
| `started_at`, `last_activity_at` | Oldest and newest record `timestamp` |
| `model` | The newest `turn_context.model` |
| `working_directory` | `session_meta.cwd` |
| `machine`, `user` | The reading host and user, as for the other kinds |
| `agent_version` | `session_meta.cli_version` |
| `entrypoint` | `session_meta.source`: `cli` (terminal), `exec`, `vscode` (app-server, the IDE extension and Codex app), `mcp` (Codex as an MCP server). A child's `source` is an object, so its entrypoint comes from the `originator` it shares with the root: `codex_exec` is `exec`, `codex_cli_rs` is `cli`, any other is `vscode` (C3) |
| `git_branch` | `session_meta.git.branch` |
| `title` | The newest `thread_name` for the thread in `session_index.jsonl`, written only when a person names a thread (Source `rollout/src/session_index.rs`) |
| `generated_title` | `None`. `state_5.sqlite` holds one, but this reader does not open the state database |
| `initial_prompt` | The first `UserMessage` item; in legacy mode the first `user_message` event. Never a `user` message holding `<environment_context>` or another injected block |
| `turns` | Above. `permission_mode` on each turn is `<approval_policy>; <sandbox_policy.type>` from the `turn_context` in force, e.g. `never; workspace-write` |
| `context_items` | `base_instructions` (source `system`); `developer` messages (`system`); injected `user` messages (`injected`); each `tool_search_output` namespace's description, which is that MCP server's own instructions (`mcp_instructions`, one part per server) |
| `compactions` | One per `compacted` record. `trigger` `unknown`; `pre_tokens` from its `latest_token_usage_record` input (C6). `compactions_recorded` is true |
| `provider_refusals` | `task_complete.error` whose `codex_error_info` is `cyber_policy`, `bio_policy`, `misalignment_policy_violation` or `invalid_prompt`, or whose message says the response stopped for `content_filter`. `category` is that code |
| `subagents_recorded` | True |

**A client error is a turn with role `error`.** `task_complete.error {message,
codex_error_info}` is the client recording that a turn failed (Seen for usage
limit, quota, server error, context overflow, content filter and invalid
prompt). It becomes a call-free turn with role `error` and the message as its
text, at the record's time. This is the counterpart of the API-error message
Claude Code writes into its own transcript. A consumer can then read every kind's
client errors in one place. `codex_error_info` is a closed vocabulary (Source
`protocol/src/codex_error_info.rs`): `usage_limit_exceeded`,
`rate_limit_exceeded`, `server_overloaded`, `too_many_denials`,
`context_window_exceeded`, `internal_server_error`, `unauthorized` and others.

## Tool calls

| Field | Source |
|---|---|
| `tool_name`, `mcp_server` | [Tool names](#tool-names) |
| `arguments` | `function_call.arguments` parsed from JSON text; `custom_tool_call.input` as `{"input": …}`. Non-object JSON gives `{"raw": …}`, reported |
| `result`, `result_size` | The output's `output`: a string, or a list of content blocks kept whole as JSON text. Never elided. With no output, the item's own result field ([Completion items](#completion-items)) |
| `error_text` | When `status` is `error`: the item's `error`, else its `stderr`, else its `aggregated_output`, else the output text |
| `truncated` | True when the result carries Codex's own cut mark, `…N tokens truncated…` or `…N chars truncated…` (Source `utils/string/src/truncate.rs`). C2 |
| `provider_call_id` | `call_id` |
| `status`, `denial_kind` | [Status](#status) |
| `working_directory` | `CommandExecution.cwd` (a `file://` URI, made a path); else the call's `workdir` against the turn's `cwd`; else the turn's `cwd` |
| `exit_code` | `CommandExecution.exit_code`. Else the `Process exited with code N` or `Exit code: N` line of the header Codex writes above `Output:`, never a line below it. Legacy rollouts have only the header |
| `duration_ms` | The item's `duration`, else `completed_at_ms − started_at_ms`. None when the call has no item |
| `transport` | For an MCP call, its server's transport from the placed connection |
| `tool_class`, `canonical_arguments` | The kind's tables ([below](#tool-class-and-canonical-arguments)) |
| `attributed_skill` | `None`. Codex lists skills in its instructions, and the model opens one with the shell, so no record names the skill a call served (C12) |
| `attributed_plugin` | The item's `plugin_id`, where one is written (C10) |

### Tool names

The first rule that applies (ADR-0033):

| # | Record | `mcp_server`, `tool_name` |
|---|---|---|
| 1 | An `McpToolCall` item | Its `server` and `tool`, as the server is configured |
| 2 | A `CollabAgentToolCall` or `Extension` item | `None` and the item's tool. A collaboration call's namespace is configurable, so the item decides |
| 3 | A call with `namespace` `mcp__<server>` | The server, and `name` (Seen: MCP tools are deferred behind `tool_search` and called this way). Codex sanitises punctuation in a server's name to `_`, so a connection the log database names breaks the tie |
| 3 | A call in Codex's own namespaces: `clock.sleep`, `web.run`, `image_gen.imagegen` | `None` and `sleep`, `web_search`, `image_generation` |
| 3 | A call in `multi_agent_v1` or `collaboration` | `None` and `name` |
| 3 | A call in any other namespace | `None` and `<namespace>.<name>` |
| 4 | A flat `mcp__<server>__<tool>` | Split by Claude Code's rule |
| 4 | Any other name | The name, with no server |

A V2 collaboration call in a configured namespace, refused before it ran, has
no item, so it keeps `<namespace>.<name>`. Matching its bare name in any
namespace would misname a dynamic tool of the same name.

### Status

From Codex's own records, never from what a command printed:

| Record | `status` | `denial_kind` |
|---|---|---|
| `CommandExecution` `completed`, exit code 0; `FileChange` `completed`; `McpToolCall` `completed` and not `isError`; `tool_search_output` `completed` | `ok` | — |
| `CommandExecution` `failed` or a non-zero exit; `McpToolCall` `failed` or `isError`; an output header with a non-zero exit | `error` | — |
| `FileChange` `declined` | `rejected` | `user-rejected` |
| Output that is Codex's rejection envelope, `exec_command failed: CreateProcess { message: "Rejected(…)` at its start, carrying `rejected by user` | `rejected` | `user-rejected` |
| The same envelope carrying `automatic approval review denied the action` (the Guardian reviewer) | `rejected` | `automode-blocked` |
| The same envelope carrying `rejected by configuration` (a hook), `forbidden by policy`, `policy forbids commands starting with` (an exec-policy rule), `rm -f style commands are not permitted` (a built-in rule), or `approval policy is Never; reject command` | `rejected` | `permission-rule` |
| The envelope with any other reason | `rejected` | `None` |
| Output `approval policy is Never; reject command` | `rejected` | `permission-rule` |
| Output `patch rejected by user` | `rejected` | `user-rejected` |
| Output `aborted by user` | `interrupted` | — |
| A Code Mode cell (`custom_tool_call` `exec`) whose output opens `Script completed`, `Script failed` or `Script terminated` (ADR-0036) | `ok`, `error` or `interrupted` | — |
| A Code Mode cell whose output opens `Script running with cell ID` | `pending` | — |
| A `tool_search_call` or `web_search_call` whose own status is `completed` or `failed` | `ok` or `error` | — |
| An output with no item and no header | `unknown` | — |
| No output and no item, after the Codex turn ended | `unknown` | — |
| No output and no item, while the Codex turn runs | `pending` | — |

- **Denials leave no item.** A refused, declined or policy-blocked command
  produces only its `function_call_output` (Seen for each case above). The
  envelope and its reasons are Codex's literals (Source
  `core/src/tools/approvals.rs`, `core/src/exec_policy.rs`), anchored at the
  start of the output, where a command's own output never appears.
- **A sandbox denial is not a denial.** The command ran and failed (`Read-only
  file system`, exit 1). It is `error`.
- **A late item after an interrupt** (`failed`, exit −1) does not turn an
  `interrupted` call into `error`.

## Model responses

**One response per `token_usage_record.response_id`** (ADR-0014). Measured
earlier at 73 records with 73 distinct ids; Seen at 11 of 11 and 9 of 9.
Compaction requests have one too, and count. A child thread's responses are in
its own rollout, and its parent's totals exclude them (Seen), so nothing counts
twice.

**Never read `token_count` as usage.** It repeats after compaction with a
synthetic total, leaves out compaction requests, and reports a context-full
estimate with zero input after an overflow (Seen).

| `ModelResponse` | Source |
|---|---|
| `response_id` | `response_id` |
| `request_id` | `None` |
| `model` | The `turn_context.model` of the record's `turn_id` |
| `occurred_at` | The record's `timestamp` |
| `final` | True. Codex writes the record when the response completes |
| `is_sidechain` | True in a sub-agent session |
| `usage.input_tokens` | `input_tokens − cached_input_tokens`. **Codex's input includes its cached tokens** (the Responses API's meaning, and Measured: cached never exceeds input). This model's input excludes cache reads, as Claude Code's and Devin's do |
| `usage.cache_read_input_tokens` | `cached_input_tokens` |
| `usage.cache_creation_input_tokens` | `cache_write_input_tokens` when above 0, else `None`. Codex writes 0 when the provider reports nothing (Source `codex-api/src/sse/responses.rs`), so a 0 is not a measurement |
| `usage.output_tokens` | `output_tokens`, which includes reasoning |
| `usage.reasoning_tokens` | `reasoning_output_tokens` |
| `context_window` | **New, additive.** `model_context_window` from the turn's `task_started` |
| 5-minute and 1-hour cache split, web request counts, tier, speed, region | `None`: not recorded |

`Turn.response_id`: an assistant turn takes the `response_id` of the next
`token_usage_record` with the same `turn_id` (ADR-0031). A thread has one
response in flight at a time, so the records between two usage records belong
to the later one.

## MCP connections: the log database

The rollout records MCP calls, not connections. Connection facts are in the
newest `<root>/logs_<n>.sqlite`, table `logs(ts, ts_nanos, level, target,
feedback_log_body, thread_id, process_uuid, …)`. Its default filter keeps
`rmcp` and `codex_rmcp_client` at INFO (Source `state/src/log_db.rs`). Lines
from the app-server run, shortened (Seen):

```
INFO rmcp::service  app_server.request{rpc.method="thread/start" rpc.request_id=2 app_server.connection_id=1 …}:new{server_name=probe}: Service initialized as client peer_info=Some(… server_info: Some(Implementation { name: "probe-mcp-server", version: "9.9.9" … }))
WARN codex_mcp::rmcp_client  app_server.request{… rpc.request_id=2 app_server.connection_id=1 …}: MCP server startup failed server_name="broken_stdio" error=No such file or directory (os error 2)
WARN codex_mcp::rmcp_client  app_server.request{…}: MCP server startup failed server_name="broken_http" error=handshaking with MCP server failed: … StreamableHttpClientAdapter … error sending request …
```

**`codex exec` writes no rows** (Seen: 0 rows after an exec run, 627 after an
app-server run). Only the terminal UI and app-server open the database. An
exec session's state is `no_log_for_session`.

**Placement by request, never by time** (ADR-0032). Startup lines carry
`process_uuid` and no `thread_id`. Codex starts a thread's servers inside the
`thread/start` request that creates it, and every line it logs there carries
that request's span. Another line in the same span carries the thread's id.

1. A startup line belongs to the thread whose own tagged line shares its
   request span (connection id and request id).
2. A line logged in no request is placed only where its process served one
   thread. Otherwise the thread's state is `placement_ambiguous`.
3. Two lines that disagree about one server leave `connected` unknown.

A slow failure is logged after the thread's first tagged line (Seen: 1.3
seconds later), so a time window would misplace it.

| `MCPConnection` | Source |
|---|---|
| `server` | `server_name` |
| `transport` | `streamable-http` where the failure names Codex's HTTP client. Otherwise `None` (C5) |
| `endpoint` | `None`. Never taken from error text |
| `advertised_name`, `advertised_version` | `server_info` on the `Service initialized` line. Codex logs the handshake at the default level, where Devin does not |
| `connected` | True on `Service initialized`, false on `MCP server startup failed` |
| `failure_category` | From the error, first match wins: `policy`; `spawn` (`No such file or directory`, `os error 2`); `auth` (401, 403, `Unauthorized`, OAuth); `timeout`; `network` (`error sending request`, `Connection refused`, DNS); `http_status`; `protocol` (`handshaking with MCP server failed` with no cause above); else `unknown` |
| `failure_detail` | The error text (`LOCAL_ONLY`) |
| `duration_ms` | `None` (C5) |

The reader reads only rows whose `target` is `rmcp::service` or
`codex_mcp::rmcp_client`, and finds a thread's request spans by its own tagged
rows. No other log body is read.

| `mcp_log_state` | Meaning for `codex` |
|---|---|
| `applied` | Placed |
| `no_log_root` | No `logs_<n>.sqlite` |
| `log_root_unreadable` | The database could not be opened |
| `no_log_for_session` | No row names the thread. Always the case for `codex exec` |
| `log_discovery_incomplete` | A read failed part-way |
| `placement_ambiguous` | A line logged in no request, in a process that served several threads |

## Who is signed in

`signed_in("codex")` reads `auth.json` under the data root (ADR-0027; the
reader on the `codex-sign-in-reader` branch). With `auth_mode` ChatGPT, the
account is the `id_token` claim `chatgpt_user_id` (else `user_id`). The email
is the top-level `email` claim, else the profile's, and the plan is
`chatgpt_plan_type`. The token's signature is not checked. Signed in with an
API key there is no person, so the answer is `None`. With
`cli_auth_credentials_store = "keyring"` there is no file, so the answer is
also `None`. `sign_in_records("codex")` names `auth.json` and `config.toml`.

## Growing sessions

The Claude Code reader's discipline (ADR-0011, ADR-0029, ADR-0030):

| Rule | Detail |
|---|---|
| Committed unit | A newline-terminated record. A torn last line waits for its newline |
| What is held | Per file: device, inode, offset, line count and the projection. In memory, never persisted, under the 128-file and 16 MiB LRU bound |
| `locations()` | `sessions/` and `archived_sessions/` |
| `collect_file(path)` | A rollout under those locations: read from the offset, and return the file's session on every read. The log database and `session_index.jsonl` are read again with it, as Claude Code's connection logs are. Anything else: nothing |
| Replaced or shorter file | Rebuilt cold |
| Malformed complete record | That file's session is withheld and the path and line reported, never the record's content. An unread record could hide a second claim on a `call_id`. A record nested more than 256 containers deep is malformed: it decodes, but anything that walks it again would fail outside the read. Codex writes with serde_json's limit of 128 (ADR-0033) |
| Malformed values inside a record | JSON `arguments` that do not decode, or nest past 256, are kept as written under `raw`. A timestamp that does not parse, or does not fit UTC, is no instant. A `session_index.jsonl` line that does not decode is skipped. A log row with an out-of-range time is skipped |
| Between reads | Calls update in place by span. Responses replace by `(session, response_id)`. No turn disappears |

## Tool class and canonical arguments

| Class | `codex` | Canonical keys (`codex` source key) |
|---|---|---|
| `shell` | `exec_command`, `shell`, `shell_command`, `local_shell` | `command`: `cmd` as written. For an argv form, the script of `bash -lc <script>` or `sh -c <script>`, else the argv joined. **Always the script, never the wrapper**, so a consumer sees `curl …` and not `bash -lc …` |
| `shell_control` | `write_stdin` | — |
| `script` | `exec`, Code Mode's cell. Each tool its script runs is its own call from its own completion item (ADR-0036) | — |
| `file_read` | `view_image` | `path` (`path`) |
| `file_edit` | `apply_patch` | `edits` (**new**, below). A patch that touches one file also gets `path` and `new_text` |
| `web_search` | `web_search` | — |
| `delegate` | `spawn_agent` | `agent`: `agent_type`, else `default`, the role Codex itself applies (Source `core/src/agent/role.rs`; C7) |
| `mcp` | Any call with an `mcp_server` | — |
| `other` | `send_input`, `resume_agent`, `wait_agent`, `close_agent`, `send_message`, `followup_task`, `interrupt_agent`, `list_agents`, `tool_search`, `update_plan`, `request_user_input`, `list_mcp_resources`, `list_mcp_resource_templates`, `read_mcp_resource`, `get_goal`, `create_goal`, `update_goal`, `image_generation`, `sleep`. The collaboration tools reach an agent that already exists, as Claude Code's `SendMessage` does | — |

Codex has no file-read, file-search, file-write or web-fetch tool. It reads,
searches and fetches through the shell, so those calls are `shell`.

**`edits` is a new canonical key** for a patch tool. It is a list of `{path,
kind, new_text}`, one per file. `kind` is `add`, `update` or `delete`. `new_text`
holds the added lines, taken from the patch's own grammar (`*** Add File:`,
`*** Update File:`, `*** Move to:`, `+` lines), else from a `changes` argument.
One canonical `path` cannot describe a patch that touches three files. Devin's
`apply_patch` has the same shape and could fill the same key; that is not done
here.

## Model additions

All additive, with defaults.

| Addition | Why |
|---|---|
| `ModelResponse.context_window` | Codex records the model's window per turn. Elsewhere a consumer has to keep a table by model name, and that table goes stale |
| Canonical key `edits` | Above |
| Turn role `error` | A client-recorded failure, as above. Documented, since `role` is otherwise a speaker |
| `toolnames.split_tool_name("codex", …)` | The namespace rule, then the flat `mcp__` rule |

## Coverage by agent kind

**Recorded** means a confirmed source. **Partial** means a source with a named
loss. **Unconfirmed** means the format has the field but no session has shown
it. **Absent** means there is no source.

| Field | `claude-code` | `devin` | `codex` |
|---|---|---|---|
| Session identity, start, last activity | Recorded | Recorded (seconds) | Recorded |
| Model, per response | Recorded | Recorded | Recorded (per turn) |
| Working directory | Recorded | Recorded | Recorded |
| Agent version | Recorded | Recorded | Recorded |
| Entrypoint | Recorded | Partial | Recorded (C3 for values) |
| Git branch | Recorded | Absent | Recorded |
| Initial prompt | Recorded | Recorded | Recorded |
| Title | Recorded | Partial | Partial: person-set names only |
| Context items | Recorded | Unconfirmed | Recorded; MCP instructions per server |
| Compactions | Recorded | Unconfirmed | Recorded; trigger absent (C6) |
| Provider refusals | Recorded | Absent | Recorded (error codes) |
| Client errors (quota, throttle, overflow) | Recorded (as turns) | Absent | Recorded (role `error`) |
| MCP connections | Recorded | Partial: no advertised identity | Partial: none under `codex exec`; no endpoint or duration |
| Responses and tokens | Recorded | Recorded | Recorded; input normalised |
| Context window | Absent | Absent | Recorded |
| Turn time | Recorded | Recorded | Recorded |
| Sub-agent sessions | Recorded | Unconfirmed | Recorded |
| Permission mode, per turn | Recorded | Absent | Recorded |
| Tool name, MCP server, arguments | Recorded | Recorded | Recorded |
| Tool class, canonical arguments | Recorded | Recorded; `apply_patch` none | Recorded; patches as `edits` |
| Status and denial | Recorded | Unconfirmed | Recorded (paginated); denials from the envelope |
| Exit code | Absent | Unconfirmed | Recorded; else from the output header |
| Per-call working directory | Recorded | Unconfirmed | Recorded |
| Duration | Recorded | Absent | Recorded (paginated) |
| Attributed skill, plugin | Recorded | Absent | Skill absent (C12); plugin unconfirmed (C10) |
| MCP transport per call | Recorded | Recorded (by server) | Partial (by server, where placed) |
| Signed-in account | Recorded | Via `devin auth status` | Recorded (ChatGPT sign-in) |

## Open facts

**Fixtures are synthetic. Real rollouts are never committed.** The probe's
files become the shape of the fixtures, not their content. Before relying on a
field, run one session against OpenAI's own model that does what the probe did.
Then re-measure the earlier corpus with the new reader.

| # | Question | Decides |
|---|---|---|
| C1 | A real-model session: `rate_limits`, cache-write counts, search ordering, and that input still includes cached tokens | Usage normalisation |
| C2 | The legacy rollout shape on disk, from a pre-paginated version: the output header, and how truncation is marked | `exit_code`, `truncated`, status in legacy mode |
| C3 | `source` and `originator` for the terminal UI, the IDE extension and the Codex app | `entrypoint` |
| C4 | What a Guardian denial's output carries in a real session, and a `too_many_denials` turn end to end | `automode-blocked` |
| C5 | Whether any log line names a stdio launch, and the connection's timing; whether `logs_2.sqlite` prunes rows, and how soon | `transport`, `duration_ms`, retention |
| C6 | Whether anything says a compaction was automatic or manual | `Compaction.trigger` |
| C7 | `spawn_agent`'s role argument in a real session | Canonical `agent` |
| C8 | Whether compressed rollouts appear by default in a later release | Reading `.jsonl.zst`, which needs a zstd dependency |
| C9 | The macOS layout, against the Linux one | Platform coverage |
| C10 | Where an MCP call served by a plugin names the plugin | `attributed_plugin` |
| C11 | `state_5.sqlite` `threads.creator_account_id` when signed in | A per-thread account, should one be needed |
| C12 | Whether any record names a skill the model used | `attributed_skill` |

## Decisions

| ADR | Decision | Rejected |
|---|---|---|
| [0030](../adrs/0030-read-codex-rollouts-on-the-claude-code-readers-design.md) | An OpenAIDR-owned reader on the Claude Code reader's design. A record is a turn. Outcomes come only from Codex's records, refusals from its envelope. A sub-agent is named by its root. A patch's files are `edits` | `adr-sensor`'s parser; the Devin reader's rebuild-and-diff; `unknown` for every unpaired output; naming by the immediate parent |
| [0031](../adrs/0031-codex-usage-from-token-usage-records-with-cached-input-apart.md) | One response per `token_usage_record`, cached input taken out of input, a 0 cache write is `None`, the window from `task_started` | `token_count`; input as recorded |
| [0032](../adrs/0032-place-codex-mcp-connections-by-the-request-that-created-the-thread.md) | MCP connections placed by the request that created the thread; none for `codex exec` | Placement by time; turning on logging |
| [0033](../adrs/0033-read-codex-against-its-own-closed-vocabularies.md) | Codex 0.163.0's closed item vocabularies as one table; a call named by its most specific record; one nesting bound on input | Any item with an id as a call; naming collaboration calls by namespace; per-site exception catches |
| [0034](../adrs/0034-a-codex-items-result-is-its-own-field-empty-included.md) | Each item's result is the one field the table names; empty is a result, absent is none | Trying fields in turn; empty as no result |
| [0035](../adrs/0035-a-codex-record-belongs-to-the-turn-it-names.md) | A record belongs to the turn it names; a web search's results are kept whole | The turn in force for every record; joining text blocks in `results` |
| [0036](../adrs/0036-a-code-mode-cell-is-a-script-and-its-tools-are-their-own-calls.md) | A Code Mode cell is classed `script` and its status is its own status line; the tools it runs stay their own calls | Dropping the cell; folding nested calls into it; `other`; joining by position |
| [0039](../adrs/0039-preserve-native-client-metadata-without-changing-runtime-identity.md) | Append local-only `Session.originator` metadata; kind, entrypoint, spans and public descriptors stay unchanged | Create an agent kind for each client name; replace the entrypoint with the originator; interpret known clients inside collection; add arbitrary originators to the public descriptor |

## Non-goals

- Codex Cloud, or any network collection.
- Configuration (MCP servers, skills, plugins, rules, hooks as configured);
  that is another component's concern.
- Running Codex, its `resume` or its app-server.
- OTel export (`[otel]`), which is off by default.
- Pricing tokens (ADR-0015).
- Windows (ADR-0013).

## References

- [Session collection](session-collection.md) · [Token usage](token-usage.md)
- ADR-0001 identity · ADR-0011 cursors · ADR-0013 Windows · ADR-0014 usage per
  response · ADR-0015 tokens as recorded · ADR-0021 tool class · ADR-0022
  absence flags · ADR-0023 locations · ADR-0027 accounts · ADR-0029 cache
  bound · ADR-0030 to ADR-0035 Codex
- openai/codex `rust-v0.163.0`: `rollout/src/policy.rs`, `rollout/src/recorder.rs`,
  `rollout/src/session_index.rs`, `protocol/src/protocol.rs`,
  `protocol/src/codex_error_info.rs`, `codex-api/src/sse/responses.rs`,
  `core/src/tools/approvals.rs`, `core/src/exec_policy.rs`,
  `state/src/log_db.rs`, `login/src/token_data.rs`, `core/src/agent/role.rs`,
  `utils/string/src/truncate.rs`
- Prior work: open-agent-security/openaidr#35 (reader), the
  `codex-sign-in-reader` branch (account)

## Native client metadata

`Session.originator` preserves a nonempty string from `session_meta.originator`.
Missing, empty and non-string values become `None`.
The field is appended to the model with a default.
It does not change positional arguments, runtime kind, session IDs or span IDs.
Cold and incremental reads preserve the same value.

The string describes the recorded client; it authenticates nothing.
Unknown strings remain recorded strings and establish no capability.
The field stays outside `public_descriptor()` and default JSON rendering.
Consumers interpret a native client name independently of collection.

An inspected local desktop rollout records `originator: codex_work_desktop` and `source: vscode`.
It contains messages and response usage, with no tool calls.
The test fixture reconstructs the envelope without private transcript content.
Synthetic tool scenarios test parser compatibility, not desktop hook dispatch.

Decision: [ADR-0039](../adrs/0039-preserve-native-client-metadata-without-changing-runtime-identity.md).
