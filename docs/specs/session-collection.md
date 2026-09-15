# OpenAIDR — Session Collection and the Session Model

*Design spec. Status: proposed (2026-08-28).*

**OpenAIDR is open source, Apache-2.0.** Reading an agent's session log and
normalising it into a session model is plumbing, not judgement, and it runs on
one machine — so it is open, and every new agent format is coverage somebody
else can contribute. What a consumer does with the model is the consumer's
concern; this document takes no position on it.

**OpenAIDR does not parse agent formats itself.** `adr-sensor` does — a
third-party Apache-2.0 package, not ours, which reads seven agent kinds today.
This package is the normalisation layer over it: it owns the session model, the
identity a consumer addresses rows by, and the fields upstream does not
populate. Parsing a format ourselves is the exception, taken per kind and only
when upstream cannot serve it (see [One contract, per agent
kind](#one-contract-per-agent-kind)).

| | `adr-sensor` supplies | OpenAIDR adds |
|---|---|---|
| **Agent formats** | Seven kinds parsed, of which this package instantiates **one** ([Agent kind coverage](#agent-kind-coverage)) | Nothing — this is why the dependency exists |
| **Discovery** | A whole-tree pass keyed on the `sessionId` field | Which files exist, and which session each one *is* — the per-file call that keeps a subagent distinct from its parent |
| **Span identity** | No identifier on a tool call | Derived from session, turn key and the call's index within that turn |
| **Status** | `success` / `unknown`, per parser | `pending` from a missing result, `rejected` inferred from the refusal wording that survives in the result body |
| **MCP server** | Populated by some parsers, not all | Per-kind `(server, tool)` normalisation — the join key |
| **Vocabulary** | Each agent's own name for itself | A mapping to this package's own agent kinds, with the agent's own name retained alongside it |

Two properties of that dependency shape this design and are stated where they
bite: it has a single published release, and it truncates long tool arguments
and results before OpenAIDR sees them.

## Agent kind coverage

**One kind today: `claude-code`.** `default_readers()` returns exactly one
reader, and `AGENT_KIND_BY_SOURCE` holds exactly one row. Every other agent kind
is **not read at all** — not read shallowly, not read and marked. A Codex or
Cursor session sitting on the same machine is absent from this package's output
entirely.

**"The dependency parses it" is not "OpenAIDR reads it",** and the gap between
those two statements is the most likely thing to be misread about this package.
`adr-sensor` ships parsers for seven kinds; this package instantiates one of
them. Reading a kind takes two more things that are ours: a reader implementing
the collection contract, and a row mapping the agent's own on-disk source name to
an agent kind. Neither exists for any kind but Claude Code.

Nor is an unread kind the same as a *kind-anonymous* one. Kind-anonymous
(see [Source vocabulary](#source-vocabulary)) describes a session this package
**collected** and could not place. A kind with no reader produces no session to
place, so it never reaches that path and never appears in a coverage count. The
absence is total and silent, which is why it is declared here.

### Codex and Cursor are the intended next kinds

Both are still to do. What follows is not a plan but the measurement that should
inform one, taken from one active development machine so the cost is known before
the work is scheduled rather than discovered during it. Both would arrive
**dependency-only** — the recoveries the Claude Code reader performs are per-kind
and none of them would carry over.

| | Measured |
|---|---|
| **Volume available** | 242 Codex rollouts and 6 Cursor conversations, against 616 Claude Code sessions |
| **Codex: the dependency reads 15% of tool calls** | It has a branch for `function_call` and none for `custom_tool_call`, which is where Codex puts most invocations — **619 read against 3,586 dropped**. A Codex session's call list would be a sixth of what ran, and closing it needs one branch rather than a new source |
| **Codex: `error` is unreachable** | The dependency marks a call `pending` when issued and `success` when any output arrives, without inspecting it, so a command that exited non-zero reads `ok`. All 619 calls read `ok` |
| **Codex: results are not always strings** | 52 of 619 outputs are lists of content blocks, so `ToolUsage.result` does not match its own annotation and a reader must normalise it or raise |
| **Cursor: no directory and no model** | `project_path` and `model` are unset on every conversation. A session with no directory cannot be resolved against a project by a consumer — the sharpest single consequence |
| **Cursor: no result bodies** | The dependency passes a result through for the tool `list_dir` alone. Cursor recorded **1,178 result bodies across 1,448 calls**, of which **0** would survive, because `list_dir` appears zero times |
| **Both: no MCP evidence** | The connection log is a Claude Code artifact. `mcp_connections` would be empty and `mcp_log_state` `not_attempted` on every session, so no transport, endpoint, advertised identity or failure category — and `mcp_server` would be `None` on every call, absent a measured per-kind name-splitting rule |
| **Both: no timings, refusals, context items, compactions, provider refusals, permission mode or subagent marker** | All per-kind recoveries. Cursor does record which cancellations were refusals — `userDecision` reads `rejected` on 12 of 1,448 calls — and the dependency drops it |

### Cursor cannot be added dependency-only without breaking span identity

This one is a blocker rather than a fidelity cost, and it is the reason Cursor is
not simply the cheaper of the two.

The dependency keys each message on `checkpointId or _bubble_id or seq_<n>`.
Measured on a real `state.vscdb`, `checkpointId` covers **22 of 2,165** bubbles
and `_bubble_id` covers **none**, so 2,143 messages fall back to a *position*.
`cursorDiskKV` is declared `key TEXT UNIQUE ON CONFLICT REPLACE` and
`EXPLAIN QUERY PLAN` reports a plain `SCAN` for the query the dependency runs, so
bubbles arrive in rowid order — and a bubble Cursor *rewrites* is deleted and
reinserted at the end of it. The conflict clause exists because rewriting is
normal. One rewritten bubble therefore shifts every position after its old slot,
and a later turn silently inherits the key, and the span, an earlier one held.

Stable span identity across re-reads is this package's published contract
(ADR-0001), so that is not a thinner answer but a wrong one: a consumer keeps
reading a stored span successfully while it points at a different call.

Cursor itself records what is needed — `bubbleId` on **2,165 of 2,165** bubbles,
unique on every one and equal to its own row key's last segment, and `toolCallId`
on **1,448 of 1,448** calls. Both are dropped by the dependency. So adding Cursor
requires at least the `bubbleId` recovery, which is the `project_path` shape of
recovery — one key off a record upstream has already parsed — rather than the
MCP-log shape that reads a separate source and joins it on ordinals.

Ordering is a separate question and cannot be fully closed at any price:
`composerData.fullConversationHeadersOnly` is an ordered bubble list covering
only the renderable thread — 14 of 171 bubbles in one conversation, 239 of 1,359
in another — so Cursor turns would arrive in an order that is not conversation
order even with identity fixed.

## What the dependency cannot carry

Measured against `adr-sensor` 1.0.0, not assumed. Its unified event model is
session-shaped: one timestamp per session, tool outcomes collapsed to `success`
or `unknown`, and no field for the call identifier it uses internally. Parsing
stays upstream's; what the model cannot express is normalised here, and what it
discards is stated rather than implied.

| This model wants | Survives `adr-sensor` | What this package does |
|---|---|---|
| Per-turn time | No — one session-level timestamp, the earliest | **Modelled**, from each record's own timestamp, recovered in this package's own pass. Absent where a record carried no timestamp, or no identity for the join to land on. See ADR-0010 |
| `rejected` | No — `toolDenialKind` and `is_error` are both dropped | **Inferred** from the refusal wording left in the result body: 88% of denials recovered at 97% precision, measured over real transcripts |
| `error` | Only where a parser sets it; the Claude one does not | **Taken only from an explicit upstream status.** Inferring it from result text scored 70% recall at poor precision, and a false `error` maligns a tool that worked |
| `interrupted` | No | **Not emitted.** The `Status` type carries it for kinds that can supply it |
| Span identity | No call id — but `sequence_id` carries the record's own key | **Derived** from session, turn key and index within the turn |
| Sidechain marker | No | **Derived from the file's path**: a transcript under a `subagents` directory is a subagent's, and every turn in it is marked |
| Session end | No — only the earliest timestamp | **Not modelled.** Session start is carried, and beside it the newest record observed — which is the latest activity seen, not an end. A session still being written has a newest record either way |

**Discovery is ours, because identity is.** Upstream's whole-tree parse keys each
session on the `sessionId` field, and every record in a subagent transcript
carries the *parent's* id while the event model has no field for the file it came
from. Parsed that way a parent and its subagents collapse into one identity, so
this package walks the tree itself and calls upstream per file, which keeps the
path — the only thing that distinguishes them.

## What the dependency drops, and where the fix has to live

**All of it is now read here, in one pass.** What began as a list of upstream
asks became a list of recoveries, because contributing upstream is not an option
available to this project and every field was in the file. The table below is
kept as the record of what was missing and what closed it.

The cost is stated plainly rather than buried: this reader knows nine keys of
Claude Code's JSONL, and every recovery is per-kind. A second agent kind gets
sessions from the dependency for free and none of this — measured, for the two
kinds most likely to be added next, in
[Agent kind coverage](#agent-kind-coverage). The division that
actually holds is **the dependency owns event extraction across kinds; the reader
owns per-kind recovery of what the shared schema drops** — which is a defensible
architecture, and not the one "we depend on a parser" describes.


`adr-sensor` is a third party we consume and do not modify: it is Uber's
project, pinned to a released version from PyPI. **Contributing upstream is not
an option available to this project**, so every gap below is ours to close or
ours to carry — there is no third state in which we wait for someone else.

Each field is present in the agent's own session state and discarded before this
package sees an event. Measured across 60 recent transcripts — 4,159 tool calls
— every one of them survives in the file itself:

| Dropped before we see it | Why it matters here | What the transcript still holds |
|---|---|---|
| ~~**Per-call timings**~~ — *closed* | Latency is the most requested thing a session view can show, and duration is a behavioural signal a consumer's rules can use | Each record's own `timestamp`, joined per call. **100% coverage**; median 67ms. Wall clock, not tool latency: `AskUserQuestion` blocks on a person, and the longest measured is 24 hours |
| ~~**A real success signal**~~ — *closed* | Was the single largest quality gap: every returned call read `unknown` | `is_error` on the result block. **`unknown` fell from 15,948 to 4,227**; `ok` 11,683, `error` 320. The remainder is results that state no outcome, which is not success |
| ~~**The context a call ran in**~~ — *closed* | `permissionMode`, `version` and `entrypoint` are recorded by the agent and dropped upstream. The first is the one that changes how everything else reads: with checks bypassed a refusal *cannot* occur, so "no refusals" means nothing was asked rather than everything was approved | All three read here. Mode is positional — declared on a turn, holding until the next declaration — because a session can enter `bypassPermissions` partway through. **42 sessions, 179 calls, ran unguarded** |
| ~~**Refusal as a first-class outcome**~~ — *closed* | Was recovered from English wording. Measured over 21,874 real tool results that scored **80.2% precision, 81.5% recall**, and its false positives were files *about* permissions — including this reader's own source | `toolDenialKind` on the record. **Read here now**, matched whole; three kinds kept apart rather than collapsed |
| ~~**The provider's call identifier**~~ — *closed* | Lets a consumer correlate with anything else that saw the same call | `tool_use.id`, **100% coverage**. Span identity stays derived (ADR-0001): changing what a span is would move every row a consumer has already addressed |
| ~~**What an MCP call did, and over what**~~ — *closed* | The transcript records that an MCP tool was called and, measured on a real corpus, nothing about how it ended: **129 of 130 MCP calls read `unknown`** despite every one having returned, so a successful MCP call could not be told from a failed one. It never records the transport, which is the only thing about an MCP call that can be *observed* rather than taken from the server's own schema | The per-server connection logs Claude Code writes beside its cache. **130 of 130 calls joined**; status, transport and duration recovered, plus each server's advertised identity and why a connection failed. Best-effort: an undocumented path, so every way it falls short is a state on the session (`mcp_log_state`), never a silent absence |
| **A subagent marker** | Delegated work is distinguished here by file path alone, which is sufficient and already correct | `isSidechain`. **Not taken**: the file path already identifies a subagent transcript unambiguously, and a second source for one fact is a disagreement waiting to happen |
| ~~**`project_path`**~~ — *closed* | The field exists in the shared schema and was simply left empty: upstream fixes it from whichever record first bore the session's id, and a transcript opening with a `queue-operation` carries none. 44 of 333 sessions lost a directory their later records stated — one of them 93 times | `cwd`, on every substantive record. **Recovered here; 44 sessions with no directory became 0** |
| **A record's identity through projection** | Upstream builds `chat_history` from a strict subset of the file — a `system` boundary, an `attachment` and a `user` record carrying a tool result are all dropped — and carries nothing that says which records survived. Occurrence is a *position in a sequence*, so a recovery pass counting over every record and a turn counting over `chat_history` disagree the moment a uuid repeats, and the turn reads a key holding another record's call id, result, permission mode, directory and attribution | Nothing: the file cannot answer it, only upstream can. **Carried, not closed.** `_projects_to_message` restates upstream's own filter so both sides count the same population (ADR-0004). It is the one place this reader depends on upstream's *filtering* rather than its output, and an upstream change to it desynchronises the counters silently — a parser that carried the raw record identity through projection would let the restatement go |

**Two rows are closed, and the first set the precedent.** `project_path` is reread from
the transcript when upstream leaves it empty: one key, keyed by session id so a
file holding two sessions cannot lend one's directory to the other, and read only
for sessions upstream returned empty. What it recovers is the transcript's own
recorded `cwd`, not an inference from it.

`toolDenialKind` followed, for a stronger reason. That one was not a gap but a
*wrong answer*: six English phrases stood in for a fact the record states
outright, and matching them classified any document discussing approvals as a
human refusal. Reading the field is exact where the phrases were 80% precise, and
it recovers something the phrases could never express — `user-rejected`,
`automode-blocked` and `permission-rule` are three different events, and a person
declining is not an automated classifier blocking.

Association is by whole result text, keyed within one transcript, because
upstream drops the tool-use id that would otherwise match a refusal to its call.
That is sound rather than convenient: across 21,874 results, 134 distinct refusal
bodies and 17,604 clean ones share not one value, since a refusal body is
generated boilerplate and not a tool's output. A refusal whose body upstream
abridged cannot be matched — one of 433 on one machine — and is reported
`unknown` rather than guessed at.

**The remaining four rows are an open decision, not a settled plan.** What argues for
closing them: `unknown` on every returned call is the single largest quality gap
in this package's output, and `rejected` currently rests on six English phrases
where the file states the fact outright. What argues against: reading these
fields means holding per-format knowledge about Claude Code's JSONL inside a
package whose whole shape came from delegating that, and a second reading of a
file the dependency has already parsed can misjoin against the first.

The cost is bounded and worth stating plainly, because "we cannot change
upstream" is not the same as "we cannot have this". Both readings walk the same
file; joining them on `tool_use.id` — carried on 100% of calls — is exactly the
identity that makes a misjoin detectable rather than silent.

Until that is decided this package reports what it can support and says `unknown`
where it cannot, rather than filling a gap with a plausible value.

A recovered directory is not a live one. A session names where it *ran*, and a
project can be renamed or deleted afterwards — 44 sessions on one machine name
directories that no longer exist. Saying so is a consumer's concern rather than
this package's: what is reported here is what the session recorded.

**Result bodies are carried whole, and deliberately unbounded.** Upstream
abridges every result to 1,000 characters from the middle. Measured on one
corpus, that cap sat *below the median result size*, cut 38% of results, and held
22% of 56.5M characters — while removing exactly the middle of a long response
and leaving both ends looking intact. The result body is where a consumer's
evidence usually is, so it is read from the record instead.

No cap is imposed, and the cost is reported rather than guessed at: `sessions`
prints the characters held and the largest single body on every run. Holding the
entire corpus measured **117MB peak RSS against an 18MB baseline**, and the size
distribution says where a cap would belong if one becomes necessary — p50 is 516
characters, p99 is 31k, p99.9 is 70k, and the twelve largest results are all
`Read` of large files whose evidence is already in the call's arguments. A cap
chosen before the reported number is uncomfortable would be the same arbitrary
threshold at a different value; the number on screen is what says when to pick
one.

**One inherited limit, named rather than discovered.** Upstream abridges long
*arguments* before this package sees them, so argument fidelity is bounded by
what survives that. And it drops sessions it judges trivial — a single message of
five characters or fewer that calls no tool is never returned, so its absence is
upstream's judgement rather than this package's account of the machine.

## Principles

| | |
|---|---|
| **Read-only sensing** | The JSONL and SQLite caches agents already write, **opened read-only**. No process hooks, no proxy, no injected agent, no listener. SQLite is opened in a mode that cannot write, so a corrupt or locked database degrades to no data rather than to a damaged one |
| **After the fact** | Sensing never sits in the agent's execution path, so a fault here cannot affect the agent it observes |
| **The dependency stops here** | Upstream types convert at this boundary and never appear downstream |
| **Absence is not falsehood** | An unmapped agent kind is reported as unmapped; a truncated value is shown as truncated |
| **The join key is ours** | Upstream does not reliably supply the MCP server name — recovering it is part of this contract, not an optional enrichment |

## The session model

Three nested levels. Each row is information the model captures, not a field
declaration — naming and representation are implementation choices.

**Session** — one agent conversation.

| Captures | Notes | |
|---|---|---|
| Session identity | Assigned by the agent, namespaced by agent kind | |
| Agent kind | Which agent kind produced it, per this package's own mapping | |
| Start time | Earliest observed activity. An end time is not available from the dependency | |
| Last activity | The newest record observed for this session. **Not an end**, and not the newest turn either: boundary and tool-result records carry timestamps without producing turns, so this is routinely later than the last turn. Absent where no record carried a time | |
| Turn count | | |
| Model | As the agent reports it, where it does | |
| Working directory | | local-only |
| Machine and user | | local-only |
| Turns | Ordered | |

**Turn** — one exchange.

| Captures | Notes | |
|---|---|---|
| Position | Stable ordinal within the session | |
| Occurrence time | When the record that produced this turn was written. Parallel tool calls in one record share it — within a turn, span order is the only finer ordering. Absent where the record carried no time | |
| Role | Who produced it | |
| Text | | local-only |
| Sidechain marker | Belongs to a subagent, not the main thread | |
| Tool calls | Ordered | |

**Tool call** — one invocation and its outcome.

| Captures | Notes | |
|---|---|---|
| Span identity | Stable across re-reads — see below | |
| Tool name | Normalized | |
| MCP server | Absent for built-in tools | |
| Arguments | | local-only |
| Status | Six values — see below | |
| Duration | Wall clock from the record's own timestamps, not from the dependency's event model, which carries none. For an MCP call the connection log's tool-execution time fills a gap the transcript left, and never replaces a value it stated. **No absolute per-call time is carried beside it**: a call's start is its turn's occurrence time, and a call's end follows from the two — carrying either again would restate a value the model already holds, and recovering an end independently would require a join this package refuses (ADR-0010) | |
| MCP transport | Which transport carried this call, where the connection log could be read. `None` elsewhere, and `None` is *unknown*, never local | |
| Outcome | The abridged result and error text | local-only |
| Result size | The size of what the agent produced, not of the abridged copy held here | |
| Truncation marker | Upstream elided part of the result. The marker it leaves carries the count removed, which is what makes the original size exact rather than estimated | |

A session additionally carries its **MCP connections** — one record per server per session: transport, endpoint, the name and version the server advertised on the handshake, whether it came up, and a failure category if it did not. Connection-scoped, not call-scoped: these are properties of the connection, and repeating them on every call would say otherwise. The server's own words about a failure are **local-only**; the category is not. Beside them, `mcp_log_state` records whether the log was readable at all.

**local-only** information serves correlation and local rendering. It never enters
a finding — a detection receives session identity, kind, start and turn count, and
nothing more.

Times are **not** local-only. A turn's occurrence time says when something
happened and nothing about what was said, so it carries no conversation
material; a consumer that could not date what it found would be left with the
session's start, which for a long or resumed session is wrong by hours or by
weeks. Times travel.

**Every time this package reports is timezone-aware and converted to UTC.**
Transcripts are written on developer machines in every zone, and a session
resumed after travel can carry more than one offset within a single file, so an
instant is the only comparable form. Converting an offset-bearing value to UTC
is lossless — the same instant, one canonical spelling — and it matches the zone
the dependency already returns the session's start in, so every time on the
model has the same `tzinfo` whatever produced it. Consumers may rely on that:
comparing, sorting and folding across sessions is only sound if every value is
an instant, and a single canonical zone additionally makes the serialised form
stable for anything that reads a date off it.

**One rule, and it is the dependency's.** A value carrying no offset is taken as
UTC, exactly as the dependency takes it when producing the session's start. The
alternative — reporting it absent, on the grounds that a zoneless wall-clock
reading could be anywhere in a day-wide band — would be stricter, and would put
two rules in one model: a consumer would have to know that the session's start
guesses a zone while a turn's time refuses to. The dependency's behaviour cannot
be changed from here, so matching it is the only way to have a single rule, and
a single rule is worth more than the strictness. See ADR-0010.

The line that is **not** crossed is between interpreting a value and inventing
one. A record that carried no time at all yields no time — this package never
substitutes the moment of collection, a neighbouring turn's value, or the file's
modification time for a value the transcript does not contain.

This is the trust tenet at its narrowest point. Transcripts are read only on the
machine that produced them. **OpenAIDR has no upload path at all** — it reads,
normalises and returns a model, and ships nothing anywhere. What a consumer does
with that model is the consumer's decision and the consumer's boundary to state.

### Stable span identity

Span identity must survive re-reads of a growing session, because an incremental
consumer draws a call when parsed and attaches outcome, component and findings later by
naming the same span. Identity that shifted as a session extended
would land every enrichment on the wrong row, silently.

It is therefore derived only from things that do not change as a session grows:
which session, which turn, which call within that turn. Anything derived from
position in a mutable list, or from content that may later be appended to, is
disqualified.

### Status: four failures, not one

| Status | Means |
|---|---|
| `ok` | Ran and returned, and the evidence says it worked |
| `unknown` | Ran and returned, and nothing reaching this package says whether it worked |
| `error` | Ran and failed — only where the parser for that kind says so |
| `rejected` | **The user declined to let it run** |
| `interrupted` | The user stopped the turn mid-flight |
| `pending` | Called, not yet resolved |

**`unknown` is a statement about our evidence, not about the call.** For an agent
kind whose parser sets no real success signal, a returned call is `unknown` rather
than `ok`: upstream marks a Claude Code call `success` merely because a result
exists — it never reads `is_error` — so passing that through as `ok` would launder
an absence of evidence into a claim of success. A consumer reads `ok` as *worked*,
whatever a vocabulary says it means. This is a per-kind rule: a kind whose parser
does supply a genuine outcome maps it to `ok` or `error` directly. `unknown` is
therefore a *coverage statement about the dependency*, and it narrows as the asks
below are met.

For an MCP call it narrows from a second source rather than from the parser: the
connection log states the outcome the transcript does not. It only ever *resolves*
an `unknown` — a status the record itself stated is evidence from the session and
is never overwritten by a second reading of the same call — and only once the
guard in `claude_code_mcp` has established that the log and the transcript agree
on how many calls were made. Where they disagree the outcome is withheld and the
call stays `unknown`, because the log carries no call identifier and position
within a tool's sequence is the only thing that could name one.

Agents collapse these visually; AIDR does not. `rejected` is a *human judgement
about a proposed action* and the most under-used signal in agent telemetry —
repeated rejection of one tool says something no error rate does.

Deriving `rejected` and `interrupted` needs kind-specific result shapes, behind
the same per-kind rules as tool naming.

## Collection

| Decision | Why |
|---|---|
| **In process, never through a file** | A serialize-and-reparse round trip adds latency to the path that most needs speed, and would write transcript content to disk that nothing else in this design does |
| **Cold start: one full pass** | Held as a session map keyed by session identity |
| **Steady state: changed files only** | Watch modification times; sessions are append-only, so a re-read replaces a session with its longer self and emitted spans keep identity |
| **Skip unchanged work** | A per-session content hash avoids re-projecting a file that was touched but not changed |

A full two-week pass is affordable once. Repeating it on every change is not — a
tool that keeps a laptop warm gets uninstalled regardless of what it finds.

**Narrowed parse.** Whether the upstream package exposes a per-file or
per-root parse determines how efficient the steady-state path is. If it does, the
incremental path uses it; if not, OpenAIDR owns the tail loop for watched agent kinds and
calls upstream for cold start and the rest. Both satisfy the same contract, so
the choice reaches no other component. Upstreaming a narrow entry point is a
third option.

### One contract, per agent kind

Given a kind and a window, return sessions; given a kind and a watermark, return
what changed.

**The window is applied to a session file's modification time**, not to the
timestamps inside it. That holds for every kind read today, all of which keep one
file per session; a kind that keeps its sessions in a database has nothing to
stat and would have to state its own basis. That bounds the work before anything is parsed, which is
what keeps a cold pass affordable, and it means a session is returned whole when
its file was written inside the window — including turns that happened before it.
The alternative, filtering on activity timestamps, cannot be evaluated without
parsing the file it would exclude.

The window bounds **parse work, and which sessions come back** — not what a
reader may know about the files it did not parse. Facts a reader can read off a
path it has already listed (ADR-0008: which raw session id a file claims) stay
whole-corpus, because a question like "is this id claimed twice?" has no
meaningful window-scoped answer.

The dependency backs the agent kinds it supports; any kind can instead be backed
by an OpenAIDR-owned reader on the same contract — **per kind, not
all-or-nothing**. That is what bounds dependency risk to a single module. A
failure in one kind's collection is reported and isolated, never aborting the others.

## Source vocabulary

Agents name themselves in their own on-disk vocabulary, which differs from
this package's own agent kinds, so this component owns the mapping.

| Case | Behaviour |
|---|---|
| Unmapped agent kind | Collected, marked kind-anonymous, correlates less — a **coverage statement** |
| User-excluded kind | Dropped outright — a **user instruction** |

### Tool names

The MCP server survives in most agent kinds only inside the tool name, in
kind-specific shapes — one prefixes and separates with a doubled underscore, another simply
joins server and tool with a single one. Normalization happens once, here,
producing `(server, tool)`.

**Per-kind rules, never one global pattern.** A general pattern mis-splits any
server name containing the delimiter, and the result is not a missing server but
the *wrong* one — which then resolves to the wrong component and attributes a
finding to a package that was never involved. A silent wrong answer is worse than
a gap.

## What this component does not know

| Gap | Answered by |
|---|---|
| Granted authority — allowed tools, permission mode, whether the session was unattended. One agent kind records this; most do not | A consumer's own inventory of granted authority — the fallback for what the transcript does not say |
| System configuration. The dependency defines a model no parser populates | A consumer's own system inventory, with identities and provenance the dependency cannot derive |

## Scale

Measured on one active development machine over a fourteen-day window, and
recorded here because shape matters more than any single number:

| | |
|---|---|
| Sessions | **332**, of which 114 are subagent transcripts |
| Turns per session | min 2, median 32, p90 139, **max 2,105** |
| Tool calls | 18,111 — median 24 per session |
| Concentration | The busiest 10% of sessions hold **54%** of all tool calls |

The skew is the finding. A median session is small enough to be uninteresting
and the tail is three orders of magnitude larger, so windowing and batching are
sized by the tail rather than the median — and those same long sessions are the
likeliest false-positive source for anything reasoning over them later.

Subagent transcripts are a third of the session count, which is why treating
them as sessions in their own right rather than folding them into their parents
changes the shape of the collection rather than being a detail.

## What this package owns

| Surface | Scale | Approach |
|---|---|---|
| The session model and span identity | The published contract | Consumers depend on these; they are this package's public API, not internal types |
| One runtime dependency | Additive | `adr-sensor`, confined behind the adapter, so the rest of the codebase is unaware of it |
| The source-vocabulary mapping | New, small | A mapping table plus per-kind naming rules, colocated with collection |
| A watched, in-memory session cache | New | Cold pass then changed-file re-reads; no persistence, nothing on disk |

**This package's only runtime dependency is `adr-sensor`.** It does not import
`openaca`, and it must never import or mention anything proprietary. It produces
a model; interpreting that model is a consumer's concern, and OpenAIDR takes no
position on what a consumer concludes.

## Non-goals

- Parsing agent formats directly, for kinds the dependency covers
- Any interpretation: no scoring, no identity resolution, no findings
- Retaining session content beyond the current view
- Any upload path — OpenAIDR returns a model and ships nothing anywhere
- A session end time, or any inference that a session has finished
- Absolute per-call timestamps, which would restate the turn's own (ADR-0010)
- Interpolating a time for a turn whose record carried none

## Robustness bar for time

Aims to get right: a turn that could be dated is dated, and never by a
neighbouring turn's time; every time reported is an instant — timezone-aware and
normalised to UTC — so that two sessions written in different zones compare
correctly; a time this package reports is one a record stated, never one it
computed to fill a hole; `None` means *the file did not say*, and
is never substituted with the collection time, the file's modification time, or
any other clock; and the durations this package already publishes are unchanged
by the addition, because the recovery reads the record's own key and never the
call-id join those durations rest on.

A finding that a dateable turn is left undated, that a time is invented where a
record carried none, or that a published duration moved, is above the bar
however narrow the trigger. That the turn granularity is coarser than a
per-call one — parallel calls in one record share an instant — is below it, and
deliberate: a finer value would be the same number written more times.

## References

[ADRs](../adrs/INDEX.md)
