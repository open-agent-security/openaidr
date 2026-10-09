# Token Usage Implementation Plan

**Goal:** Build [`token-usage.md`](../specs/token-usage.md). Each session gains a
record of every request it made to its model provider, with the tokens the
transcript recorded and whether that record was final. Workflow agents become
sub-agents. Compactions carry their time. The client's own totals are not
read; its generated title is.

**Spec:** [`token-usage.md`](../specs/token-usage.md).

**Decisions:**
- [ADR-0014](../adrs/0014-record-usage-once-per-provider-response.md): one
  response per provider id, stated by its last record, final only with a stop
  reason.
- [ADR-0015](../adrs/0015-report-tokens-as-recorded-never-price-them.md): tokens
  as recorded, never priced here.
- ADR-0001: sub-agent identity, which this applies to workflow agents.
- ADR-0010: a value carries its own record's time.

**Tech stack:** Python 3.11+ and the standard library. No new dependency.

**Order:**
1. The workflow-agent fix first. It is a live defect: on a cold pass a parent
   can be replaced by one of its own agents.
2. The model additions.
3. The fields read from records.
4. The JSON document.
5. The corpus check.

## Global constraints

- **Plumbing, never judgement.** No price, no total across sessions, no
  estimate of output a non-final record never stated, and no reconciliation of
  the client's totals with the responses.
- **Additive.** New types, and fields appended after every existing defaulted
  field, so nothing that constructs a `Turn`, `Session` or `Compaction`
  positionally breaks. Span identity and turn identity are unchanged.
- **One recovery pass.** Everything new is read in `_recorded`, which already
  runs over the committed prefix in both the cold and the incremental path. No
  second read of a file, and no change to the incremental projection's
  retained state.
- **Absent is `None`, never zero.** A count the record does not state is `None`.
- **`generated_title` is local-only.** It joins `LOCAL_ONLY` and stays out of
  `render_json`, like `title`.
- **The gates pass at every step:** ruff, ruff format, pyright and pytest, plus
  `scripts/check-no-proprietary.sh`.

## Task 1 — Workflow agents are sub-agents

**Files:** `readers/claude_code.py`, `tests/test_claude_code.py`,
`tests/test_incremental_collector.py`, `tests/fixtures/claude_jsonl.py`.

- [x] Test (spec 9): a transcript at `<sid>/subagents/workflows/<run>/agent-x.jsonl`:
  - becomes `claude-code:<sid>:agent-x` with sidechain turns, through both
    `collect` and `collect_file`;
  - leaves the parent's identity and its own turns intact;
  - reports no collision, and leaves the parent's `mcp_log_state` not
    `session_id_collision` on the agent's account.

  A `journal.jsonl` beside it yields no session.
- [x] **One rule for every site.** A file named `agent-<id>.jsonl` at any depth
      under `<sid>/subagents/` is a sub-agent of `<sid>`. Any other file under
      `subagents/` is not a transcript. It replaces the four places that tested
      `path.parent.name == "subagents"`:
  - the `collect` loop;
  - `collect_file`;
  - `_ambiguous_transcript_ids`;
  - `_project_root`, whose project directory is the parent of `<sid>`, however
    deep the file sits.
- [x] Discovery leaves out the non-transcript files under `subagents/`.

## Task 2 — The model

**Files:** `model.py`, `tests/test_model.py`.

- [x] `TokenUsage` and `ModelResponse`, as the spec's representation.
- [x] Appended fields:
  - `Turn.response_id`;
  - `Session.responses` and `Session.generated_title`;
  - `Compaction.occurred_at`.
- [x] `generated_title` joins `LOCAL_ONLY`.

## Task 3 — What the recovery pass reads

**Files:** `readers/claude_code.py`, new `tests/test_claude_code_usage.py`.

Each step starts with its failing test.

- [x] **Responses** (spec 1–8, 11, 12). An `assistant` record with
      `message.usage` and `message.id`, keyed by `(session, message.id)`:
  - Responses are ordered by their first record.
  - Every field comes from the last record, with no merge.
  - `final` means that record's `stop_reason` is not null.
  - The usage buckets are disjoint:
    - `input_tokens` is uncached input only;
    - the cache-lifetime split comes from `cache_creation`;
    - reasoning comes from `output_tokens_details.thinking_tokens`;
    - web search and fetch come from `server_tool_use`.
  - Tier, speed and region (`inference_geo`) come from `usage`.
  - The request id comes from the record's `requestId`.
  - `is_sidechain` follows the turn rule.
  - A delegation summary on a parent's tool result is never read.
- [x] **Turn link** (spec 10). The message id is recorded under
      `(session, uuid, occurrence)`, the key turn times already use, and `_turns`
      reads it.
- [x] **Client totals are not read** (spec 15). A `cost-state` record is
      recognised and skipped. A first version carried them as
      `Session.client_reported_cost`; it was removed before release (ADR-0015)
      because they add 0.54% of tokens and carry no time.
- [x] **Generated title** (spec 16). Taken from the latest `ai-title` record's
      `aiTitle`. `custom-title` still sets only `title`.
- [x] **Compaction time** (spec 17). Taken from the `compact_boundary` record's
      own `timestamp`.

## Task 4 — Growing files and the document

**Files:** `tests/test_incremental_collector.py`, `render.py`,
`tests/test_render.py`.

- [x] **Growing file** (spec 13):
  - A response whose last record arrives after the first incremental read is
    revised from not final to final.
  - A partial trailing record contributes nothing.
  - After the final read, incremental and cold collection agree.
- [x] **Render** (spec 14). Each session document carries `responses`, and each
      turn carries `response_id`. There is no
      `generated_title`.

## Task 5 — Corpus check and release notes

- [x] On a real machine, record in the pull request:
  - Responses equal the distinct `(session, message.id)` pairs, and summed
    output equals the sum over each response's last record.
  - No session identity is reported twice.
  - For each session with sub-agents, cache-read tokens over the session and
    its sub-agents come within 1% of the client's figure, leaving out the
    side-call model.
- [x] Release notes for the minor version. They name the one behaviour change:
      workflow agents become their own sub-agent sessions.

**Corpus check, 2026-10-06, one machine, 193 sessions:**

| Check | Result |
|---|---|
| Duplicate session identities | 0. Before the fix, this machine reported 21 |
| Responses against distinct `(session, message.id)` pairs | 13,460 and 13,460 |
| Summed output against the sum of each response's last record | 9,681,781 and 9,681,781 |
| Cache reads over a session and its sub-agents, against the client's, without the side-call model | 99.37% in aggregate, matching the spec's 99.4%. Per session, 8 of 14 sessions with sub-agents are within 1%. The other 6 are 1.5–14% short, and every one of their transcripts was read. So the gap is in what the client recorded, not in what was parsed. The spec's "for each session" should say "in aggregate" |
| Sub-agent responses that are final | 816 of 2,663 |

## Out of scope

The spec's own list: pricing, completing non-final output, reconciling the
client's totals, totals across sessions, attribution to tool calls, the
signed-in account, a sub-agent's `.meta.json`, Codex and Cursor, and
provider-side usage reports.
