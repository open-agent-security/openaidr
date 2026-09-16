"""The session model: three nested levels, and the identity consumers address rows by.

Naming and representation are this module's choice; what the spec fixes is the
information captured and the stability of span identity.

What is *absent* is deliberate, and a field earns its place by being fillable
from evidence. `duration_ms` is modelled because the transcript records both ends
of a call even though the parsing dependency's event schema does not; the MCP
fields are modelled because Claude Code writes a connection log this package can
read. A field nothing can fill is a worse answer than no field -- it invites
consumers to build on a value that never arrives -- and one that is *sometimes*
fillable must say which case it is in, which is why `mcp_log_state` exists beside
`mcp_connections`. See `docs/specs/session-collection.md`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Status = Literal["ok", "error", "rejected", "interrupted", "pending", "unknown"]
"""How a call ended.

`unknown` is not a failure state and not a placeholder: it means the call ran and
returned, and the evidence reaching this package does not say whether it
succeeded. Claiming `ok` there would assert something we cannot support — a
consumer reads `ok` as *worked*, whatever the vocabulary says it means.
"""

#: Information that serves correlation and local rendering only. It never enters
#: a consumer's finding, so it is named here rather than left to convention.
LOCAL_ONLY = frozenset(
    {
        "text",
        "arguments",
        "result",
        "error_text",
        "working_directory",
        "machine",
        "user",
        # A context item's body is conversation material on the same terms as a
        # turn's text, and an initiating prompt is a person's own words.
        "context_items",
        "initial_prompt",
        # An MCP server's own words about why a connection failed. Free text the
        # server author chose, and it can carry a URL, a header fragment or a
        # path. `failure_category` is the part a consumer acts on and the part
        # that travels.
        "failure_detail",
    }
)


def span_id(session_id: str, turn_key: str, call_index: int) -> str:
    """Identity for one tool call, stable across re-reads of a growing session.

    Derived from the session, the turn's own stable key, and the call's position
    *within that turn*. Appending later turns cannot move it, and a skipped
    record cannot shift it, because nothing here counts from the start of the
    session. See ADR-0001.
    """
    return f"{session_id}:{turn_key}:{call_index}"


@dataclass(frozen=True)
class ToolCall:
    """One invocation and its outcome."""

    span: str
    tool_name: str
    mcp_server: str | None
    status: Status
    arguments: dict[str, object]
    result: str | None
    #: Characters, not bytes: the length of the result as text.
    result_size: int | None
    error_text: str | None
    truncated: bool
    #: The agent's own identifier for this call, where the transcript records
    #: one. Span identity is still derived (ADR-0001) — changing what a span is
    #: would move every row a consumer has already addressed — but carrying the
    #: provider's id lets a consumer correlate with anything else that saw it.
    provider_call_id: str | None = None
    #: Wall-clock milliseconds between the call being issued and its result
    #: arriving, where the transcript records both ends. None where it does not:
    #: a call still running is not a fast one.
    duration_ms: int | None = None
    #: Which kind of refusal, when `status` is `rejected`; None otherwise.
    #: Read from the transcript, never inferred. A person declining and an
    #: automated classifier blocking are both refusals and both `rejected`, but
    #: they are not the same event to anything reasoning about intent.
    denial_kind: str | None = None
    #: The directory this call ran in, which is not always the session's. A
    #: session can `cd`, and a relative path in an argument means nothing
    #: without the directory it was relative to — a consumer asking *does this
    #: write govern this agent* cannot answer it from the session-level value.
    working_directory: str | None = None
    #: The skill this call is attributed to, where the transcript records one.
    #: This is the agent's own attribution, not an inference: it says which
    #: skill was in effect, which is otherwise reconstructed by name-matching
    #: against a component inventory and can be wrong.
    attributed_skill: str | None = None
    #: The plugin this call is attributed to, on the same terms.
    attributed_plugin: str | None = None
    #: Which transport carried this call, for an MCP call whose connection log
    #: could be read. `None` everywhere else, including every non-MCP call.
    #:
    #: Recorded, not interpreted. That `stdio` is a local pipe and
    #: `claudeai-proxy` is not are conclusions a consumer draws; this package
    #: reports what the client negotiated and stops there.
    transport: str | None = None


@dataclass(frozen=True)
class ContextItem:
    """Material that reached the model without being a user turn or a tool result.

    **The third provenance class**, and the reason it is modelled separately.
    A consumer judging instruction-shaped content asks where it came from: a
    user's own turn is a person talking to their agent, a tool result is
    someone else's instruction arriving through data. This is a third thing —
    hook output, a skill's or agent's *description*, an MCP server's own
    instructions, a nested memory file — injected into the conversation by the
    harness. Measured on 316 real transcripts it is the second-largest record
    type at 15,653 occurrences, and a consumer reading only turns and results
    cannot see any of it.

    `span` follows the same shape as a tool call's so a finding can cite one
    the same way, without a consumer learning a second addressing scheme.
    """

    span: str
    #: What produced it: `hook`, `skill_listing`, `agent_listing`,
    #: `mcp_instructions`, `file`, `memory`, `mode`, or the raw attachment type
    #: where it is none of those. Never inferred from the content.
    source: str
    #: The hook, skill or file it came from, where the record names one.
    name: str | None
    #: The material itself. Local-only, like a turn's text.
    text: str


@dataclass(frozen=True)
class Compaction:
    """A point where the transcript was summarised and earlier turns dropped.

    Modelled because it bounds what a consumer may conclude. After a boundary
    the original request is gone, so a question of the form *does this trace
    back to what was asked* is being answered against a summary — and a
    consumer that cannot see the boundary answers it without knowing that.
    """

    #: What caused it: `manual` or `auto`.
    trigger: str
    #: Tokens in the conversation immediately before, where recorded.
    pre_tokens: int | None
    #: Tokens dropped cumulatively. Measured, one real session dropped 968,516.
    dropped_tokens: int | None


@dataclass(frozen=True)
class ProviderRefusal:
    """The provider's own safeguards declining, and the model swap that followed.

    Distinct from `ToolCall.denial_kind`, which is the user-and-permission
    side. This is the vendor refusing the request, after which the CLI
    continues on a different model — a substitution a consumer reading `model`
    alone cannot see.
    """

    #: The category the provider assigned, e.g. `cyber`.
    category: str | None
    original_model: str | None
    fallback_model: str | None


@dataclass(frozen=True)
class Turn:
    """One exchange."""

    position: int
    key: str
    role: str
    text: str
    is_sidechain: bool
    tool_calls: tuple[ToolCall, ...]
    #: Which permission mode was in effect at this turn, where the transcript
    #: places it; None where it does not. Positional rather than per-session
    #: because it *changes mid-session*: a session can enter `bypassPermissions`
    #: partway through, and only the turns after that point ran unguarded.
    permission_mode: str | None = None
    #: When the record that produced this turn was written, as an instant in
    #: UTC. None where the record carried no timestamp, or no identity for the
    #: join to land on -- never a neighbouring turn's value and never the
    #: moment of collection.
    #:
    #: This is the finest clock the model carries, and it is the record's, not
    #: the call's: every tool call issued in one record shares it, so within a
    #: turn only span order separates them. A tool call therefore carries no
    #: absolute time of its own -- its start is this, and its end follows from
    #: this and its `duration_ms` (ADR-0010).
    occurred_at: datetime | None = None


MCPLogState = Literal[
    "applied",
    "no_log_root",
    "log_root_unreadable",
    "no_log_for_session",
    "count_mismatch",
    "session_id_collision",
    "transcript_discovery_incomplete",
    "log_discovery_incomplete",
    "not_attempted",
]
"""Whether this session's MCP connection log could be read, and why not.

Nine states rather than a boolean, because they call for different responses.
A missing cache root is very likely an unsupported platform and is a property
of the machine; an unreadable one is a permission or transient filesystem
failure on a root that does exist, and must not read the same as a platform
that never had one -- collapsing the two would make "we could not look" the
same as "there was nothing there" for the one case a directory listing can
actually distinguish (unlike a listed file this package never had a name for,
see ADR-0008); a missing log for one session is a pruned cache; a count
mismatch is the guard in `claude_code_mcp` declining to attribute outcomes it
cannot place; a session id collision is more than one project claiming this
session's id -- two projects having filed a log under it, two transcript
projects sharing it, or both -- so nothing found under that id can be shown to
be this session's alone; and the two `*_discovery_incomplete` states are a
directory -- on the transcript side or the cache side -- that this pass could
not scan at all, which is weaker evidence than any of the above and therefore
withholds more: a scan that yields no file *names* cannot establish that the
entries it did find are every entry filed under this session id, and that
uniqueness is the precondition the whole join rests on (ADR-0009). Collapsing
them would make "we could not look" indistinguishable from "there was nothing
to find" -- the confusion this package exists to avoid.

`applied` does not mean every call got an outcome: a server whose log was pruned
while another's survived leaves some calls unenriched within an applied session.
"""


@dataclass(frozen=True)
class MCPConnection:
    """One MCP server as the client saw it, for one session.

    Connection-scoped, deliberately. Transport, advertised identity and whether
    the server came up are properties of the *connection*, not of any call that
    went over it, and repeating them on every call would say otherwise.

    Client-observed throughout: the transport is negotiated and recorded by the
    client, and the advertised name and version come off the initialize
    handshake. That is what makes this evidence rather than a restatement of
    what a server's manifest claims about itself.
    """

    #: The log directory's name for this server, which is the client's own
    #: identifier for it -- not necessarily what the server calls itself.
    server: str
    transport: str | None = None
    #: The URL for an HTTP transport, or the proxy's server id. An operator
    #: coordinate, on the same terms an MCP URL already travels in a BOM.
    endpoint: str | None = None
    #: What the server called itself on the initialize handshake, and its
    #: version. A cross-run identity that nothing in the transcript carries.
    advertised_name: str | None = None
    advertised_version: str | None = None
    #: `None` when the log never stated an outcome for this connection --
    #: still being established, or observed only through a line the reader
    #: does not recognise. Not the same as `False`: absence of evidence a
    #: connection failed is not evidence it did.
    connected: bool | None = None
    #: Why the connection failed, in a fixed vocabulary a consumer can act on:
    #: `auth`, `timeout`, `http_status`, `network`, `protocol`, `unknown`.
    failure_category: str | None = None
    #: The server's own words. `LOCAL_ONLY`.
    failure_detail: str | None = None
    #: How long the connection attempt took, successful or not.
    duration_ms: int | None = None


@dataclass(frozen=True)
class Session:
    """One agent conversation."""

    session_id: str
    agent_kind: str | None
    source: str
    #: The oldest record observed for this session, as an instant in UTC. None
    #: where no record stated a time this package could read -- never the moment
    #: of collection, which would date every historical transcript to whenever it
    #: happened to be read.
    started_at: datetime | None
    model: str | None
    working_directory: str | None
    machine: str | None
    user: str | None
    turns: tuple[Turn, ...]
    #: The agent build that ran. Behaviour changes between versions, so this is
    #: what lets an observation be reproduced — or explained by an upgrade.
    #:
    #: Defaulted, with `entrypoint` below, so that adding a field a reader may
    #: not be able to fill is not a breaking change for anything constructing a
    #: `Session`. A consumer's fixtures should not have to be rewritten because
    #: this package learned to recover one more thing.
    agent_version: str | None = None
    #: How the agent was driven: an editor, a terminal, or a program. A
    #: programmatic entrypoint means no human was at the keyboard, which is a
    #: different posture, not a different amount of the same one.
    entrypoint: str | None = None
    #: The branch checked out where the session ran. Recorded on every record
    #: upstream; carried because *which repository, on which branch* is the
    #: first thing a person asks about a finding.
    git_branch: str | None = None
    #: The request that started the session, as the transcript records it
    #: separately from the turns. It survives compaction, which the first user
    #: turn does not, so it is the only durable baseline for judging whether
    #: later work was asked for.
    initial_prompt: str | None = None
    #: Material that reached the model outside the turn structure. See
    #: `ContextItem` — this is the injection surface a turns-and-results reader
    #: cannot see.
    context_items: tuple[ContextItem, ...] = ()
    #: Every point the conversation was compacted. Non-empty means earlier
    #: turns are gone and any conclusion about the original request is being
    #: drawn from a summary.
    compactions: tuple[Compaction, ...] = ()
    #: Every MCP server this session connected to, as the client recorded it.
    #: Empty where the logs could not be read -- `mcp_log_state` says which.
    mcp_connections: tuple[MCPConnection, ...] = ()
    #: Whether the MCP connection log was read for this session. Carried rather
    #: than logged for the same reason the collection carries its failures: a
    #: session whose transports are unknown and one whose servers were all
    #: local are not the same answer.
    mcp_log_state: MCPLogState = "not_attempted"
    #: How many MCP calls in this session had their status and duration
    #: withheld because the log cannot order them against the transcript --
    #: either two calls to the same tool were in flight at once, or the line
    #: announcing one of them was never readable, which leaves the client's
    #: completion order unusable as invocation order either way.
    #:
    #: Independent of `mcp_log_state`: a session missing this many calls'
    #: worth of outcome is still `applied` overall, since the log and the
    #: transcript otherwise agree on how many calls were made -- but that
    #: alone reads as full coverage. Nonzero here is what says some of it was
    #: nonetheless withheld, and why (ADR-0003's ordinal join has nothing to
    #: order two calls to the same tool it cannot separate).
    mcp_overlap_withheld: int = 0
    #: Times the provider's own safeguards declined and the CLI fell back.
    provider_refusals: tuple[ProviderRefusal, ...] = ()
    #: The newest record observed for this session, as an instant in UTC.
    #:
    #: **Not an end.** Sessions are read while they are still being written, so
    #: the newest record is the latest activity seen and says nothing about
    #: whether more is coming. Named for what it is.
    #:
    #: **Not the newest turn, either.** Boundary and tool-result records carry
    #: timestamps without producing turns, so this is routinely later than the
    #: last `Turn.occurred_at` -- which is what makes it the recency signal for
    #: a session, where the newest turn alone would understate it.
    last_activity_at: datetime | None = None

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def public_descriptor(self) -> dict[str, object]:
        """Identity, kind, start and turn count — and nothing else.

        This is the shape a consumer may carry outward. Everything omitted here
        is local-only by construction rather than by a consumer's discipline.
        """
        return {
            "session_id": self.session_id,
            "agent_kind": self.agent_kind,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "turn_count": self.turn_count,
        }
