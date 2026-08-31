"""The session model: three nested levels, and the identity consumers address rows by.

Naming and representation are this module's choice; what the spec fixes is the
information captured and the stability of span identity.

What is *absent* is deliberate. Per-call timings are not modelled because the
parsing dependency's event schema carries no timestamp below the session, and a
field that is always empty is a worse answer than no field: it invites consumers
to build on a value that never arrives. See `docs/specs/session-collection.md`.
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


@dataclass(frozen=True)
class Session:
    """One agent conversation."""

    session_id: str
    agent_kind: str | None
    source: str
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
    #: Times the provider's own safeguards declined and the CLI fell back.
    provider_refusals: tuple[ProviderRefusal, ...] = ()

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
