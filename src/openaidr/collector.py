"""Collection: run the readers for the selected kinds and hold the result in memory.

In process, never through a file. A serialize-and-reparse round trip would add
latency to the path that most needs speed, and would put transcript content on
disk that nothing else here creates.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from openaidr.kinds import KindSelection
from openaidr.model import Session
from openaidr.readers.base import Reader, ReaderFailure, Window, collect_from
from openaidr.readers.claude_code import ClaudeCodeReader


@dataclass(frozen=True)
class Collection:
    """What one pass produced, and what failed while producing it."""

    sessions: list[Session] = field(default_factory=list)
    failures: list[ReaderFailure] = field(default_factory=list)


def default_readers(root: Path | None = None) -> list[Reader]:
    """The readers this package ships. One kind today."""
    return [ClaudeCodeReader(root=root)]


def collect(
    selection: KindSelection,
    window: Window,
    readers: Sequence[Reader] | None = None,
) -> Collection:
    """One cold pass over the selected kinds, held by session identity, newest first.

    A session map keyed by identity, not a bare list. A second result naming
    an identity already seen is a collision — the readers' own session-id
    contracts are assumed unique, so this means that assumption broke (a
    copy, a restore, two files resolving to the same namespaced id) — and is
    reported as a failure rather than silently replacing the session already
    held under that identity.
    """
    chosen = [
        r
        for r in (readers if readers is not None else default_readers())
        if selection.includes(r.agent_kind)
    ]
    collected, failures = collect_from(chosen, window)
    by_identity: dict[str, Session] = {}
    collisions: list[ReaderFailure] = []
    for session in collected:
        if session.session_id in by_identity:
            collisions.append(
                ReaderFailure(
                    agent_kind=session.agent_kind or "unknown",
                    message=f"duplicate session identity {session.session_id!r}; kept the first one seen",
                )
            )
            continue
        by_identity[session.session_id] = session
    sessions = sorted(by_identity.values(), key=_sort_key, reverse=True)
    return Collection(sessions=sessions, failures=[*failures, *collisions])


def _sort_key(session: Session) -> datetime:
    return session.last_activity_at or session.started_at or datetime.min.replace(tzinfo=UTC)
