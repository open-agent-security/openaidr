# OpenAIDR — Devin CLI

*Implemented (2026-10-09). A second agent kind, `devin`, with an
OpenAIDR-owned reader (`readers/devin_cli.py`, `readers/devin_cli_log.py`).
Extends [Session collection](session-collection.md) and
[Token usage](token-usage.md). Decisions: ADR-0018 to ADR-0022, and ADR-0024
for the kind's name: `devin` rather than `devin-cli`, because Devin CLI and
Devin Desktop's local agent are one agent writing one store.*

## At a glance

| | |
|---|---|
| Covers | Devin CLI (`devin`), Devin Local (Devin Desktop's default agent), and editors driving it over ACP (Agent Client Protocol): one agent harness, one local state |
| Not covered | **Devin Cloud**: remote sessions, so reading them needs a network client, which read-only sensing rules out. **Cascade**: Devin Desktop's legacy agent, with an encrypted store of its own |
| Reads | Under the data root: `sessions.db` (SQLite, read-only) for messages, tool calls, results and per-request tokens; `session_locks/<session>.lock` for the PID that held each session; `logs/devin_<ts>_<pid>.log[.gz]` for MCP connections, the build and the entrypoint |
| Data root | `$XDG_DATA_HOME/devin/cli`, else `~/.local/share/devin/cli`. `OPENAIDR_DEVIN_ROOT` overrides it for the CLI; `default_readers(devin_root=…)` for callers |
| Backed by `adr-sensor` | No. Upstream has no Devin parser. Same `Reader` and `IncrementalReader` contracts as other readers |
| Platforms | Linux and macOS. Windows deferred (ADR-0013) |
| Main gaps | Per-turn permission mode; who blocked a `Blocked` call; a call's skill or plugin; MCP server identity (debug level only); REPL vs `-p`; durations. Each has a named source to confirm in [Open facts](#open-facts) |

## Evidence

- **Binary.** Devin CLI 3000.11.3 (`x86_64-unknown-linux`, commit
  `9c803229faa4`), checksum-verified against the vendor manifest. It carries
  its store's SQL migrations (`V1__initial_schema` to `V17__subagent_heads`),
  the field names it serialises, its tool input schemas and its user-facing
  literals.
- **Live run.** ACP mode in an isolated `$HOME`, which connects MCP servers
  without sign-in, against three test servers: a working stdio server, one with
  a missing binary, and an HTTP server that refuses connections. It wrote a real
  store, real run logs and real session locks. **The run-log parser and the
  lock join are validated against those files**: every server, transport, URL,
  outcome and failure category, the build version and the ACP entrypoint came
  out as written, including a server the ACP client registered that appears in
  no config file.
- **Three open-source parsers** of real stores: agentsview, observer and
  codeburn. They confirm what a signed-in session writes: `metadata.created_at`
  is the call time, retries share a `request_id`, `compactor` and
  `cache_keepalive` requests are real calls, and `prompt_history` holds typed
  prompts.
- **Not checked: a signed-in session.** No model call was made, so the store
  side is tested against synthetic stores built from the above. Fields a
  conversation writes and no parser confirms are marked **(unconfirmed)**.

## The session store

| | |
|---|---|
| Path | `<data>/sessions.db`. The program honours a `CHISEL_SESSION_DB` override; the reader takes its root instead |
| Journal | WAL. Opened `mode=ro`; one read transaction per pass sees committed rows only (ADR-0018) |
| Schema | Migrations in `refinery_schema_history`. Built against V1 to V17; a newer one is read for the known columns and reported |
| `sessions` | `id` (slug), `working_directory`, `workspace_dirs`, `backend_type`, `model`, `agent_mode` (the current permission mode; renamed from `permission_mode` in V13), `created_at`, `last_activity_at` (epoch seconds), `title`, `main_chain_id`, `hidden`, `metadata`, `cogs_json`, `shell_last_seen_index` |
| `message_nodes` | `row_id` (autoincrement), `session_id`, `node_id` (unique per session), `parent_node_id`, `chat_message` (JSON), `created_at` (epoch seconds), `metadata` (JSON: `summarized_from`, `num_tokens_preceding`) |
| `subagent_heads` | `session_id`, `agent_id`, `chain_node_id`, `updated_at`. Each sub-agent chain is "its own tree in the forest, unreachable from `sessions.main_chain_id`" (the migration's own comment) |
| `prompt_history` | `content`, `timestamp`, `session_id`, `is_shell`: what a person typed, shell-mode lines marked |
| `tool_call_state` | The final ACP `ToolCall` and `ToolCallUpdate` JSON per call, kept for resume. Only `ToolCall.kind` is read: a call the table does not name is `other` when it has one (ADR-0025) |

**`chat_message` fields**, by their names in the binary:

- `role` (`user`, `assistant` or `agent`, `system`, `tool`), `content` (a string
  or a list of parts), `thinking`.
- `tool_calls[] {id, name, arguments, index, kind}`, or the OpenAI shape
  `{id, function: {name, arguments}}`; `arguments` as JSON text or an object.
  `tool_call_id` on a result.
- `metadata` (16 fields): `request_id`, `generation_model`, `finish_reason`,
  `is_user_input`, `metrics {input_tokens, output_tokens, cache_read_tokens,
  cache_creation_tokens, ttft_ms, total_time_ms}`, `created_at` (the call
  time), `started_generation_at`, `num_tokens`, `loaded_tool_names`,
  `extensions` (a result's `ToolResult` is under
  `extensions["chisel/tool_result_meta"]`), and billing and telemetry fields
  that are not read.

**Failures** are a `ReaderFailure`, never raised:

| Condition | Result |
|---|---|
| No data root, or no store | No sessions, and no failure |
| Unreadable root or store | Failure, with the path |
| Locked past a 2-second timeout | Failure: store busy |
| A required table or column missing | Failure: unsupported schema, naming what is missing |
| A migration newer than V17 | Read, and reported |
| A `chat_message` that is not a JSON object | That node kept with an empty message -- no role, so no turn or context item, but its `parent_node_id` still places it in the tree -- and reported |
| `arguments` that are not an object | `{}`, and reported |

## Identity

| Level | Identity |
|---|---|
| Main session | `devin:<sessions.id>`. The slug is unique in the store |
| Sub-agent session | `devin:<sessions.id>:<subagent_heads.agent_id>` (ADR-0001's `<kind>:<parent>:<stem>`). All its turns and responses are `is_sidechain` |
| Turn | `key` is the `node_id`; `position` is the index among turns along the chain |
| Span | `span_id(session_id, node_id, call_index)`, the index being the call's position in its node's `tool_calls` |

- **The conversation is a tree.** `main_chain_id` is the current leaf; turns
  are the path from it to the root. A session with no `main_chain_id` uses its
  newest main-tree node. Other branches are not turns, but their requests
  count as usage (ADR-0019).
- **One rule says which session a node is.** A node belongs to the session
  whose declared chain holds its nearest ancestor-or-self; the main chain
  claims first, then each sub-agent's head chain; a node below no chain is the
  main session's. Each session's turns, usage and times come from the nodes it
  owns and nothing else (ADR-0019).
- **Spans are never reused, and turns can vanish.** A regenerated turn leaves
  the chain and its spans stop being reported. `node_id` is unique per session.
- **Hidden sessions** (`hidden = 1`) are internal helpers such as the
  summarizer, with no recorded parent, person or owner. Not read (M7).

## Session fields

| Field | Source |
|---|---|
| `started_at` | Main session: `sessions.created_at`. Sub-agent: the oldest time among the nodes it owns, since it can start long after the row |
| `last_activity_at` | Main session: the latest of `sessions.last_activity_at` and its own nodes' times. Sub-agent: the newest time among the nodes it owns, since the parent can outlast it. The `--since` window and log placement use a row's span across all its sessions |
| `model` | The newest response's `generation_model`; else `sessions.model`, which is often empty or an alias |
| `working_directory` | `sessions.working_directory` |
| `machine`, `user` | The reading host and user, as for `claude-code` |
| `title`, `generated_title` | `sessions.title` goes to `generated_title` only, until it is known whether a person set it (M8) |
| `initial_prompt` | The first `user` chain node that is a person's; else the session's first non-shell `prompt_history` row |
| `turns` | Chain nodes with role `assistant`, or role `user` unless `is_user_input` is false. `tool` nodes are results |
| `context_items` | `system` chain nodes (source `system`), and `user` nodes with `is_user_input` false (source `injected`, e.g. a `cache_keepalive` "continue"); `occurred_at` is the node's time |
| `compactions` | One per chain node whose node `metadata` has `summarized_from`: `pre_tokens` from `num_tokens_preceding`, `trigger` `unknown`, `occurred_at` from the node. `compactions_recorded` is true |
| `agent_version` | `version=` on the placed run log's `init_cli` line |
| `entrypoint` | From the placed run log: `acp` when the process ran as an ACP server (`run_acp_server`), `cli` when it ran the terminal (`run_repl_mode`, which covers both the REPL and `-p`; M14) |
| `git_branch` | `None`. The store records none. The `git_branch` in the binary is a skill's source context (`SkillGitContext`), not the session's |
| `permission_mode` | `None` on every turn. `agent_mode` holds only the current mode (`normal`, `accept-edits`, `smart`, `bypass`, `autonomous`), and stamping it on past turns would state what the store does not know (M12) |
| `provider_refusals` | Empty. The program has a refusal path ("provider content filter") and a fallback, but where a session records them is unconfirmed (M9) |
| `subagents_recorded` | True where the store has `subagent_heads` |
| Time of a turn or response | The message's own `metadata.created_at` (epoch seconds or milliseconds, or RFC 3339), else the row's whole second |

## Tool calls

| Field | Source |
|---|---|
| `tool_name`, `mcp_server` | `tool_calls[].name`, split by Claude Code's `mcp__<server>__<tool>` rule, which Devin's own hook matchers use. **`mcp_call_tool`** names its server and tool in its arguments (`server_name`, `tool_name`, `arguments`, from its input schema), so the reader recovers both; `arguments` stays as recorded |
| `arguments` | Parsed from JSON text. Non-object JSON gives `{}`, reported |
| `result`, `result_size`, `error_text` | The `tool` node whose `tool_call_id` matches, on the chain first, else anywhere in the session's tree. `error_text` is the body when the status is `error` |
| `truncated` | True when the result records `overflow_metadata` (unconfirmed) |
| `provider_call_id` | `tool_calls[].id` |
| `status`, `denial_kind` | [Status](#status) |
| `working_directory`, `exit_code` | The result record's `cwd` and `exit_code` (unconfirmed placement) |
| `duration_ms` | `None`. Node times are whole seconds; message call times may support it (M15) |
| `transport` | For an MCP call, its server's transport from the placed connection |
| `tool_class`, `canonical_arguments` | The kind's tables (ADR-0021; [Model additions](#model-additions)) |
| `attributed_skill`, `attributed_plugin` | `None`. A call record carries `tool_provenance`, which may name them (M13) |

### Status

From the result's own record (`ToolResult {success, failure_reason}`), never
from its text:

| Record | `status` | `denial_kind` |
|---|---|---|
| `success` true | `ok` | — |
| `failure_reason` `Rejected` | `rejected` | `user-rejected` |
| `failure_reason` `Blocked` | `rejected` | `permission-rule` |
| `failure_reason` `ToolError`, `ValidationError` or `UnsearchedTool` | `error` | — |
| `success` false with no recognised reason, or no record | `unknown` | — |
| No result, at the chain's tip | `pending` | — |
| No result, and the chain moved on | `unknown` | — |

- `failure_reason` is read as a string or as a one-key object (an externally
  tagged enum).
- **A failure with no stated reason is `unknown`, never `error`**: reading a
  person's refusal as a tool's fault is the worst wrong answer this model can
  give.
- **`Blocked`** covers deny rules, built-in defaults, organisation policy and,
  in `smart` mode, the safety classifier. The record that says which is a
  `PermissionResolution {decision_source, …}` (M2); until it is read, all are
  `permission-rule`, never `automode-blocked`.
- **`interrupted`** is not emitted until a session shows how an interruption is
  recorded (M9).

## Model responses

- **One response per `metadata.request_id`, across the session's tree**
  (ADR-0019). Retries are sibling nodes with the same `request_id` and identical
  metrics, so they count once (ADR-0014). Regenerated turns have their own
  `request_id` and count. A node with metrics and no `request_id` is keyed
  `node-<node_id>`. `compactor` and `cache_keepalive` requests count: they were
  real calls.
- **A request belongs to the session that owns its node** (the ownership rule
  above), so no `request_id` counts in two sessions, and a sub-agent's
  abandoned regeneration is the sub-agent's.
- **Not read:** Devin's own billing figures (`committed_credit_cost`,
  `committed_acu_cost`, `cogs_json`) and the totals in `sessions.metadata`.
  Tokens are as recorded and never priced (ADR-0015).

| `ModelResponse` | Source |
|---|---|
| `response_id`, `request_id` | `request_id` |
| `model` | `generation_model` |
| `occurred_at` | The message's call time, else the node's second |
| `final` | True when `finish_reason` is recorded |
| `is_sidechain` | True in a sub-agent session |
| `usage.input_tokens` | `metrics.input_tokens`, already net of cache reads |
| `usage.cache_read_input_tokens` | `metrics.cache_read_tokens` |
| `usage.cache_creation_input_tokens` | `metrics.cache_creation_tokens`; null stays `None`, never zero |
| `usage.output_tokens` | `metrics.output_tokens` |
| 5-minute and 1-hour cache split, `reasoning_tokens`, web request counts, tier, speed, region | `None`: not recorded |

`Turn.response_id` is the same key on the assistant node that made the request.

## MCP connections: the run log

The store records MCP calls, not connections. Every process writes
`<data>/logs/devin_<YYYYMMDD-HHMMSS>_<pid>.log`, gzipped (not deleted) after
48 hours. At the default level it logs every connection. From the live run,
paths shortened:

```
INFO init_cli: chisel: version=3000.11.3 commit=9c803229faa4 os=linux arch=x86_64 binary=devin startup
INFO run_acp_server: chisel_server::acp: Starting ACP server
INFO toolbox::tools::mcp: server="fakeok" Connecting to MCP server 'fakeok'
INFO toolbox::tools::mcp::config: server="fakeok" Starting stdio MCP server 'fakeok': "python3" ["-I", ".../fake_mcp.py"]
INFO toolbox::tools::mcp: MCP server 'fakeok' connected successfully
INFO toolbox::tools::mcp::config: server="remotehttp" Connecting to streamable HTTP MCP server 'remotehttp' at: http://127.0.0.1:9/mcp
WARN toolbox::tools::mcp: MCP server 'remotehttp' connection failed: Streamable HTTP connection failed for 'remotehttp' at '…': … tcp connect error …
WARN toolbox::tools::mcp: MCP server 'fakebroken' connection failed: cannot find binary path
WARN chisel_mcp::mcp_description_cache: Failed to connect to MCP server 'fakebroken' for description: cannot find binary path
```

**Placement (ADR-0020).** A line names a process, never a session. The session
lock `<data>/session_locks/<session>.lock` holds the PID of the process that
last held the session, which is the PID in the log's file name.

1. The session's log is the one with its lock's PID whose span covers the
   session's activity (two seconds' slack; a reused PID's older log does not).
2. If another non-hidden session's lock names the same process over that span,
   connections are withheld and `mcp_log_state` is `placement_ambiguous`.
   `agent_version` and `entrypoint`, being the process's, are kept.
3. A failure followed by the description-cache line for the same server is the
   background pass, and is set aside. Attempts that still disagree leave
   `connected` as `None`.
4. A placement is made for the row, then held to each session's own span: a
   session, sub-agent included, takes the row's verdict only if a process it was
   drawn from was alive at that session's last activity. A sub-agent that
   finished in an earlier process than the lock's last holder is
   `no_log_for_session`, whether the row was placed, ambiguous, or unresolved
   between candidate logs or by an unreadable peer lock.

| `MCPConnection` | Source |
|---|---|
| `server` | The quoted name |
| `transport` | `stdio` (`Starting stdio MCP server`), `streamable-http` (`Connecting to streamable HTTP MCP server`), `sse` (`Connecting to legacy SSE MCP server`) |
| `endpoint` | The URL, for `streamable-http` and `sse`. `None` for `stdio`, because the logged command line can carry credentials |
| `connected` | True on `connected successfully`, false on `connection failed`, `None` with neither or with disagreeing attempts |
| `failure_category` | From the reason, first match wins: `policy` (blocked by policy, disabled by the organisation, not in its allowlist); `spawn` (cannot find binary); `auth` (401, 403, auth required, OAuth); `timeout`; `http_status`; `network` (tcp connect, refused, DNS); `protocol`; else `unknown` |
| `failure_detail` | The reason text (`LOCAL_ONLY`) |
| `duration_ms` | From `Connecting` to the deciding outcome line, at microsecond resolution |
| `advertised_name`, `advertised_version` | `None`. The handshake line (`rmcp::service`, `server_info`) is logged only with `RUST_LOG=rmcp=info` |

| `mcp_log_state` | Meaning |
|---|---|
| `applied` | Placed |
| `no_log_root` | No `logs/` directory |
| `log_root_unreadable` | The directory could not be opened |
| `no_log_for_session` | No lock for the session, or no log of its process spans it |
| `log_discovery_incomplete` | The logs or locks directory could not be fully listed, or a candidate log could not be read |
| `placement_ambiguous` | **New.** The process log served more than one session |

Per-call MCP outcomes come from the store; this log has no per-call lines at the
default level, so there is no ordinal join and `mcp_overlap_withheld` is 0.

## Growing sessions

ADR-0011's discipline, applied to a database (ADR-0018):

| Rule | Detail |
|---|---|
| Committed unit | A committed row: what a newline-terminated record is to JSONL |
| What is held | Per session, a digest of the session last returned. In memory, never persisted |
| `collect_file(path)` | Any path under the data root that can change a session: `sessions.db`, `sessions.db-wal` (a WAL commit changes only `-wal`), a run log, a session lock. Every session is rebuilt; those whose digest differs are returned. Any other path: nothing |
| The invariant | After any event, a consumer that replaced the sessions returned holds exactly what a cold read returns. One session's lock changes another's placement, so outputs are compared rather than inputs traced (ADR-0018) |
| Replaced store | Needs no special case: content that differs is returned, identical content is not |
| Between reads | Responses replace by `(session, response_id)`. **Turns can disappear on regeneration**, so a consumer replaces a `devin` session's turns wholesale |

## Model additions

All additive, with defaults, so no constructor changes. Filled for both kinds.

| Addition | Why |
|---|---|
| `ToolCall.tool_class` | What the tool *is*, from a closed set: `shell`, `shell_control`, `file_read`, `file_search`, `file_write`, `file_edit`, `web_fetch`, `web_search`, `delegate`, `skill`, `mcp`, `other` (ADR-0021) |
| `ToolCall.canonical_arguments` (`LOCAL_ONLY`) | One key name across kinds: `command`, `path`, `content`, `new_text`, `url`, `agent`, `skill`. A key the call did not record is absent |
| `ToolCall.exit_code` | A shell command's own exit status, where the kind records one |
| `Session.compactions_recorded`, `subagents_recorded` | Whether an empty list means "none happened" or "the kind can't say" (ADR-0022) |
| `MCPLogState` `placement_ambiguous` | ADR-0020 |
| `failure_category` `spawn`, `policy` | A failed launch and a policy block are neither network nor protocol failures, and folding them into `unknown` hides the two most actionable reasons |
| `Compaction.trigger` `unknown` | For a kind that records a compaction but not its cause |

**`tool_class` and canonical keys:**

| Class | `claude-code` | `devin` | Canonical keys (`devin` source key) |
|---|---|---|---|
| `shell` | `Bash`, `Monitor` | `exec` | `command` (`command`) |
| `shell_control` | `BashOutput`, `KillShell`, `KillBash` | `get_output`, `write_to_process`, `kill_shell` | — |
| `file_read` | `Read`, `NotebookRead` | `read`, `notebook_read` | `path` (`file_path`, `notebook_path`) |
| `file_search` | `Grep`, `Glob`, `LS` | `grep`, `glob`, `find_file_by_name`, `code_search` | — for `devin` (schema keys unconfirmed) |
| `file_write` | `Write` | `write` | `path`, `content` (`file_path`, `content`) |
| `file_edit` | `Edit`, `MultiEdit`, `NotebookEdit` | `edit`, `apply_patch`, `notebook_edit` | `path`, `new_text` (`file_path`, `new_string`); none for `apply_patch` (M3) |
| `web_fetch` | `WebFetch` | `webfetch` | `url` (`url`) |
| `web_search` | `WebSearch` | `web_search` | — |
| `delegate` | `Agent`, `Task` | `run_subagent` | `agent` for `claude-code`; none for `devin` (M3) |
| `skill` | `Skill` | `skill` | `skill` (`skill` or `name`) |
| `mcp` | Any call with an `mcp_server` | Any call with an `mcp_server` | — |
| `other` | `TodoWrite`, `ExitPlanMode`, `EnterPlanMode`, `AskUserQuestion`, `SlashCommand`, the MCP resource tools, `ToolSearch`, `TaskCreate`, `TaskOutput`, `TaskStop`, `SendMessage`, `ListAgents`, `SubagentHandback`, `Artifact`, `ReportFindings`, `SendUserFile` | `read_subagent`, `todo_write`, `exit_plan_mode`, `request_scope`, `mcp_list_servers`, `mcp_list_tools`, `mcp_read_resource` | — |

A Devin call the table does not name is `other` when Devin declared an ACP
kind for it in `tool_call_state` (ADR-0025): known to be Devin's own, with no
capability claimed. ACP's kinds are icon hints whose definitions span "files or
data", "commands or code" and "external data", so only the table gives a class
like `file_read`, `shell` or `web_fetch`.
A name the table does not hold and Devin declared nothing for gets `None`,
never `other`: a tool added after the table was written is unknown, not
ordinary. Devin Desktop bundles its own copy of the agent, which can be a
different version from the CLI writing the same store, so the two can use tool
names the other does not; the declared kind covers both.

## Coverage by agent kind

**Recorded** means a confirmed source; **Partial**, a source with a named
loss; **Unconfirmed**, a source in the binary not yet seen in a signed-in
session; **Absent**, no source.

| Field | `claude-code` | `devin` |
|---|---|---|
| Session identity, start, last activity | Recorded | Recorded (seconds) |
| Model, per response | Recorded | Recorded |
| Working directory | Recorded | Recorded |
| Agent version | Recorded | Recorded (run log, per process) |
| Entrypoint | Recorded | Partial: `acp` or `cli`; REPL and `-p` not separated |
| Git branch | Recorded | Absent |
| Initial prompt | Recorded | Recorded (`prompt_history` fallback) |
| Title | Recorded | Partial: `generated_title` until M8 |
| Context items | Recorded | Unconfirmed |
| Compactions | Recorded | Unconfirmed (`summarized_from`) |
| Provider refusals | Recorded | Absent (M9) |
| MCP connections | Recorded | Partial: no advertised identity at the default level |
| Responses and tokens | Recorded | Recorded; cache writes where the provider reports them |
| Turn time | Recorded | Recorded (call time), else Partial (seconds) |
| Sub-agent sessions | Recorded | Unconfirmed (`subagent_heads`) |
| Permission mode, per turn | Recorded | Absent (current value only) |
| Tool name, MCP server, arguments | Recorded | Recorded, `mcp_call_tool` included |
| Tool class, canonical arguments | Recorded | Recorded; some keys absent (M3) |
| Status and denial | Recorded | Unconfirmed (`failure_reason`); `automode-blocked` absent (M2) |
| Exit code | Absent | Unconfirmed |
| Per-call working directory | Recorded | Unconfirmed (`cwd`) |
| Duration | Recorded | Absent (M15) |
| Attributed skill, plugin | Recorded | Absent (M13) |
| MCP transport per call | Recorded | Recorded (by server) |

## Open facts

**To measure before relying on a field marked unconfirmed.** One signed-in
session settles most of them. It should run a shell command; edit and write a
file; call an MCP tool directly and through `mcp_call_tool`; be refused once by
a person and once by a rule; run once in `smart` mode; be compacted once; spawn
a sub-agent; and run once with `-p`. Then measure a larger corpus before
trusting any consumer rule built on one session, as [Token usage](token-usage.md)
asks for Codex. Fixtures are synthetic. **Real stores are never committed.**

| # | Question | Decides |
|---|---|---|
| M1 | That `failure_reason`, `exit_code` and `cwd` sit in `chisel/tool_result_meta`; what `Rejected` carries | Status, denial, exit code, working directory |
| M2 | Where `PermissionResolution {decision_source, resolution, …}` is stored, and whether it names a person, a rule, an organisation or the `smart` classifier | `denial_kind`, including `automode-blocked` |
| M3 | Argument keys of `grep`, `glob`, `apply_patch` and `run_subagent` (`SubagentInput` has five fields) | `canonical_arguments` |
| M4 | That the compaction node carries `summarized_from`, and whether a trigger is stated | `compactions` |
| M5 | That `subagent_heads` holds every sub-agent, and that its requests never also sit on the parent's tree | Sub-agent sessions; no double count |
| M6 | That `is_user_input` separates a person from the harness in every build | Turns, `initial_prompt`, context items |
| M7 | Hidden sessions' share of a store's tokens, and whether they take session locks | Whether hidden sessions stay unread |
| M8 | Whether `sessions.title` is set by a person or generated | `title` or `generated_title` |
| M9 | Where an interruption and a provider refusal are recorded | `interrupted`, `provider_refusals` |
| M10 | Whether one REPL process holding several sessions in turn is common | How often `placement_ambiguous` applies |
| M11 | The macOS path, against the documented one | Platform coverage |
| M12 | Whether a mode change is recorded per turn (`mode_change` appears only as telemetry) | `permission_mode` |
| M13 | What `tool_provenance` on a call record holds | `attributed_skill`, `attributed_plugin` |
| M14 | Whether `-p` runs log `run_repl_once` | `entrypoint` for programmatic runs |
| M15 | The unit of `metadata.created_at` on a result node, and whether it marks completion | `duration_ms` |

## Decisions

1. **An OpenAIDR-owned, read-only, transaction-consistent reader over SQLite**
   that re-emits whatever session differs from what it last returned
   (ADR-0018). Rejected: the program's `--export`, which runs the agent's
   binary; the export-only transcripts; `row_id` cursors with per-event peer
   re-placement, which review showed cannot be made complete.
2. **Turns from the main chain; responses from the whole tree** (ADR-0019).
3. **The run log is the MCP connection source, placed through the session
   lock** (ADR-0020). Rejected: time overlap, which misses connections made
   before the session row and cannot separate two sessions in one process.
4. **`tool_class` and `canonical_arguments` are model fields**, filled per kind
   (ADR-0021). Rejected: renaming every kind's tools to Claude Code's.
5. **`compactions_recorded` and `subagents_recorded`** (ADR-0022).

## Non-goals

- Devin Cloud, Cascade, or any network collection.
- Configuration (MCP servers, skills, plugins as configured); that is another
  component's concern.
- Running the agent's binary, its export command included.
- The program's own cost figures (ADR-0015).
- Windows (ADR-0013).

## References

- [Session collection](session-collection.md) · [Token usage](token-usage.md)
- ADR-0001 identity · ADR-0002 withholding · ADR-0003 connection logs ·
  ADR-0011 cursors · ADR-0013 Windows · ADR-0014 usage per response ·
  ADR-0015 tokens as recorded · ADR-0018 to ADR-0022
- Devin CLI 3000.11.3: embedded migrations and docs (`share/devin/docs`)
- Open-source store parsers: agentsview (`internal/parser/devin.go`), observer
  (`internal/adapter/devin`), codeburn (`docs/providers/devin.md`)
