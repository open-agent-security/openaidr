# OpenAIDR — Devin CLI

*Proposed (2026-10-08). A new agent kind, `devin-cli`, with an OpenAIDR-owned
reader. Extends [Session collection](session-collection.md) and
[Token usage](token-usage.md); nothing changes for `claude-code`.*

## At a glance

| | |
|---|---|
| Covers | Devin CLI (`devin`), Devin Local (Devin Desktop's default agent), and editors driving it over ACP (Agent Client Protocol): one agent harness, one local state |
| Not covered | **Devin Cloud**: remote sessions, so reading them needs a network client, which read-only sensing rules out. **Cascade**: Devin Desktop's legacy agent, with an encrypted store of its own |
| Reads | `sessions.db` (SQLite, read-only), which holds messages, tool calls, results and per-request tokens; and the per-process run logs, which hold MCP connections |
| Backed by `adr-sensor` | No. Upstream has no Devin parser. Same `Reader` and `IncrementalReader` contracts as other readers |
| Platforms | Linux and macOS. Windows deferred (ADR-0013) |
| Kind mapping | `"devin": "devin-cli"` joins `AGENT_KIND_BY_SOURCE` only when the reader lands, because the row is a coverage claim |
| Main gaps | Server advertised identity (debug level only); MCP placement when one process serves overlapping sessions; the unconfirmed fields in [Open facts](#open-facts) |

## Evidence

- **Binary.** Devin CLI 3000.11.3 (`x86_64-unknown-linux`, commit
  `9c803229faa4`), checksum-verified against the vendor manifest. It carries
  its store's SQL migrations (`V1__initial_schema` to `V17__subagent_heads`),
  serialisation field names and user-facing literals.
- **Live run.** ACP mode in an isolated `$HOME`, which connects MCP servers
  without sign-in, against three test servers: a working stdio server, one with
  a missing binary, and an HTTP server that refuses connections. It wrote a real
  store (WAL journal, 17 migrations) and a real run log.
- **Three open-source parsers** of real stores: agentsview, observer and
  codeburn. They show what a signed-in session writes.
- **Not checked: a signed-in session.** No model call was made. Fields a
  conversation writes are marked **(unconfirmed)**; see
  [Open facts](#open-facts).

## The session store

| | |
|---|---|
| Path | `$XDG_DATA_HOME/devin/cli/sessions.db`, else `~/.local/share/devin/cli/sessions.db`. The program honours a `CHISEL_SESSION_DB` override. The reader takes `root`, like other readers |
| Journal | WAL. Opened `mode=ro`, and a read transaction sees committed rows only |
| Schema | Applied migrations are listed in `refinery_schema_history`. The reader is built against V1 to V17 |
| `sessions` | `id` (slug), `working_directory`, `workspace_dirs`, `backend_type`, `model`, `agent_mode`, `created_at`, `last_activity_at` (epoch seconds), `title`, `main_chain_id`, `hidden`, `metadata` |
| `message_nodes` | `row_id` (autoincrement), `session_id`, `node_id` (unique per session), `parent_node_id`, `chat_message` (JSON), `created_at`, `metadata` |
| `subagent_heads` | `session_id`, `agent_id`, `chain_node_id`, `updated_at`. Each sub-agent chain is "its own tree in the forest, unreachable from `sessions.main_chain_id`" (the migration's own comment) |
| `tool_call_state` | The final ACP `ToolCall` and `ToolCallUpdate` JSON per call, kept for session resume |

**The conversation is a tree.** Regenerating or editing a turn forks it, and
`main_chain_id` is the current leaf. **Turns are the path from that leaf to the
root.** Other branches are not turns, but their requests count as usage
([Model responses](#model-responses)).

**`chat_message` fields**, by their names in the binary, which match the
parsers:

- `role`, `content`, `thinking`.
- `tool_calls[] {id, name, arguments, index, kind}`, and `tool_call_id` on a
  result.
- `metadata`:
  - `request_id`, `generation_model`, `finish_reason`, `is_user_input`;
  - `summarized_from`, `num_tokens_preceding`;
  - `metrics {input_tokens, output_tokens, cache_read_tokens,
    cache_creation_tokens, ttft_ms, total_time_ms}`;
  - `extensions`. A tool result's own record (`ToolResult`) is under
    `extensions["chisel/tool_result_meta"]`.

**Failures** are reported as a `ReaderFailure`, never raised:

| Condition | Result |
|---|---|
| No store | No sessions, and no failure |
| Unreadable store | Failure, with the path |
| Locked past a short timeout | Store busy |
| A required table is missing | Unsupported schema, naming the last migration |
| A migration newer than V17 | Read for the known columns, and reported |

## Identity

| Level | Identity |
|---|---|
| Main session | `devin-cli:<sessions.id>`. The slug is unique in the store |
| Sub-agent session | `devin-cli:<sessions.id>:<subagent_heads.agent_id>`. This is ADR-0001's `<kind>:<parent>:<stem>`, with the agent id as the stem. All its turns and responses are `is_sidechain` |
| Turn | `key` is the `node_id`; `position` is the index along the chain |
| Span | `span_id(session_id, node_id, call_index)`, where the index is the call's position in its node's `tool_calls` |

- **Spans are never reused, and turns can vanish.** A regenerated turn leaves
  the chain and its spans stop being reported. `node_id` is unique per session
  and `row_id` only grows, so nothing attached to a span can move to another
  call.
- **Hidden sessions** (`hidden = 1`) are internal helpers such as the
  summarizer. They are not read in the first pass, because they have no
  recorded parent, person or owner. M7 measures their token share to confirm
  this.

## Session fields

| Field | Source |
|---|---|
| `started_at`, `last_activity_at` | `sessions.created_at`, `last_activity_at` |
| `model` | The newest response's `generation_model`. `sessions.model` is often empty or an alias |
| `working_directory` | `sessions.working_directory` |
| `title`, `generated_title` | `sessions.title` goes to `generated_title` only, until it is known whether a person set it (unconfirmed) |
| `initial_prompt` | The first chain node with `is_user_input` true |
| `turns` | Chain nodes with role `assistant`, or role `user` with `is_user_input` true. A `user` node without `is_user_input` was written by the harness and becomes a context item (unconfirmed). `tool` nodes are results |
| `context_items` | `system` chain nodes (source `system`), and harness-written `user` nodes (source `injected`) (unconfirmed) |
| `compactions` | One per chain node with `summarized_from`: `pre_tokens` from `num_tokens_preceding`, `occurred_at` from the node, and `trigger` `unknown` unless recorded (unconfirmed) |
| `agent_version`, `entrypoint` | `None` in the first pass. Both are in the run log, per process |
| `permission_mode` | `None` on every turn. `agent_mode` holds only the current value, and stamping it on past turns would state what the store does not know |
| `provider_refusals` | Empty for now. The program has a refusal path ("provider content filter") and a fallback, but where a session records them is unconfirmed |

## Tool calls

| Field | Source |
|---|---|
| `tool_name`, `mcp_server` | `tool_calls[].name`. `mcp__<server>__<tool>` is Claude Code's convention too, so `split_tool_name` gains a `devin-cli` branch with the same rule |
| `arguments` | `tool_calls[].arguments`, parsed from JSON text. Non-object JSON gives `{}`, plus a `ReaderFailure` naming the node |
| `result`, `result_size`, `error_text` | The `tool` node whose `tool_call_id` matches. `error_text` is the content when `success` is false |
| `truncated` | True when the result records `overflow_metadata` (unconfirmed) |
| `provider_call_id` | `tool_calls[].id` |
| `status`, `denial_kind` | [Status](#status) |
| `working_directory` | The shell result's `cwd` (unconfirmed) |
| `exit_code` | The shell result's `exit_code` (unconfirmed); a new field |
| `duration_ms` | `None`. Node times are whole seconds, so a 40 ms call would read as 0 or 1000 |
| `transport` | For an MCP call, from its server's placed connection |
| `tool_class` | The kind's table ([Model additions](#model-additions)) |

### Status

From the result's own record (`ToolResult {success, failure_reason}`), never
from its text:

| Record | `status` | `denial_kind` |
|---|---|---|
| `success` true | `ok` | — |
| `failure_reason` `Rejected` | `rejected` | `user-rejected` |
| `failure_reason` `Blocked` | `rejected` | `permission-rule` |
| `failure_reason` `ToolError`, `ValidationError` or `UnsearchedTool` | `error` | — |
| `success` false, no `failure_reason` | `error` | — |
| No result, and the session is still growing | `pending` | — |
| No result, and a later turn exists | `unknown` | — |

- **Same `denial_kind` vocabulary as the Claude Code reader**, so a refusal
  reads alike whichever kind wrote it.
- **`Blocked`** covers deny rules, built-in defaults and organisation policy.
  Whether the record says which is unconfirmed.
- **`interrupted`** is not emitted until a session shows how an interruption is
  recorded.
- **Interim rule.** Until M1 confirms where `failure_reason` sits,
  `success: false` maps to `unknown`, never `error`. Reading a person's refusal
  as a tool's fault is the worst wrong answer this model can give.
- **`mcp_call_tool`** is a generic tool that names the MCP server and tool in
  its arguments. As written, such a call has no `mcp_server`. Once its keys are
  known (M3), the reader recovers `(server, tool)`, a per-kind recovery of the
  join key this package owns.

## Model responses

- **One response per `metadata.request_id`, across the whole tree**, not only
  the main chain.
  - **Retries** are sibling nodes with the same `request_id` and identical
    metrics, so they count once (ADR-0014).
  - **Regenerated turns** have their own `request_id` and count, because they
    were real requests.
  - **Metrics without a `request_id`** are keyed `node-<node_id>`.
- **Sub-agent responses** come from the sub-agent's own chain
  (`subagent_heads.chain_node_id`). Whether they also sit on the parent's chain
  is unconfirmed (M5). No `request_id` may count in two sessions.
- **Not read:** Devin's own billing figures (`committed_credit_cost`,
  `committed_acu_cost`) and the cumulative totals in `sessions.metadata`.
  Tokens are as recorded and never priced (ADR-0015).

| `ModelResponse` | Source |
|---|---|
| `response_id` | `request_id` |
| `model` | `generation_model` |
| `occurred_at` | The node's `created_at`, in whole seconds |
| `final` | True when `finish_reason` is recorded |
| `is_sidechain` | True in a sub-agent session |
| `usage.input_tokens` | `metrics.input_tokens`, already net of cache reads |
| `usage.cache_read_input_tokens` | `metrics.cache_read_tokens` |
| `usage.cache_creation_input_tokens` | `metrics.cache_creation_tokens`. Null where the provider does not report it, and null stays `None`, never zero |
| `usage.output_tokens` | `metrics.output_tokens` |
| 5-minute and 1-hour cache split, `reasoning_tokens`, web request counts | `None`: not recorded |

## MCP connections: the run log

The store records MCP calls, not connections. The run log is
`<data>/logs/devin_<YYYYMMDD-HHMMSS>_<pid>.log`, one per process. Logs of
finished processes are gzipped after 48 hours, not deleted. At the default log
level it logs every server connection. These lines are from the live run, with
paths shortened:

```
INFO toolbox::tools::mcp: server="fakeok" Connecting to MCP server 'fakeok'
INFO toolbox::tools::mcp::config: server="fakeok" Starting stdio MCP server 'fakeok': "python3" ["-I", ".../fake_mcp.py"]
INFO toolbox::tools::mcp: MCP server 'fakeok' connected successfully
INFO toolbox::tools::mcp::config: server="remotehttp" Connecting to streamable HTTP MCP server 'remotehttp' at: http://127.0.0.1:9/mcp
WARN toolbox::tools::mcp: MCP server 'remotehttp' connection failed: Streamable HTTP connection failed for 'remotehttp' at '…': … tcp connect error …
WARN toolbox::tools::mcp: MCP server 'fakebroken' connection failed: cannot find binary path
```

| `MCPConnection` | Source |
|---|---|
| `server` | The quoted name |
| `transport` | `stdio` (`Starting stdio MCP server`), `streamable-http` (`Connecting to streamable HTTP MCP server`), `sse` (`Connecting to legacy SSE endpoint`) |
| `endpoint` | The URL, for `streamable-http` and `sse`. `None` for `stdio`, because the logged command line can carry credentials |
| `connected` | True on `connected successfully`, false on `connection failed`, `None` with neither |
| `failure_category` | From the reason: tcp connect error is `network`; HTTP status is `http_status`; `Auth required` or an OAuth failure is `auth`; a time-out is `timeout`; `cannot find binary path` or another launch failure is `spawn` (new); `MCP server is blocked by policy` is `policy` (new); anything else is `unknown` |
| `failure_detail` | The reason text (`LOCAL_ONLY`) |
| `duration_ms` | From `Connecting` to the outcome line, at microsecond resolution |
| `advertised_name`, `advertised_version` | `None` at the default level. They are filled from the handshake line (`rmcp::service: Service initialized as client`, with `server_info`) only when the log has it, which needs `RUST_LOG=rmcp=info` |

**Placement.** A line names a server and a process, never a session:

1. **Logs.** A log spans its first to last timestamp. Every log overlapping the
   session's `started_at` to `last_activity_at` serves it, so a resumed session
   can have several.
2. **Attempts.** Only those made while the session was live count. In an ACP
   process, `Dispatched SessionEnd for session <id>` closes the window.
3. **The description-cache pass.** It connects to every configured server in
   the background. Its failures are followed by `Failed to connect to MCP
   server '…' for description` (`chisel_mcp::mcp_description_cache`) and are
   dropped. Its successes cannot be told apart from the session's. If attempts
   agree, nothing changes; if they disagree, `connected` is `None`.
4. **Ambiguity.** If another session of the store was live in the same process
   over the same interval, everything for the session is withheld and
   `mcp_log_state` is `placement_ambiguous`. ACP clients pass servers per
   session, so not even transport is shared.

`mcp_log_state` keeps its existing values:

| Value | Meaning |
|---|---|
| `applied` | Placed |
| `no_log_root` | No `logs/` directory |
| `log_root_unreadable` | The directory could not be read |
| `no_log_for_session` | No log overlaps the session |
| `log_discovery_incomplete` | The directory could not be fully listed |
| `not_attempted` | No placement tried |

It adds one value, `placement_ambiguous`. Session times are whole seconds, so
two sessions that start in the same second are ambiguous.

**Per-call MCP outcomes come from the store.** Unlike Claude Code's connection
log (ADR-0003), this log has no per-call lines at the default level, so there
is no ordinal join.

## Growing sessions

ADR-0011's discipline, applied to a database:

| Rule | Detail |
|---|---|
| Committed unit | A committed row: what a newline-terminated record is to JSONL. A partial message is never read |
| Cursor | Per session: the highest `message_nodes.row_id` seen. Held in memory, never persisted. `row_id` only grows |
| `collect_file(path)` | Accepts `sessions.db` or `sessions.db-wal`: a WAL commit changes only `-wal`, so a watcher on the main file misses every write. Returns each session with rows above its cursor, rebuilt in full. A run-log path re-places connections for the sessions it overlaps |
| Replaced store | A new inode, or the highest `row_id` going backwards: drop every cursor and read cold |
| Between reads | Responses are replaced by `(session, response_id)`, as for Claude Code. **Turns can disappear on regeneration**, so a consumer replaces a `devin-cli` session's turns wholesale |

## Model additions

All additive, with defaults, so no constructor changes.

| Addition | Why |
|---|---|
| `ToolCall.tool_class: str \| None = None` | What the tool *is*, from a closed set: `shell`, `shell_control`, `file_read`, `file_search`, `file_write`, `file_edit`, `web_fetch`, `web_search`, `delegate`, `skill`, `mcp`, `other`. Filled from each kind's table, with `tool_name` kept as recorded. Otherwise every consumer must learn every kind's tool names, the per-kind knowledge this package exists to hold. It normalises vocabulary, like the `(server, tool)` split, and says nothing about what a call did |
| `ToolCall.canonical_arguments: dict[str, object]` (`LOCAL_ONLY`) | One key name across kinds: `command` (shell), `path` (file classes), `content` (file_write), `new_text` (file_edit, where the tool gives replacement text), `patch` (file_edit by patch, which can touch several files), `url` (web_fetch), `agent` (delegate), `skill` (skill). A key the kind does not record is absent, never guessed. `arguments` is unchanged |
| `ToolCall.exit_code: int \| None = None` | A shell command's own exit status. Recorded by `devin-cli` (unconfirmed placement), not by `claude-code` |
| `Session.compactions_recorded: bool = False` | Says whether an empty `compactions` means "none happened" or "the kind can't say". `claude-code` sets it true. The same reason `mcp_log_state` exists |
| `Session.subagents_recorded: bool = False` | The same, for sub-agents. True for `claude-code`; true for `devin-cli` once `subagent_heads` is confirmed complete (M5) |
| `MCPLogState` gains `placement_ambiguous` | See [MCP connections](#mcp-connections-the-run-log) |
| `failure_category` gains `spawn` and `policy` | A failed launch and a policy block are neither network nor protocol failures, and folding them into `unknown` hides the two most actionable reasons |
| `Compaction.trigger` gains `unknown` | For a kind that records a compaction but not its cause |

**`tool_class` tables:**

| Class | `claude-code` | `devin-cli` |
|---|---|---|
| `shell` | `Bash` | `exec` |
| `shell_control` | `BashOutput`, `KillShell` | `get_output`, `write_to_process`, `kill_shell` |
| `file_read` | `Read`, `NotebookRead` | `read`, `notebook_read` |
| `file_search` | `Grep`, `Glob` | `grep`, `glob` |
| `file_write` | `Write` | `write` |
| `file_edit` | `Edit`, `MultiEdit`, `NotebookEdit` | `edit`, `apply_patch`, `notebook_edit` |
| `web_fetch` | `WebFetch` | `webfetch` |
| `web_search` | `WebSearch` | — |
| `delegate` | `Agent`, `Task` | `run_subagent` |
| `skill` | `Skill` | `skill` |
| `mcp` | Any call with an `mcp_server` | Any call with an `mcp_server` |
| `other` | Every other built-in the reader knows | Every other built-in the reader knows: `read_subagent`, `todo_write`, `exit_plan_mode`, the `mcp_list_*` tools, `request_scope` |

A name in neither table gets `None`, never `other`: a tool added after the
table was written is unknown, not ordinary.

## Coverage by agent kind

**Recorded** means a confirmed source; **Partial**, a source with a named
loss; **Unconfirmed**, in the binary but not yet seen in a signed-in session;
**Absent**, no source.

| Field | `claude-code` | `devin-cli` |
|---|---|---|
| Session identity, start, last activity | Recorded | Recorded (seconds) |
| Model | Recorded | Recorded, per response |
| Working directory | Recorded | Recorded |
| Agent version, entrypoint | Recorded | Absent in the first pass (run log, per process) |
| Initial prompt, title | Recorded | Recorded; title goes to `generated_title` until confirmed |
| Context items | Recorded | Unconfirmed |
| Compactions | Recorded | Unconfirmed (`summarized_from`) |
| Provider refusals | Recorded | Unconfirmed |
| MCP connections | Recorded (connection log) | Partial (run log; no advertised identity at the default level) |
| Responses and tokens | Recorded | Recorded; cache writes where the provider reports them |
| Turn time | Recorded | Partial (whole seconds) |
| Sub-agent sessions | Recorded | Unconfirmed (`subagent_heads`) |
| Permission mode | Recorded, per turn | Absent (current value only) |
| Tool name, MCP server, arguments | Recorded | Recorded; `mcp_call_tool` needs its keys |
| Status and denial | Recorded | Unconfirmed (`failure_reason`) |
| Exit code | Absent | Unconfirmed |
| Per-call working directory | Recorded | Unconfirmed (`cwd`) |
| Duration | Recorded | Absent (seconds only) |
| MCP transport per call | Recorded | Partial (by server, from the run log) |

## Open facts

**To measure before the reader lands.** One signed-in session settles most of
them. It should:

- run a shell command;
- edit and write a file;
- call an MCP tool directly and through `mcp_call_tool`;
- be refused once;
- be compacted once;
- spawn a sub-agent.

Then measure a larger corpus before trusting any rule built on one session, as
[Token usage](token-usage.md) asks for Codex. Fixtures are synthetic, built
from what is measured. **Real stores are never committed.**

| # | Question | Decides |
|---|---|---|
| M1 | Where `failure_reason`, `exit_code` and `cwd` sit in a stored result; what `Rejected` carries | Status, denial, exit code, working directory |
| M2 | Whether `Blocked` names a person, a rule or an organisation | `denial_kind` |
| M3 | Argument keys of `write`, `edit`, `apply_patch`, `webfetch`, `run_subagent`, `skill` and `mcp_call_tool` | `canonical_arguments`; the `mcp_call_tool` join key |
| M4 | Which node carries `summarized_from`, and whether a trigger is stated | `compactions`, `compactions_recorded` |
| M5 | Whether `subagent_heads` holds every sub-agent, and whether its requests also sit on the parent's chain | Sub-agent sessions; no double count |
| M6 | Which `user` nodes are a person and which the harness wrote | Turns, `initial_prompt`, context items |
| M7 | Hidden sessions' share of a store's tokens | Whether hidden sessions stay unread |
| M8 | Whether `sessions.title` is set by a person or generated | `title` or `generated_title` |
| M9 | Where an interruption and a provider refusal are recorded | `interrupted`, `provider_refusals` |
| M10 | Whether a REPL process's log names its session | Placement in single-session processes |
| M11 | The macOS path, against the documented one | Platform coverage |

## Decisions (ADRs before implementation)

1. **An OpenAIDR-owned, read-only, transaction-consistent reader over
   SQLite**, with per-session `row_id` cursors: ADR-0011 applied to a database.
   Rejected: exporting sessions with the program's `--export`, which runs the
   agent's binary. This package never does that.
2. **Turns come from the main chain; responses from the whole tree.**
   Rejected: every branch as turns, which interleaves abandoned and current
   conversation. Also rejected: responses from the main chain only, which drops
   real regenerated requests.
3. **The run log is the MCP connection source**, placed by time, with
   `placement_ambiguous`. It is undocumented and per process, as Claude Code's
   connection logs are undocumented, and the time join must be able to say it
   failed.
4. **`tool_class` and `canonical_arguments` become model fields**, filled per
   kind. Rejected: renaming every kind's tools to Claude Code's names. That
   makes one vocabulary canonical without saying so, has no name for
   `apply_patch`, and loses what the agent actually called.
5. **`compactions_recorded` and `subagents_recorded`**: "absence is not
   falsehood", applied to two lists that can be empty for two different
   reasons.

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
  ADR-0004 recovery keys · ADR-0011 cursors · ADR-0013 Windows ·
  ADR-0014 usage per response · ADR-0015 tokens as recorded
- Devin CLI 3000.11.3: embedded migrations and docs (`share/devin/docs`,
  including `reference/otel.mdx`, an optional export not read here)
- Open-source store parsers: agentsview (`internal/parser/devin.go`), observer
  (`internal/adapter/devin`), codeburn (`docs/providers/devin.md`)
