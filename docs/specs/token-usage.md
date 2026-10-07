# OpenAIDR — Token Usage

*Design spec. Status: proposed (2026-10-04; revised 2026-10-06 after
re-measuring, which found that sub-agent transcripts mostly lack a response's
final record, and that workflow agents are read as their parent).*

**What this adds.** The session model gains a record of every request a session
made to its model provider, carrying the token counts the transcript recorded
for it. Tokens are the unit agent activity is billed and rate-limited in, and
the measure anyone needs in order to say where a session's consumption went.
Claude Code already writes them into the transcripts this package reads; the
parsing dependency drops them (its `ChatMessage` and `AgentEvent` have no field
for usage), so recovering them is this package's job on the same terms as every
other field upstream drops.

Three smaller changes ship with it, because without them a consumer cannot
total or attribute that usage:

- **Workflow agents are sub-agents.** Claude Code writes a workflow's agents
  one directory deeper than other sub-agents. This package reads each as a
  session holding the parent's identity, and that both loses their usage and
  withholds the parent's MCP enrichment ([Sub-agents and workflow
  agents](#sub-agents-and-workflow-agents)).
- **A compaction carries its time,** so consumption can be placed before or
  after the point where the context was reset.
- **The client's generated title is read,** beside the name a person sets,
  which is all `Session.title` carries today.

**What this does not add.** A monetary value of its own. This package reports
tokens and the attributes that determine their price, and never prices them.
Where the client records its own cost estimate, that is not read at all
(ADR-0015).

## What a consumer can build from this

Accounting for spend means pricing each response, then totalling and
attributing it. Each step needs one of these facts, and this release completes
the set.

| To | A consumer needs | Carried by |
|---|---|---|
| Price a response | Tokens by price-relevant bucket, the model, the cache lifetime, tier, speed and region | `ModelResponse` (new) |
| Know whether a count is complete | Whether the record reported is the provider's final one | `ModelResponse.final` (new) |
| Place it in time | When the response was stated | `ModelResponse.occurred_at` (new) |
| Total a piece of work | Every transcript that carries its responses, and which session delegated to which | Sub-agent identity (ADR-0001), now including workflow agents |
| Attribute it to tool calls | The response each turn belongs to | `Turn.response_id` (new) |
| Attribute context growth | Tool results and their sizes, and where the context was reset | `ToolCall.result_size` (existing); `Compaction.occurred_at` (new) |
| Group like with like | Working directory, branch, entrypoint, model, start and latest activity | Existing `Session` fields |
| Label the work | The client's name for the session | `Session.title` (existing); `Session.generated_title` (new) |

## What the transcript records

Measured on one developer machine on 2026-10-06: 193 Claude Code transcript
files (115 main, 53 sub-agent, 21 workflow agent) and 4 workflow journals,
written by client versions 2.1.283 and 2.1.284. They hold 27,661 `assistant`
records with a `message.usage` object and 13,011 distinct provider responses.
The 2026-10-04 draft measured 184 transcripts on the same machine and found the
same shape. A
second machine and an older client version should be measured before this spec
is accepted.

| Measured | Consequence |
|---|---|
| **One response is written as several records.** 9,192 of 13,011 responses (71%) span more than one record, up to 13 — one per content block. Each record repeats the response's `message.id` and carries a `usage` object | Summing usage per record counts 27,661 usages for 13,011 responses, about 2.1×. Usage cannot be read per record |
| **Only output grows between records.** In 457 responses the records' `usage` differs. In every one, `output_tokens` rises and the last record holds the maximum. No other count, tier or region ever differs between records of one response. The only other change is fields appearing: `output_tokens_details`, `server_tool_use`, `speed` and `iterations` appear on the final record only, and no field present on an earlier record is ever absent from the last | The last record states the response. Input, cache and every attribute are exact on any record; only output depends on reading the right one |
| **The final record is the one with a stop reason.** On every one of 13,011 responses, the last record carries a `stop_reason` exactly when it carries the four final-only fields. Earlier records carry `stop_reason: null` | A record with a stop reason is the provider's final statement of the response |
| **Sub-agent transcripts mostly lack the final record.** Every response in a main transcript ends in one (10,658 of 10,658). In sub-agent transcripts 728 of 1,044 responses (70%) do not, and in workflow-agent transcripts 851 of 1,309 (65%). Their last record is a streaming snapshot, usually showing 10 output tokens or fewer. These are the responses the draft found lacking all four fields, "cause not established" | Output on such a response is a lower bound, and nothing on disk states the true figure. Summed across the 14 sessions that have sub-agents, transcript output is 64% of the client's own total, leaving out the side-call model |
| **A response can produce no turn.** 10 responses consist only of records with thinking or empty content, which upstream projects into no message, so ADR-0001's turn rule builds no turn for them | Usage cannot hang off turns without losing responses |
| **`message.id` is on every usage-bearing record; `requestId` is missing on 6.** No response id appears in more than one session or more than one file | The response id is the key. The request id is carried where present |
| **Sub-agent and workflow-agent transcripts carry their own responses.** No response appears in both a parent's file and a sub-agent's | A sub-agent's consumption is its own session's |
| **The parent restates a sub-agent's last response.** 23 delegation results in parent transcripts carry `toolUseResult.usage` and `totalTokens`. On all 23, `usage` equals the sub-agent's own last response exactly, and `totalTokens` is that response's sum. It is not a total of the sub-agent's work | Reading these as usage counts that response twice, and they cannot fill the shortfall above |
| **The client records its own totals, per session.** 134 `cost-state` records in 110 of 115 main sessions, none in any sub-agent file: 86 sessions have one, 24 have two. Each holds `totalCostUSD`, `hasUnknownModelCost` and a per-model `modelUsage` with `inputTokens`, `outputTokens`, `thinkingTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`, `webSearchRequests` and `costUSD`. No cache-lifetime split and no web-fetch count. `totalCostUSD` never falls within a session. No `assistant` record carries a monetary field | Not read (ADR-0015, and the three rows below) |
| **The client's totals have no time.** None of the 134 `cost-state` records has a `timestamp`. In 128 of them, no later `user` or `assistant` record follows in the file; the other 6 are followed by later turns of the same session | It could carry no time (ADR-0010 allows only a record's own), and it appears after the work it covers, never during it |
| **The client's totals cover more than one transcript.** Sessions without sub-agents agree with their own transcript to within 0.2% on output, cache-read and cache-write tokens. Sessions with sub-agents agree only once their sub-agents' and workflow agents' transcripts are added in, and then on cache tokens only. The client also counts side calls on a small model (Haiku), present in 105 of 110 sessions' totals. In 98 of those, no response in the session's own or its sub-agents' transcripts is on that model | The figure belongs to the main session and covers its sub-agents and side calls too. It is never comparable with one transcript alone |
| **What the client's totals add is small.** Over the 111 sessions with a `cost-state` record, measured again on 2026-10-06, the totals count 13.4M tokens beyond the 2.49B that the session's own, sub-agent and workflow-agent transcripts record: 0.54%. 1.6M of it is side calls on the small model; most of the rest is cache reads and the unfinished output above | Not read. A field with no time, written after the work, by one client only, would add half a percent |
| **`<synthetic>` responses.** 14 records name the model `<synthetic>`, all with zero counts, and every one is marked `isApiErrorMessage` | Carried as recorded; their counts are genuinely zero |
| **Breakdowns hold on every record.** `cache_creation.ephemeral_5m_input_tokens + ephemeral_1h_input_tokens` equals `cache_creation_input_tokens` on every record that carries the split. `output_tokens_details.thinking_tokens` never exceeds `output_tokens`, and the client's `thinkingTokens` never exceeds its `outputTokens` | Both are carried as breakdowns, as recorded. Whether one count includes another is the provider's definition, not restated here |
| **`iterations` restates the top level.** Where present it is a one-item list whose counts equal the top-level counts (23,129 records), or empty (11) | Not projected (see [Open questions](#open-questions)) |
| **The generated title.** 3,913 `ai-title` records (`aiTitle`) across 110 sessions, none in a sub-agent file. Each session's records repeat a single value. `custom-title` appears in 2 sessions (7 records), neither of which has an `ai-title` | Read as its own field. Which one the client displays when both exist is not observed |
| **Compactions are timed.** All 31 `compact_boundary` records carry a `timestamp` | Carried on the compaction, under ADR-0010's rule |

## The model addition

**Model response** — one request a session made to its provider, and what it
consumed. Each row is information captured, as in the session-collection spec;
naming is the implementation's.

| Captures | Notes |
|---|---|
| Response identity | The provider's own id for the response (`message.id`). Unique within its session (ADR-0004); carried so a consumer can also deduplicate across sessions, which this package does not do |
| Request identity | The provider's request id, where recorded. `None` on the 6 measured records without one |
| Model | As recorded on the response. A session can change model partway through, by command or by provider fallback (`ProviderRefusal`), so this is per response. `Session.model` is unchanged |
| Occurrence time | When the record whose usage is reported was written: the response's last record. ADR-0010's rule: a value carries the time of the record that stated it |
| Final | Whether that record is the provider's final statement of the response. Input, cache-read, cache-write, reasoning and request counts, model and tier are exact either way. **Output on a response that is not final is a lower bound.** Not final means the final record has not been written yet, in a growing file, or never was, which is most sub-agent responses |
| Sidechain marker | On the same rule as a turn's: every response in a sub-agent's or workflow agent's own transcript is sidechain (ADR-0001) |
| Input tokens | Uncached input only, whatever the provider's own field includes |
| Cache-read input tokens | |
| Cache-write input tokens | And its split by cache lifetime: 5-minute and 1-hour, which are priced differently |
| Output tokens | |
| Reasoning tokens | The part of output spent thinking. Claude Code states it as `output_tokens_details.thinking_tokens`, Codex as `reasoning_output_tokens` |
| Web search and web fetch requests | Server-side tool requests, which are billed per request rather than per token |
| Service tier, speed, inference region | As recorded strings (`standard`, `not_available`, ...). Each can change the price of the same tokens |

**The token buckets are provider-neutral and disjoint.** Input means uncached
input only, so input, cache reads and cache writes never overlap, and reasoning is
the part of output spent thinking. Providers count differently: Claude Code's
`input_tokens` already excludes cache reads, while Codex's `input_tokens`
includes its cached tokens (see [A second kind: Codex](#a-second-kind-codex)).
Each kind's reader converts to these buckets, so a consumer can sum and price
any kind the same way.

Every count is `None` where the record does not state it. Zero means the record
said zero. Most fields a non-final record lacks are absent because that record
does not state them, so they are `None`, not zero.

None of this is **local-only**. Counts, ids, model names and tiers carry no
conversation material.

**Turn** gains the **response identity** of the record that produced it, or
`None` for a user turn and for any record without one. Several turns share one
response when the client wrote it as several records. This is the join a
consumer uses to attribute a response's consumption to the tool calls it issued;
the attribution itself is the consumer's.

**Session** gains:

- **Model responses**, ordered by each response's first record in the file.
- **Generated title**: the client's own generated name for the session, from its
  `ai-title` records. `None` where it wrote none, which includes every sub-agent.
  **Local-only**, on the same terms as `title`: it is written from the
  conversation and can name anything the conversation did.

**Compaction** gains its **occurrence time**: the boundary record's own
timestamp.

Representation, for orientation:

```python
@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_creation_5m_input_tokens: int | None = None
    cache_creation_1h_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    web_search_requests: int | None = None
    web_fetch_requests: int | None = None


@dataclass(frozen=True)
class ModelResponse:
    response_id: str
    request_id: str | None
    model: str | None
    occurred_at: datetime | None
    final: bool
    is_sidechain: bool
    usage: TokenUsage
    service_tier: str | None = None
    speed: str | None = None
    inference_region: str | None = None


# Turn:       response_id: str | None = None                          (appended)
# Session:    responses: tuple[ModelResponse, ...] = ()               (appended)
#             generated_title: str | None = None                      (appended; LOCAL_ONLY)
# Compaction: occurred_at: datetime | None = None                     (appended)
```

Every new field on an existing type is appended after every existing field with
a default, as `Session.title` was, so nothing constructing a `Turn`, a `Session`
or a `Compaction` positionally is broken.

**Why the generated title is its own field.** `Session.title` reads only
`custom-title` today, and folding `ai-title` into it is the obvious
alternative. It would need a precedence rule for when both exist, and that
case was never observed. Two fields state only what was recorded, keep
`title`'s meaning unchanged for every consumer, and leave the choice between
them to whoever displays it.

## Claude Code rules

Per-kind, like tool naming and status. Rules 1–7 are fixed by ADR-0014.

1. **Which records count.** A record of `type` `assistant` whose `message` has a
   `usage` object and an `id`.
2. **Which session.** The record's own `sessionId`, as for every other
   per-session recovery: one file can hold more than one session after a resume.
   In a sub-agent's or workflow agent's transcript, that file's path-namespaced
   session (ADR-0001).
3. **One response per id.** Within a session, every record with the same
   `message.id` belongs to one response.
4. **The last record states it.** Every field of the response is read from the
   last committed record with that id, in file order: counts, model, tier,
   speed, region, request id, time. Fields are not merged across records.
5. **Final is a stop reason.** A response is final when that last record's
   `message.stop_reason` is not null.
6. **Delegation summaries are not usage.** `toolUseResult.usage` and
   `totalTokens` on a parent's tool result are never read as usage. They restate
   the sub-agent's last response, which its own transcript carries.
7. **`<synthetic>` is carried.** Its counts are what the record states.
8. **`iterations` is not read.** See [Open questions](#open-questions).
9. **The client's running totals are not read.** A `cost-state` record is
   recognised and skipped (ADR-0015).
10. **The generated title is the latest `ai-title` record's `aiTitle`.** Assigned
    rather than kept from the first, as `custom-title` is, though no session was
    seen to change it.
11. **A compaction's time is its `compact_boundary` record's `timestamp`.**

## Sub-agents and workflow agents

ADR-0001 makes each sub-agent transcript its own session, identified as
`<agent kind>:<parent sessionId>:<file stem>`, because its records carry the
parent's `sessionId`. It found sub-agents at
`<project>/<sessionId>/subagents/agent-<id>.jsonl`, and the reader recognises a
sub-agent by its directory being named `subagents`.

A workflow writes its agents one level deeper:

```
<project>/<sessionId>/subagents/workflows/<run>/agent-<id>.jsonl
<project>/<sessionId>/subagents/workflows/<run>/agent-<id>.meta.json
<project>/<sessionId>/subagents/workflows/<run>/journal.jsonl
```

| Measured | Consequence today |
|---|---|
| 21 workflow-agent transcripts under one session, in 4 runs. Their records carry the parent's `sessionId` (every record that has one), and `isSidechain: true` | Each is read as a main session with the parent's identity. One cold pass returns 22 sessions with the same identity: the parent and 21 agents. The collector keeps the first and reports the others as collisions, and paths sort a directory's contents before the file beside it (`<sid>/…` before `<sid>.jsonl`). So the session reported as the parent is one of its workflow agents (88 turns, opening with the workflow harness), and the parent's own 290 turns and every other agent's usage are lost |
| The parent's id is now claimed by 22 files | On a cold pass, the parent's MCP enrichment is withheld as `session_id_collision`. `collect_file` on the parent's own file is unaffected, because there an unopened file claims only its stem |
| `collect_file` on a workflow agent's transcript | Returns a session under the **parent's** identity, with turns not marked sidechain. A consumer keyed by session identity overwrites the parent with the agent |
| Agent ids are unique: no two `agent-*.jsonl` under one session share a stem, across plain and workflow sub-agents | ADR-0001's identity works unchanged for workflow agents |
| Each run's `journal.jsonl` holds `launched`, `started` and `result` records with no `sessionId` and no `message` | Not a transcript |

**Rule.** A file named `agent-<id>.jsonl` at any depth under
`<project>/<sessionId>/subagents/` is a sub-agent transcript of `<sessionId>`,
with ADR-0001's identity and sidechain marking. Its project directory is the
parent of `<sessionId>`, however deep the file sits. Any other file under
`subagents/` is not a transcript and is not read. This is ADR-0001 applied to a
location it did not measure, not a new decision. The workflow run is not part of
the identity, because the stem is already unique and adding the run would give
a workflow agent a different identity shape from every other sub-agent.

## A second kind: Codex

Codex is not read yet, but its format was measured before this model was fixed, so
the model does not assume one agent. Five Codex sessions on 2026-10-05:

| Measured | Rule |
|---|---|
| Each provider response has one `token_usage_record` with its own `response_id`: 73 records, 73 distinct ids | One model response per `response_id`, as ADR-0014 does for Claude Code with a different collapse |
| Every response is also reported by an `event_msg` of type `token_count`, carrying `last_token_usage` and a running `total_token_usage` | Never read as usage; doing so counts each response twice |
| `cached_input_tokens` never exceeds `input_tokens`, and `total_tokens` equals input plus output on every record | Codex's input includes its cached tokens. Uncached input is `input_tokens − cached_input_tokens`; cache reads are `cached_input_tokens` |
| `reasoning_output_tokens` never exceeds `output_tokens` | Reasoning is part of output, as for Claude Code |
| `cache_write_input_tokens` is zero on every record | Carried as recorded. How it relates to `input_tokens` is not measured, so no conversion is applied until it is |
| `session_meta` names a `parent_thread_id` on 2 of 5 sessions | Child threads are sub-agent work, the counterpart of Claude Code's sub-agent files |
| No session title and no client cost estimate are recorded | `title`, `generated_title` and the client-reported cost stay `None` for Codex |
| Whether a usage record can be written before its response completes was not measured | Decide Codex's rule for `final` from measurement when its reader lands |

Measure a larger Codex corpus when its reader lands, as was done here for Claude
Code.

## Growing sessions

Collection reads the committed prefix of a growing file (ADR-0011), and the
recovery pass that recovers usage reads that same prefix. A response still being
written is therefore reported from the newest of its records committed so far,
with `final` false, and a later read reports a later record. **Response identity
is the update key.** A consumer that stores responses must replace by
`(session, response id)`, never append. Only output and `final` change on a
response already reported, together with the fields that first appear on the
final record. No response disappears while the file only grows.

A trailing partial record contributes nothing until its newline arrives. A
truncated or replaced file is rebuilt cold, as it is today.

The client's totals appear only after the work they cover. A consumer reading a
live session has its responses, but not yet the client's figure for them.

## Where it is read

- **Discovery** (`collect` and `collect_file` alike) recognises a sub-agent by
  the rule in [Sub-agents and workflow agents](#sub-agents-and-workflow-agents),
  not by its parent directory's name. Sub-agent files then stay out of the set
  of files claiming a raw session id. `_project_root` finds the project
  directory above `<sessionId>`, not at a fixed depth.
- **`_recorded`**, the single recovery pass, gains a per-session map from
  response id to the index of its first record and the fields of its latest
  record, including its stop reason. It also takes the latest `ai-title`
  record per session, and the timestamp of each `compact_boundary`.
  It already runs over the committed prefix in both the cold and the incremental
  path, so neither a second pass nor any change to the incremental projection's
  dependency state is needed.
- **Turn link.** The same pass records the message id under
  `(session, uuid, occurrence)`, the key turn times already use (ADR-0010), and
  `_turns` reads it from there.
- **`_session`** builds `Session.responses` and `generated_title`, and each
  compaction's time.
- **`render_json`** carries `responses` in each session document, and `response_id` on each turn. `generated_title` is
  local-only and withheld, like `title`. The text renderer is unchanged in this
  step.

## What this package does not do

- **Price tokens.** No price table and no cost of its own. The client's own
  estimate is not read (ADR-0015).
- **Complete a response that is not final.** Its output is reported as
  recorded and marked. Estimating the rest, or filling it from the client's
  totals, is interpretation.
- **Reconcile the client's totals with the responses.** The two differ by side
  calls, by sub-agent output that was never finalised, and by any transcript
  not read. Explaining that difference is the consumer's.
- **Total or deduplicate across sessions.** The response id is carried so a
  consumer can.
- **Attribute consumption to tool calls or components.** The turn link and the
  compaction times make that a consumer's join.
- **Read the account the client is signed into.** No transcript record states
  how a session was signed in. The client keeps the current login in its own
  configuration, which is machine state rather than session state, and tying a
  session to a person is identity resolution.
- **Read a sub-agent's `.meta.json`** (agent type, description, the tool call
  that launched it), or a workflow run's journal or script. Nothing here needs
  them. They can be added on their own evidence.
- **Read other agent kinds' usage.** Codex and Cursor are not read yet. Their
  formats must be measured, as this one was, when their readers land.
- **Read provider-side usage reports.** This package reads what is on disk.

## Compatibility

Additive for the model: new types, appended fields on `Turn`, `Session` and
`Compaction`, and new keys in the JSON document. Span identity, turn identity
and every existing field are unchanged.

One behaviour changes, as a fix: a workflow agent's transcript, previously
reported under its parent's identity (or dropped as a collision), now becomes
its own sub-agent session, `claude-code:<parent>:agent-<id>`, with sidechain
turns. The parent's MCP enrichment is no longer withheld on its account. The
release notes say so. Released as a minor version.

## Acceptance tests

Unit tests against the existing transcript fixtures:

1. **Collapse.** Three records with one `message.id` produce one response.
2. **Last record wins.** Records with `output_tokens` 10, 50 and 120 produce
   120, and fields present only on the last record appear.
3. **No merge.** A field present on an earlier record and absent from the last
   is `None`.
4. **Final.** A response whose last record has a `stop_reason` is final. One
   whose records all have `stop_reason: null` is not, and its output is the last
   record's.
5. **Turnless response.** A response whose records produce no turn still
   appears in `Session.responses`.
6. **Absent is `None`.** A record without `speed`, `server_tool_use` or
   `output_tokens_details` yields `None` for those, not zero.
7. **Two sessions, one file.** Responses go to the session named on their own
   records.
8. **Sub-agent.** A sub-agent file's responses belong to its own session and are
   sidechain. A parent's delegation summary adds nothing to the parent.
9. **Workflow agent.** A transcript at
   `<sid>/subagents/workflows/<run>/agent-x.jsonl` becomes session
   `claude-code:<sid>:agent-x`, sidechain, with its responses, through both
   `collect` and `collect_file`. The parent keeps its own identity, and its
   `mcp_log_state` is not `session_id_collision` on the agent's account. No
   collision is reported. A `journal.jsonl` beside it yields no session.
10. **Turn link.** Assistant turns carry their response id, and user turns carry
    `None`. Two turns from one response share it.
11. **Missing request id.** A response is still keyed and reported, with
    `request_id` `None`.
12. **`<synthetic>`.** Carried with zero counts.
13. **Growing file.** A response whose last record arrives after the first
    incremental read is revised on the next read, from not final to final. A
    partial trailing record contributes nothing. After the final read,
    incremental and cold collection agree, added to the existing contract tests
    in `test_incremental_collector.py`.
14. **Render.** The JSON document carries responses and turn response ids, and
    no generated title.
15. **Client totals are not read.** A session with `cost-state` records is
    identical to the same session without them.
16. **Generated title.** The latest `ai-title` sets `generated_title`. A
    `custom-title` still sets only `title`, and neither stands in for the other.
17. **Compaction time.** A boundary's time is its own record's timestamp, and
    `None` where the record has none.

Corpus check, recorded in the pull request rather than in a test, on a real
machine:

- The number of responses equals the number of distinct `(session, message.id)`
  pairs, and summed output tokens equal the sum over each response's last
  record.
- No session identity is reported twice.
- Aggregated over sessions with sub-agents, cache-read tokens summed over the
  session and its sub-agents come within 1% of the client's own figure,
  leaving out the side-call model. On the 2026-10-06 corpus they come to
  99.4% in aggregate, against 78.8% with workflow agents missing. Per
  session the figure varies: 8 of 14 sessions are within 1%, the other 6
  are 1.5–14% short, in every case because of what the client recorded
  rather than what was parsed.

## Open questions

- **Multi-item `iterations`.** Never observed. If it appears, what the top-level
  counts mean relative to its items must be measured before anything is
  projected from it.
- **Re-emitted responses.** ADR-0001 records resumed sessions re-emitting
  records under the same uuid. No response id was found in two sessions across
  13,011 responses, but a resume that re-emits an assistant record under a new
  session id would make two sessions report the same response. Measure it on a
  corpus with more resumed sessions. The response id is carried so a consumer
  can deduplicate either way.
- **Uncached input the transcript misses.** Even without sub-agents, the
  client's uncached `inputTokens` for the session's main models is 2.3× the
  transcript's (20,906 against 9,240 tokens across 96 sessions), while every
  other bucket agrees within 0.2%. The cause is not established. Here it is
  small, but it should be measured on a larger corpus.
- **Both titles at once.** No session had both `custom-title` and `ai-title`.
  Which the client shows is a consumer's display choice either way, but worth
  observing.

## References

- `session-collection.md` — the session model this extends.
- ADR-0001 — turn and sub-agent identity.
- ADR-0004 — scoping a key to the identity that makes it unique.
- ADR-0010 — a value carries the time of its own record.
- ADR-0011 — append cursors and the committed prefix.
- ADR-0014 — one response per provider id, stated by its last record, final
  only with a stop reason.
- ADR-0015 — tokens as recorded, never priced here; the client's own cost
  is not read.
