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
from openaidr.readers.base import (
    IncrementalReader,
    Reader,
    ReaderFailure,
    Window,
    collect_from,
)
from openaidr.readers.claude_code import ClaudeCodeReader
from openaidr.readers.devin_cli import DevinCliReader


@dataclass(frozen=True)
class Collection:
    """What one pass produced, and what failed while producing it."""

    sessions: list[Session] = field(default_factory=list)
    failures: list[ReaderFailure] = field(default_factory=list)


def default_readers(root: Path | None = None, devin_root: Path | None = None) -> list[Reader]:
    """The readers this package ships: one per kind.

    `root` is Claude Code's, kept positional for existing callers; each other
    kind's root has its own keyword.
    """
    return [ClaudeCodeReader(root=root), DevinCliReader(root=devin_root)]


def default_incremental_readers(
    root: Path | None = None, devin_root: Path | None = None
) -> list[IncrementalReader]:
    """The stateful readers this package ships: one per kind."""
    return [ClaudeCodeReader(root=root), DevinCliReader(root=devin_root)]


class IncrementalCollector:
    """Keep per-reader cursors in memory and return one growing file as sessions."""

    def __init__(
        self,
        root: Path | None = None,
        readers: Sequence[IncrementalReader] | None = None,
        *,
        devin_root: Path | None = None,
    ) -> None:
        available = (
            readers if readers is not None else default_incremental_readers(root, devin_root)
        )
        self._readers = {reader.agent_kind: reader for reader in available}

    def locations(self) -> dict[str, tuple[Path, ...]]:
        """Per agent kind, the directories its session files live under.

        Everything a watcher needs to follow every kind this collector reads:
        a file that changed beneath a kind's directories goes to `collect`
        under that kind, which ignores a file the kind does not read.
        """
        return {kind: reader.locations() for kind, reader in self._readers.items()}

    def collect(self, agent_kind: str, path: Path) -> Collection:
        reader = self._readers.get(agent_kind)
        if reader is None:
            return Collection(
                failures=[
                    ReaderFailure(
                        agent_kind=agent_kind,
                        message=f"no incremental reader for agent kind {agent_kind!r}",
                    )
                ]
            )
        try:
            sessions, failures = reader.collect_file(path)
        except Exception as error:  # noqa: BLE001 - isolation is the contract
            return Collection(failures=[ReaderFailure(agent_kind=agent_kind, message=str(error))])
        return _collection(sessions, failures)


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
    return _collection(collected, failures)


def _collection(collected: Sequence[Session], failures: Sequence[ReaderFailure]) -> Collection:
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
