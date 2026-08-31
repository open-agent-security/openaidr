"""One contract, per agent kind: given a kind and a window, return sessions.

Which implementation backs a kind — one of ours, or a third-party parser — is a
per-kind choice made on measured fidelity, and it reaches no consumer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from openaidr.model import Session


@dataclass(frozen=True)
class Window:
    """How far back to read. `None` means everything the reader can see.

    `since`, when set, must be timezone-aware — every timestamp this package
    parses from a session record is, and comparing a naive value against them
    raises `TypeError` rather than silently misbehaving. `render.parse_since` is
    the one place a `since` is constructed, and it always returns an aware UTC
    value or `None`.
    """

    since: datetime | None


@dataclass(frozen=True)
class ReaderFailure:
    """One kind failed to collect. Reported, never raised past this boundary."""

    agent_kind: str
    message: str


class Reader(Protocol):
    """A source of sessions for exactly one agent kind."""

    agent_kind: str

    def collect(self, window: Window) -> tuple[list[Session], list[ReaderFailure]]: ...


def collect_from(
    readers: Sequence[Reader], window: Window
) -> tuple[list[Session], list[ReaderFailure]]:
    """Collect from every reader, isolating failures to the kind that produced them.

    A reader's own returned failures are carried through unchanged — it has
    already isolated them to one file or one record. Only an exception escaping
    `collect` entirely is caught here, for the failure mode no reader
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
