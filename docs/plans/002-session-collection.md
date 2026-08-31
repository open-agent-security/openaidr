# Session Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Milestone scope:** this plan delivers the first milestone the spec's design
supports on its own — one agent kind, one cold pass — and is the P0 the spec's
own "Scale" section refers to when it says concrete figures are measured
during P0 and recorded there. `docs/specs/session-collection.md`
also describes a watched, incremental steady state and six further agent kinds;
those are each a separate reader or collection mode behind the same contracts
this plan establishes, and ship as their own plans once this milestone is in
place. Every task below that touches those surfaces says so at the point it
matters, rather than leaving their absence to be inferred from what is missing.

**Goal:** `openaidr sessions` prints what the Claude Code sessions on this
machine actually did — sessions, turns, tool calls and the outcomes the
evidence supports — by parsing with `adr-sensor` and normalising here. Once
the incremental and multi-kind milestones land, the same command covers every
agent kind and stays current without a repeated cold pass; until then, the CLI
and README say plainly which of those this build has.

**Architecture:** `adr-sensor` owns the format; this package owns everything its shared event schema cannot express. Upstream is called **per file** rather than per tree, because its whole-tree parse keys sessions on the `sessionId` field and every record in a subagent transcript carries the *parent's* — parsed that way a parent and its subagents collapse into one identity. Discovery therefore stays here, which keeps the file path, and with it the only thing distinguishing them. A per-kind reader contract fronts this; a collector runs one cold pass in memory; a renderer prints text or JSON.

**Tech Stack:** Python 3.11+, `adr-sensor` as the single runtime dependency, stdlib for the rest (`argparse`, `json`, `dataclasses`, `pathlib`, `datetime`). `pytest`, `ruff`, `pyright` as gates.

**Spec:** `docs/specs/session-collection.md`

**Status:** this milestone's implementation work is complete; each task's
steps are checked off below as a record of what shipped, in the order the
commits landed. Acceptance is not complete: Task 5 leaves one item
explicitly open — the denial-marker corpus and labelling method — and until
it exists, whether `"requires approval"` and `"requested permissions"`
actually mean a user declined, rather than naming a decision still pending,
is unverified rather than merely undocumented. The milestone should not be
treated as accepted until that item closes. A file action reads as what that
step did to the tree *at the time*, not as a standing instruction against the
current tree — re-running this plan from a checkout that already has the
milestone finds every module and test already in place, and the stated
`ModuleNotFoundError` red states will not reproduce.

## Global Constraints

- **Plumbing, never judgement.** No scoring, no identity resolution, no findings, no severity, no confidence.
- **No upload path, ever.** This package reads, normalises and returns a model. It ships nothing anywhere.
- **Nothing proprietary.** No import, config key, URL or mention of any closed product.
- **Read-only sensing.** Session files are opened read-only. No process hooks, no proxy, no listener.
- **Per-kind rules, never one global pattern.** A general tool-name pattern mis-splits any server containing the delimiter, and the result is the *wrong* server, not a missing one.
- **Absence is not falsehood.** An unmapped kind is reported as unmapped; an outcome nothing establishes is `unknown`, never `ok`.
- **Span identity is stable across re-reads** of a growing session, derived only from what does not change as it grows (ADR-0001).
- **The dependency stops at the reader.** `adr-sensor` types convert there and never appear in the model, collector, renderer or CLI.
- **In process, never through a file.** No session dump is written and none read back.
- Python floor `>=3.11`; line length 100. `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright`, `uv run pytest -q` all pass before every commit.

---

### Task 1: The session model and span identity

**Files:** Create `docs/adrs/0001-session-turn-and-subagent-identity.md`, `src/openaidr/model.py`; test `tests/test_model.py`.

**Interfaces:** Produces `Status` (Literal, six values); `ToolCall`, `Turn`, `Session` frozen dataclasses; `span_id(session_id, turn_key, call_index) -> str`; `LOCAL_ONLY: frozenset[str]`; `Session.turn_count`; `Session.public_descriptor() -> dict[str, object]`. Consumes nothing.

- [x] **Step 0: Lock the `adr-sensor` contract this model and reader are built against**

Everything from here down — the schema fields read, the exceptions swallowed,
the filtering and truncation behaviour — is measured against one installed
release, not assumed from its interface. Record the exact version
(`adr-sensor==1.0.0`, not an open `>=` range: an import of parser and schema
internals, not just the public entry point, means a later release is free to
rename or restructure them without that being a breaking change by its own
semver) in `pyproject.toml`, and note in this plan which upstream module paths
and dataclass fields the reader depends on so a version bump is a deliberate
re-verification, not a silent drift.

- [x] **Step 1: Record the identity decisions in an ADR, before the model is frozen**

Six decisions with plausible alternatives a later contributor will re-suggest, whose reasoning is invisible in the resulting code: a turn is one message record as upstream reports it; a subagent transcript is its own path-namespaced session; a call's identity is `session:turn_key:call_index` with the index *within its turn*; a repeated turn key disambiguates only the *later* occurrence, so a span already emitted never moves; positional fallbacks are valid only under an append-only source; and a session identity already held is a collision to report, not a duplicate to replace. Write the ADR from `docs/adrs/TEMPLATE.md` recording each with its rejected alternative, add it to `docs/adrs/INDEX.md`, and commit before Step 2 so what follows implements a recorded decision.

- [x] **Step 2: Write the failing test**

```python
from openaidr.model import LOCAL_ONLY, Session, span_id


def test_span_id_names_the_session_turn_and_call() -> None:
    assert span_id("s1", "turn-uuid", 0) == "s1:turn-uuid:0"


def test_span_id_indexes_within_the_turn_not_the_session() -> None:
    assert span_id("s1", "turn-uuid", 2) == "s1:turn-uuid:2"


def test_span_id_is_unchanged_by_later_turns() -> None:
    """A session grows by appending; an existing span must keep its key."""
    first = span_id("s1", "turn-a", 0)
    _later = span_id("s1", "turn-b", 0)
    assert span_id("s1", "turn-a", 0) == first


def test_public_descriptor_carries_no_local_only_information() -> None:
    session = Session(
        session_id="claude-code:s1",
        agent_kind="claude-code",
        source="claude",
        started_at=None,
        model="claude-opus-5",
        working_directory="/Users/someone/secret-project",
        machine="host",
        user="someone",
        turns=(),
    )
    descriptor = session.public_descriptor()
    assert descriptor == {
        "session_id": "claude-code:s1",
        "agent_kind": "claude-code",
        "started_at": None,
        "turn_count": 0,
    }
    assert not LOCAL_ONLY & set(descriptor)
```

- [x] **Step 3: Run test to verify it fails** — `uv run pytest tests/test_model.py -q`, expect `ModuleNotFoundError: No module named 'openaidr.model'`.

- [x] **Step 4: Write the implementation**

```python
Status = Literal["ok", "error", "rejected", "interrupted", "pending", "unknown"]
"""How a call ended.

`unknown` is not a failure state and not a placeholder: it means the call ran and
returned, and the evidence reaching this package does not say whether it
succeeded. Claiming `ok` there would assert something we cannot support — a
consumer reads `ok` as *worked*, whatever the vocabulary says it means.
"""

LOCAL_ONLY = frozenset(
    {"text", "arguments", "result", "error_text", "working_directory", "machine", "user"}
)


def span_id(session_id: str, turn_key: str, call_index: int) -> str:
    """Identity for one tool call, stable across re-reads of a growing session.

    Derived from the session, the turn's own stable key, and the call's position
    *within that turn*. Appending later turns cannot move it, and a skipped
    record cannot shift it, because nothing counts from the start of the session.
    """
    return f"{session_id}:{turn_key}:{call_index}"
```

`ToolCall` carries `span`, `tool_name`, `mcp_server`, `status`, `arguments`, `result`, `result_size` (characters, not bytes), `error_text`, `truncated` — and **no timings**: the dependency's schema has no timestamp below the session, and a field that is always empty invites consumers to build on a value that never arrives. `Turn` carries `position`, `key`, `role`, `text`, `is_sidechain`, `tool_calls`. `Session` carries `session_id`, `agent_kind`, `source`, `started_at`, `model`, `working_directory`, `machine`, `user`, `turns`; `source` sits beside `agent_kind` so a consumer can select on the agent's own name for itself, including for a kind that maps to nothing.

`machine` and `user` are named for what they are, not for what a reader can
promise about them: the dependency's event schema only carries a hostname and
username when a parser sets them, and for Claude Code none does, so the
dependency's own `__post_init__` fills the gap from the *collecting* machine's
environment. A transcript copied from another machine and read here reports
this machine, silently. This is a per-kind fidelity question — a parser that
does read the value from the transcript should set it there — so it is
recorded as a caveat on the field, not solved by removing it.

- [x] **Step 5: Run the gate** — `uv run pytest tests/test_model.py -q && uv run pyright && uv run ruff check . && uv run ruff format --check .`, expect PASS.

- [x] **Step 6: Commit**

```bash
git add src/openaidr/model.py tests/test_model.py
git commit -m "feat(model): session model and stable span identity"
```

---

### Task 2: Source vocabulary — agent kinds

**Files:** Create `src/openaidr/kinds.py`; test `tests/test_kinds.py`.

**Interfaces:** Produces `AGENT_KIND_BY_SOURCE`, `map_source(source) -> str | None`, `KindSelection.includes(kind) -> bool`, `parse_kind_filter(values) -> KindSelection`. Consumes nothing.

- [x] **Step 1: Write the failing test**

```python
from openaidr.kinds import map_source, parse_kind_filter


def test_known_source_maps_to_an_agent_kind() -> None:
    assert map_source("claude") == "claude-code"


def test_unknown_source_is_unmapped_rather_than_guessed() -> None:
    """Unmapped is a coverage statement, not an error and not a guess."""
    assert map_source("some-new-agent") is None


def test_no_filter_includes_every_kind_including_the_unmapped() -> None:
    selection = parse_kind_filter(None)
    assert selection.includes("claude-code")
    assert selection.includes(None)


def test_explicit_filter_excludes_other_kinds_and_the_unmapped() -> None:
    selection = parse_kind_filter(["claude-code"])
    assert selection.includes("claude-code")
    assert not selection.includes("cursor")
    assert not selection.includes(None)
```

- [x] **Step 2: Run test to verify it fails** — `ModuleNotFoundError: No module named 'openaidr.kinds'`.

- [x] **Step 3: Write the implementation**

```python
#: On-disk self-name -> agent kind. Only kinds this package can actually read
#: belong here; adding a row is a coverage claim.
AGENT_KIND_BY_SOURCE = {"claude": "claude-code"}


def map_source(source: str) -> str | None:
    """Return the agent kind for an on-disk source name, or None if unmapped.

    None is a *coverage statement* — the session is still collected and still
    counted; it simply resolves less. It is never an error and never a guess.
    """
    return AGENT_KIND_BY_SOURCE.get(source)


@dataclass(frozen=True)
class KindSelection:
    kinds: frozenset[str] | None

    def includes(self, kind: str | None) -> bool:
        if self.kinds is None:
            return True
        return kind is not None and kind in self.kinds
```

- [x] **Step 4: Run the gate** — `uv run pytest tests/test_kinds.py -q && uv run pyright && uv run ruff check .`

- [x] **Step 5: Commit**

```bash
git add src/openaidr/kinds.py tests/test_kinds.py
git commit -m "feat(kinds): map on-disk source names to agent kinds"
```

---

### Task 3: Tool-name normalisation, per kind

**Files:** Create `src/openaidr/toolnames.py`; test `tests/test_toolnames.py`.

**Interfaces:** Produces `split_tool_name(agent_kind: str | None, raw: str) -> tuple[str | None, str]`. Consumes nothing.

- [x] **Step 1: Write the failing test**

```python
import pytest

from openaidr.toolnames import split_tool_name


def test_claude_code_mcp_tool_splits_on_the_doubled_underscore() -> None:
    assert split_tool_name("claude-code", "mcp__github__get_issue") == ("github", "get_issue")


def test_claude_code_builtin_tool_has_no_server() -> None:
    assert split_tool_name("claude-code", "Read") == (None, "Read")


def test_server_name_containing_a_single_underscore_survives() -> None:
    """A general pattern would mis-split here and name the *wrong* server."""
    assert split_tool_name("claude-code", "mcp__my_server__do_thing") == ("my_server", "do_thing")


def test_tool_name_containing_the_delimiter_keeps_its_tail() -> None:
    assert split_tool_name("claude-code", "mcp__srv__a__b") == ("srv", "a__b")


def test_unknown_kind_never_guesses_a_server() -> None:
    """No global fallback pattern: a silent wrong answer is worse than a gap."""
    assert split_tool_name(None, "mcp__github__get_issue") == (None, "mcp__github__get_issue")


@pytest.mark.parametrize("raw", ["mcp__", "mcp__github", "mcp____x"])
def test_malformed_claude_names_are_left_whole(raw: str) -> None:
    assert split_tool_name("claude-code", raw) == (None, raw)
```

- [x] **Step 2: Run test to verify it fails** — `ModuleNotFoundError: No module named 'openaidr.toolnames'`.

- [x] **Step 3: Write the implementation**

```python
_CLAUDE_PREFIX = "mcp__"
_CLAUDE_SEP = "__"


def split_tool_name(agent_kind: str | None, raw: str) -> tuple[str | None, str]:
    """Split a raw tool name into `(mcp_server, tool_name)` for one agent kind.

    An unknown kind returns the name whole with no server. There is deliberately
    no general fallback pattern: applied to the wrong kind it does not fail to
    find a server, it finds the wrong one — which then resolves to the wrong
    component downstream.
    """
    if agent_kind == "claude-code":
        return _split_claude_code(raw)
    return None, raw


def _split_claude_code(raw: str) -> tuple[str | None, str]:
    if not raw.startswith(_CLAUDE_PREFIX):
        return None, raw
    remainder = raw[len(_CLAUDE_PREFIX) :]
    server, separator, tool = remainder.partition(_CLAUDE_SEP)
    if not separator or not server or not tool:
        return None, raw
    return server, tool
```

- [x] **Step 4: Run the gate** — `uv run pytest tests/test_toolnames.py -q && uv run pyright && uv run ruff check .`

- [x] **Step 5: Commit**

```bash
git add src/openaidr/toolnames.py tests/test_toolnames.py
git commit -m "feat(toolnames): per-kind (server, tool) normalisation"
```

---

### Task 4: The per-kind collection contract

**Files:** Create `src/openaidr/readers/__init__.py`, `src/openaidr/readers/base.py`; test `tests/test_readers_base.py`.

**Interfaces:** Produces `Window(since: datetime | None)`; `Reader` Protocol with `agent_kind: str` and `collect(window) -> tuple[list[Session], list[ReaderFailure]]`; `ReaderFailure(agent_kind, message)`; `collect_from(readers, window) -> tuple[list[Session], list[ReaderFailure]]`. Consumes `Session` (Task 1).

A reader returns its own failures rather than only raising, because the
failures this contract must carry are not all exceptions: a per-kind source
can legitimately swallow a per-file error internally (§Task 5) and report it
only as an empty result, which is indistinguishable from "nothing here" unless
the reader itself surfaces the diagnostic. `collect_from` still isolates an
*unexpected* exception escaping `collect` entirely — that case remains, for a
failure mode no reader anticipated — but a reader's own known per-file
diagnostics travel back as data, not as control flow.

- [x] **Step 1: Write the failing test**

`_StubReader` returns a fixed list of sessions and no failures; `_ReportingReader` returns no sessions and one `ReaderFailure` of its own — the case where a reader has already caught a per-file problem and reports it as data, per Task 5; `_ExplodingReader` raises `RuntimeError("unreadable database")` from `collect` and declares `agent_kind = "cursor"` — the case nothing anticipated.

```python
def test_collects_from_every_reader() -> None:
    readers = [_StubReader("claude-code", [_session("a")])]
    sessions, failures = collect_from(readers, Window(since=None))
    assert [s.session_id for s in sessions] == ["a"]
    assert failures == []


def test_a_readers_own_reported_failure_survives_collect_from() -> None:
    """A reader that already caught its own error still gets to report it."""
    readers = [_StubReader("claude-code", [_session("a")]), _ReportingReader()]
    sessions, failures = collect_from(readers, Window(since=None))
    assert [s.session_id for s in sessions] == ["a"]
    assert failures == [ReaderFailure(agent_kind="cursor", message="cursor.db: locked")]


def test_one_failing_kind_is_isolated_and_reported() -> None:
    """An exception escaping one kind's `collect` is reported and isolated, never aborting the others."""
    readers = [_StubReader("claude-code", [_session("a")]), _ExplodingReader()]
    sessions, failures = collect_from(readers, Window(since=None))
    assert [s.session_id for s in sessions] == ["a"]
    assert len(failures) == 1
    assert failures[0].agent_kind == "cursor"
    assert "unreadable database" in failures[0].message
```

- [x] **Step 2: Run test to verify it fails** — `ModuleNotFoundError: No module named 'openaidr.readers'`.

- [x] **Step 3: Write the implementation**

`Window.since`, when set, must be timezone-aware: every timestamp parsed from a session record is, and comparing a naive value raises rather than silently misbehaving.

```python
def collect_from(
    readers: Sequence[Reader], window: Window
) -> tuple[list[Session], list[ReaderFailure]]:
    """Collect from every reader, isolating failures to the kind that produced them.

    A reader's own returned failures are carried through unchanged — it has
    already isolated them to one file or one record. Only an exception
    escaping `collect` entirely is caught here, for the failure mode no reader
    anticipated.
    """
    sessions: list[Session] = []
    failures: list[ReaderFailure] = []
    for reader in readers:
        try:
            reader_sessions, reader_failures = reader.collect(window)
        except Exception as error:  # noqa: BLE001 - isolation is the point
            failures.append(ReaderFailure(agent_kind=reader.agent_kind, message=str(error)))
            continue
        sessions.extend(reader_sessions)
        failures.extend(reader_failures)
    return sessions, failures
```

Which implementation backs a kind — one of ours, or the dependency — is a per-kind choice made on measured fidelity, and it reaches no consumer.

- [x] **Step 4: Run the gate** — `uv run pytest tests/test_readers_base.py -q && uv run pyright && uv run ruff check .`

- [x] **Step 5: Commit**

```bash
git add src/openaidr/readers tests/test_readers_base.py
git commit -m "feat(readers): per-kind collection contract with failure isolation"
```

---

### Task 5: The Claude Code reader over `adr-sensor`

**Files:** Create `src/openaidr/readers/claude_code.py`, `tests/fixtures/__init__.py`, `tests/fixtures/claude_jsonl.py`; test `tests/test_claude_code.py`.

**Interfaces:** Produces `ClaudeCodeReader` with `agent_kind = "claude-code"`, `__init__(root: Path | None = None)`, `collect(window) -> tuple[list[Session], list[ReaderFailure]]`; `DEFAULT_ROOT`; `SOURCE = "claude"`. Consumes Task 1's model and `span_id`, Task 2's `map_source`, Task 3's `split_tool_name`, Task 4's `Window` and `ReaderFailure`, and `adr_sensor.parsers.claude_parser.ClaudeParser` plus `adr_sensor.schemas.agent_event_schema.{AgentEvent, ChatMessage, ToolUsage}`.

**What upstream's own failure handling means for this reader.** `ClaudeParser.parse_jsonl_file` already catches a file it cannot open or decode, prints one line naming the file and the error, and returns whatever sessions it managed rather than raising — often none. A bare `try/except` around the call therefore does not see that failure at all: it already happened, silently, inside the call that succeeded. The only surviving trace is the line upstream printed, and this reader must capture it rather than discard it. A malformed *line* inside an otherwise-readable file is a second, narrower gap: upstream's per-line `JSONDecodeError` handling drops that line with no print at all, so nothing observable survives it to report — accepted here because detecting it would need a second, independent parse of the same bytes, which the design already rejects for the same reason it rejects one for truncation and timings (see "What we want from the dependency and cannot get yet").

- [x] **Step 1: Write the fixture builders**

`tests/fixtures/claude_jsonl.py` writes records shaped like real Claude Code JSONL — `sessionId`, `uuid`, `timestamp`, `isSidechain`, `cwd`, `message`, and for outcomes a `tool_result` content block — with `write_session(root, project, records)` writing `<root>/<project>/<sessionId>.jsonl`.

**Fixture text must exceed five characters and say something.** The dependency drops a session whose single message is five characters or fewer and calls no tool, so `"hi"` silently produces no session and a test written against it asserts nothing.

- [x] **Step 2: Write the failing test**

`tests/test_claude_code.py` covers, one test each: session identity is `claude-code:<agent id>`; `source` is retained as `"claude"`; a subagent transcript under `<root>/-p/s1/subagents/agent-abc123.jsonl` becomes `claude-code:s1:agent-abc123` while its parent stays `claude-code:s1`; every turn in a subagent transcript is `is_sidechain`; a repeated `sequence_id` yields unique turn keys with the first unchanged; a returned call is `unknown` not `ok`; an unresolved call is `pending`; a result body reading `"The user doesn't want to proceed with this tool use."` is `rejected`; one reading `"This command requires approval"` is `rejected`; one reading `"error: exit code 1"` is **not** `rejected`; `mcp__github__get_issue` splits to `("github", "get_issue")`; a built-in tool has no server; upstream's progress output never reaches stdout (`capsys.readouterr().out == ""`); a session the dependency judges trivial is not reported; and the window excludes files modified before it.

The stability contract is proven against the reader itself, not only against the pure `span_id` formatter: the same file is read twice, once with one pending call and once after the file has grown a result for that call plus a later turn and call — the first call's span is unchanged and its status has moved from `pending` to `unknown`, while the later call gets a span of its own. Appending is the only way the fixture grows between reads, matching the append-only assumption ADR-0001 already records.

The evidence for `"requires approval"`/`"requested permissions"` as *denial* wording, specifically, does not have a recorded corpus or method anywhere in this repository — the 88%/97% figures in the spec need one before this reader can claim them for those two markers. Any test asserting on that exact wording stays in place, but passing it is not treated as closing this gap on its own; the corpus/method itself is the open acceptance criterion.

Two more, for what upstream's own failure handling hides: a file upstream cannot open or fully decode does not lose the other sessions *and* produces one `ReaderFailure` naming that file, rather than silently returning as if it had no sessions; and a result truncated by `truncate_middle`'s default `edge_chars=400` is `truncated` — the marker sits inside the string, not within its last 200 characters, so a boundary test must place enough real content after the marker to catch an implementation that only checks a fixed-size suffix.

- [x] **Step 3: Run test to verify it fails** — `ModuleNotFoundError: No module named 'openaidr.readers.claude_code'`.

- [x] **Step 4: Write the implementation**

Discovery walks `root.glob("**/*.jsonl")` and calls `ClaudeParser().parse_jsonl_file(path)` per file, capturing upstream's stdout narration with `contextlib.redirect_stdout` so it cannot corrupt machine-readable output — but the capture is read, not merely discarded: `parse_jsonl_file`'s only `print` is inside its own outer exception handler, so a non-empty capture is never ordinary progress narration, it means upstream caught something this reader could not otherwise see. That capture becomes one `ReaderFailure(agent_kind="claude-code", message=f"{path}: {captured.strip()}")` rather than a silently empty result, and the exact printed prefix (`"[CLAUDE] Error reading ..."`) is asserted in the test against the pinned `adr-sensor==1.0.0`, so a later pin bump that changes it is a deliberate re-check rather than silent drift. A file that raises past that (permission, encoding this reader itself cannot even hand to upstream) is reported the same way, by message, rather than skipped. Identity strips upstream's `claude_` prefix, and a file whose parent directory is `subagents` becomes `<kind>:<parent id>:<file stem>` with every turn marked sidechain.

Turn keys come from `ChatMessage.sequence_id`, disambiguated by occurrence so only a later repeat is suffixed:

```python
    turns: list[Turn] = []
    seen: dict[str, int] = {}
    for message in messages:
        base = message.sequence_id or f"turn-{len(turns)}"
        occurrence = seen.get(base, 0)
        seen[base] = occurrence + 1
        key = base if occurrence == 0 else f"{base}#{occurrence}"
```

Server recovery prefers what upstream populated, because some parsers set `server_name` and the Claude one does not:

```python
def _server_and_tool(tool: ToolUsage) -> tuple[str | None, str]:
    server, name = split_tool_name("claude-code", tool.tool_name)
    if tool.server_name:
        return tool.server_name, name
    return server, name
```

Outcome normalisation asserts only what the surviving evidence supports:

```python
_DENIAL_MARKERS = (
    "doesn't want to proceed",
    "requested permissions",
    "requires approval",
    "requires permission",
    "user rejected",
    "operation was rejected",
)


def _status(tool: ToolUsage) -> Status:
    if tool.result is None:
        return "pending"
    body = tool.result.lower()
    if any(marker in body for marker in _DENIAL_MARKERS):
        return "rejected"
    if (tool.status or "").lower() == "error" or tool.error:
        return "error"
    return "unknown"
```

`pending` is structural. `rejected` is inferred from the result body, the only place a refusal survives upstream's schema — measured at 88% recall and 97% precision against real transcripts, and kept narrow because a false `rejected` claims a person made a decision they did not make. `error` comes only from an explicit upstream signal: inferring it from result text scored 70% recall at poor precision. Everything else is `unknown` rather than `ok` for this kind, because upstream's `success` means only that a result exists — it never reads `is_error`.

**The `rejected` markers are not evenly evidenced.** `"doesn't want to proceed"` and `"user rejected"` name a decision directly. `"requires approval"` and `"requested permissions"` name a *state* — access not yet granted — that a live session and a stalled one can share; whether the transcripts behind the 88%/97% figures actually distinguish those is not something this plan can currently point to anything in the repository to confirm.

- [ ] **Acceptance: record the denial-marker corpus and labelling method** — a
  short reference file naming the sample transcripts and how each was
  labelled, precisely because a false `rejected` is the one mistake this
  design calls worse than `unknown`. Not yet done: no such file exists in this
  repository. Until it does, the spec's 88% recall / 97% precision figures
  are unverified for `"requires approval"` and `"requested permissions"`
  specifically — the marker-table tests below prove the code follows its own
  table, not that these two entries carry the meaning the figures claim.
  Left open rather than closed by the tests that do exist. This item blocks
  the milestone's acceptance, not only its documentation: the corpus is what
  would show these two markers mean a completed decline rather than an
  unresolved approval state, and that question stays open until it lands.

Truncation is a second inference from the same result body, and needs its own rule rather than inheriting the denial scan's. Upstream's `truncate_middle` inserts a literal, parseable marker between the text it kept from the start and the text it kept from the end — `... [truncated N chars] ...` — and the `N` it carries is the count of what it removed, which is what makes the original size exact rather than estimated:

```python
_TRUNCATION_MARKER = re.compile(r"\.\.\. \[truncated (\d+) chars\] \.\.\.")


def _truncation(result: str | None) -> tuple[bool, int | None]:
    """`result_size` reports the size of what the agent actually produced, not
    the size of the abridged copy held here — reporting the copy would
    understate every long result, and the marker's own count makes the true
    figure exact."""
    if result is None:
        return False, None
    match = _TRUNCATION_MARKER.search(result)
    if match is None:
        return False, len(result)
    removed = int(match.group(1))
    return True, len(result) - len(match.group(0)) + removed
```

- [x] **Step 5: Run the gate** — `uv run pytest -q && uv run pyright && uv run ruff check . && uv run ruff format --check .`

- [x] **Step 6: Commit**

```bash
git add src/openaidr/readers/claude_code.py tests/fixtures tests/test_claude_code.py
git commit -m "feat(claude-code): sessions, turns and outcomes over adr-sensor"
```

---

### Task 6: The collector

**Files:** Create `src/openaidr/collector.py`; test `tests/test_collector.py`.

**Interfaces:** Produces `Collection(sessions, failures)`, `default_readers(root=None) -> list[Reader]`, `collect(selection, window, readers=None) -> Collection`. Consumes Task 4's contract, Task 5's reader, Task 2's `KindSelection`.

- [x] **Step 1: Write the failing test**

```python
def test_collects_claude_code_sessions_by_default(tmp_path: Path) -> None:
    result = collect(parse_kind_filter(None), Window(since=None), default_readers(root=tmp_path))
    assert [s.session_id for s in result.sessions] == ["claude-code:s1"]
    assert result.failures == []


def test_a_kind_filter_drops_other_kinds_outright(tmp_path: Path) -> None:
    """A user-excluded kind is a user instruction, not a coverage gap."""
    assert [s.agent_kind for s in result.sessions] == ["claude-code"]


def test_sessions_are_ordered_newest_first(tmp_path: Path) -> None:
    assert [s.session_id for s in result.sessions] == ["claude-code:s2", "claude-code:s1"]


def test_a_repeated_session_identity_is_reported_not_silently_replaced() -> None:
    """Two readers naming the same identity is a collision this design does
    not expect from one source's own contract; the session already held wins,
    and the collision is reported rather than one copy vanishing unremarked."""


def test_a_nonexistent_or_empty_root_returns_no_sessions_and_no_failure(tmp_path: Path) -> None:
    """A root nobody has written to yet is not this collector's failure to report."""
```

Also covered: a nonexistent root and an empty root both collect cleanly to
nothing, with no `ReaderFailure` for either — a directory with nothing in it
yet is not evidence of a broken reader.

- [x] **Step 2: Run test to verify it fails** — `ModuleNotFoundError: No module named 'openaidr.collector'`.

- [x] **Step 3: Write the implementation** — one cold pass, held in memory, newest session first, readers filtered by the selection. Nothing is persisted: a serialize-and-reparse round trip would add latency to the path that most needs speed and would put transcript content on disk that nothing else here creates.

- [x] **Step 4: Run the gate** — `uv run pytest -q && uv run pyright && uv run ruff check .`

- [x] **Step 5: Commit**

```bash
git add src/openaidr/collector.py tests/test_collector.py
git commit -m "feat(collector): cold pass over selected kinds, in memory"
```

---

### Task 7: `openaidr sessions`

**Files:** Create `src/openaidr/render.py`; modify `src/openaidr/__main__.py`; test `tests/test_render.py`, `tests/test_cli.py`.

**Interfaces:** Produces `render_text(collection) -> str`, `render_json(collection) -> str`, `parse_since(value) -> datetime | None`, and a `sessions` subcommand with `--agent-kind` (repeatable), `--since`, `--format`. Consumes Task 6's `Collection`, Task 2's `parse_kind_filter`, Task 4's `Window`.

- [x] **Step 1: Write the failing test**

```python
def test_text_output_names_the_tool_its_server_status_and_size() -> None:
    assert "github/get_issue" in output
    assert "rejected" in output
    assert "6c" in output  # characters, not bytes


def test_text_leaves_an_unknown_outcome_blank_rather_than_claiming_ok() -> None:
    """A column repeating one word is not information; a blank claims nothing."""
    assert "unknown" not in rendered_rows


def test_text_states_the_unknown_coverage_once() -> None:
    """Suppressing the word per row must not suppress the fact."""
    assert "1 of 1 calls returned with no outcome" in output


def test_json_keeps_the_unknown_status_explicit() -> None:
    """A machine consumer must see the state, not infer it from a missing key."""
    assert call["status"] == "unknown"


def test_text_reports_a_failed_kind_rather_than_hiding_it() -> None:
    assert "cursor" in output and "unreadable" in output


def test_parse_since_rejects_nonsense() -> None:
    with pytest.raises(ValueError):
        parse_since("last tuesday")


def test_parse_since_rejects_a_negative_or_zero_count() -> None:
    """A negative count already fails `_SINCE_PATTERN` (`\\d+` has no sign) and
    needs no change. `0d` currently matches and returns `now` — a window of
    zero days is not `None`'s "everything", it is a cut-off nothing after this
    instant can ever be newer than, which is a plausible-looking but empty
    request; reject it explicitly rather than let it silently collect nothing
    every time."""
    with pytest.raises(ValueError):
        parse_since("0d")
    with pytest.raises(ValueError):
        parse_since("-3d")


def test_json_document_is_not_the_public_descriptor_contract() -> None:
    """`working_directory` is `LOCAL_ONLY` in the model, and `render_json` is a
    local surface allowed to carry it for the machine that produced it — but
    the JSON document is not `public_descriptor()`'s own, narrower contract for
    what a consumer may carry outward, and it must not be mistaken for one."""
    document = json.loads(render_json(collection))
    assert set(document["sessions"][0]) > set(collection.sessions[0].public_descriptor())
```

`tests/test_cli.py` keeps the console-script test, adds one asserting `sessions` prints a collected session, and adds one asserting an unrecognised `--agent-kind` value collects zero sessions with no crash and no silent success claim — the CLI's own help text, not a runtime warning, is where that vocabulary is discoverable today, and this test exists so a future change to that contract is a deliberate one.

- [x] **Step 2: Run test to verify it fails** — `ModuleNotFoundError: No module named 'openaidr.render'`.

- [x] **Step 3: Write the implementation**

Text rendering shows status, result size in characters, and the tool — blanking status when it is `unknown`, and closing the run with one coverage line naming how many outcomes could not be established. JSON keeps `status` explicit, because a machine consumer must see the state rather than infer it from a missing key; it carries `public_descriptor()`'s fields plus `model`, `source` and `working_directory` — a wider, explicitly local shape, not `public_descriptor()`'s own contract — and withholds turn text, arguments, results and error text. The CLI is `argparse` — no new runtime dependency — and `--since` accepts a positive day count, rejecting zero, a negative count, or anything else with a usage error rather than a traceback or a silently empty window. `--agent-kind` takes any string and filters on it rather than validating it against `AGENT_KIND_BY_SOURCE`, so a typo collects zero sessions rather than failing loudly; narrowing that is future work; the test above records today's behaviour as a deliberate baseline rather than an oversight.

- [x] **Step 4: Run the gate** — `uv run pytest -q && uv run pyright && uv run ruff check . && uv run ruff format --check .`

- [x] **Step 5: Commit**

```bash
git add src/openaidr/render.py src/openaidr/__main__.py tests/test_render.py tests/test_cli.py
git commit -m "feat(cli): openaidr sessions"
```

---

### Task 8: End-to-end proof and documentation

**Files:** Create `tests/test_end_to_end.py`; modify `CLAUDE.md`, `README.md`, `src/openaidr/render.py`.

- [x] **Step 1: Write the failing test** — one realistic session through the installed console script, asserting the statuses this reader can emit (`unknown`, `rejected`, `pending`), the `(server, tool)` split, and that no conversation text appears in the JSON document.

- [x] **Step 2: Run the full gate** — `uv run pytest -q && uv run pyright && uv run ruff check . && uv run ruff format --check .`

- [x] **Step 3: Update the docs** — `CLAUDE.md`'s command block gains `openaidr sessions` and `--format json`; `README.md` shows real output and states what is not yet delivered; every touched module's own docstring is checked against what it actually does, not only the user-facing docs — `render.py`'s claimed "how long it took" with no per-call timing field modelled anywhere.

- [x] **Step 4: Measure scale figures against a real developer machine, as this milestone's P0** — ran `openaidr sessions --since 14d --format json` against a machine with two weeks of real Claude Code history and recorded session count and the turn-count distribution's shape: 332 sessions (114 of them subagent transcripts), 18,111 tool calls, turns ranging 2 to 2,105 with a median of 32, and the busiest tenth of sessions holding 54% of all calls — consistent with the spec's qualitative claim, more sessions than its former "on the order of a hundred" phrasing suggested. The figures are recorded in `docs/specs/session-collection.md`'s "Scale" section.

- [x] **Step 5: Commit**

```bash
git add tests/test_end_to_end.py CLAUDE.md README.md
git commit -m "test: end-to-end session collection; docs: openaidr sessions"
```

---

## Out of scope

Named so a reviewer does not read their absence as an oversight. Each is in `docs/specs/session-collection.md`:

- **Incremental collection** — watermarks, changed-file re-reads, the per-session content hash. This is one cold pass.
- **The other six agent kinds**, each a reader behind the existing contract.
- **A second pass over the file to recover fields the dependency drops** — per-call timings, `is_error`, `toolDenialKind`, `tool_use.id`, `isSidechain`. It would recover all of them, and it would put per-format knowledge back inside this package while risking a silent misjoin between two readings of one file. The gap is carried visibly instead, and closes upstream.
- **`interrupted`** — in the `Status` type for kinds that can supply it; not emitted by this reader.
