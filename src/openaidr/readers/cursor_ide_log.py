"""Reads the Cursor desktop app's structured log: usage and client errors.

The desktop app writes no token usage to its state database. Its structured
log, one file for each window and launch, writes the line the CLI's debug log
writes for each model response (ADR-0039), under
`<user data>/logs/<launch>/window<N>/exthost/<extension>/Cursor Structured Logs.log`.
Cursor keeps the current launch's folder and the nine before it.

A line is `<local date> <local time>.<ms> [<level>] <JSON>`. The time has no
zone; it is the machine's own. Only these are read:

- `agent.turn.start` and `agent.turn.outcome`: `conversation_id`, `request_id`.
  A turn is open from its start to its outcome. An outcome of `error` is a
  client error, with its `error_text`.
- `Setting token details for client token ring`: one model response, with
  counts as strings. It names no conversation, and one window runs several at
  once, so it is placed only on the one turn open in its file.
- `subagent proxy: delegating to renderer` and `subagent proxy: renderer
  settled`, with the starting call's `toolId`: a sub-agent's span. A usage line
  inside one such span, with one turn open, is that sub-agent's.

A line that cannot be placed without a guess is dropped (ADR-0039 clause 3).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from openaidr.readers.cursor_log import Outcome, Placement, Usage

LOG_NAME = "Cursor Structured Logs.log"

_LINE = re.compile(
    r"^(?P<at>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \[(?P<level>[a-z]+)\] (?P<body>\{.*\})\s*$"
)
_TURN_START = "agent.turn.start"
_TURN_OUTCOME = "agent.turn.outcome"
_USAGE = "Setting token details for client token ring"
_DELEGATE = "subagent proxy: delegating to renderer"
_SETTLED = "subagent proxy: renderer settled"
#: Depth of the log file below the logs root: launch, window, exthost, extension.
_DEPTH = 4


@dataclass
class _File:
    """One structured log's committed prefix and what it has placed so far."""

    identity: tuple[int, int] | None = None
    offset: int = 0
    #: Conversation to the request of its open turn.
    open_turns: dict[str, str | None] = field(default_factory=dict)
    #: Sub-agent call to the request of the turn that started it.
    open_children: dict[str, str | None] = field(default_factory=dict)
    conversations: list[str] = field(default_factory=list)
    requests: dict[str, list[str]] = field(default_factory=dict)
    usage: dict[str, list[Usage]] = field(default_factory=dict)
    subagent_usage: dict[str, list[Usage]] = field(default_factory=dict)
    errors: dict[str, list[Outcome]] = field(default_factory=dict)
    counts: dict[tuple[str, str | None], int] = field(default_factory=dict)
    first_at: datetime | None = None

    def read(self, path: Path) -> None:
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            identity = (info.st_dev, info.st_ino)
            if self.identity != identity or info.st_size < self.offset:
                fresh = _File(identity=identity)
                self.__dict__.update(fresh.__dict__)
            stream.seek(self.offset)
            for raw in stream:
                if not raw.endswith(b"\n"):
                    break
                self.offset += len(raw)
                self._line(raw.decode("utf-8", errors="replace"))

    def _line(self, text: str) -> None:
        match = _LINE.match(text)
        if match is None:
            return
        try:
            body = json.loads(match.group("body"))
        except (ValueError, RecursionError):
            return
        if not isinstance(body, dict):
            return
        metadata = body.get("metadata")
        if not isinstance(metadata, dict):
            return
        at = _instant(match.group("at"))
        if self.first_at is None:
            self.first_at = at
        message = body.get("message")
        if message == _TURN_START:
            self._turn_start(metadata)
        elif message == _TURN_OUTCOME:
            self._outcome(metadata, at)
        elif message == _USAGE:
            self._usage(metadata, at)
        elif message == _DELEGATE:
            call = _string(metadata.get("toolId"))
            if call is not None:
                parent = next(iter(self.open_turns.values())) if len(self.open_turns) == 1 else None
                self.open_children[call] = parent
        elif message == _SETTLED:
            call = _string(metadata.get("toolId"))
            if call is not None:
                self.open_children.pop(call, None)

    def _turn_start(self, metadata: dict[str, object]) -> None:
        conversation = _string(metadata.get("conversation_id"))
        if conversation is None:
            return
        request = _string(metadata.get("request_id"))
        self.open_turns[conversation] = request
        if conversation not in self.conversations:
            self.conversations.append(conversation)
        if request is not None:
            self.requests.setdefault(conversation, []).append(request)

    def _outcome(self, metadata: dict[str, object], at: datetime | None) -> None:
        conversation = _string(metadata.get("conversation_id"))
        if conversation is None:
            return
        self.open_turns.pop(conversation, None)
        if metadata.get("outcome") != "error":
            return
        if conversation not in self.conversations:
            self.conversations.append(conversation)
        self.errors.setdefault(conversation, []).append(
            Outcome(
                request_id=_string(metadata.get("request_id")),
                occurred_at=at,
                text=_string(metadata.get("error_text")),
            )
        )

    def _usage(self, metadata: dict[str, object], at: datetime | None) -> None:
        # Placeable only with one turn open, and at most one sub-agent span
        # inside it: anything more and the line could be another's.
        if len(self.open_turns) != 1 or len(self.open_children) > 1:
            return
        conversation, request = next(iter(self.open_turns.items()))
        child: str | None = None
        if self.open_children:
            child, request = next(iter(self.open_children.items()))
        owner = child if child is not None else conversation
        key = (owner, request)
        self.counts[key] = self.counts.get(key, 0) + 1
        maximum = _count(metadata.get("maxTokens"))
        line = Usage(
            request_id=request,
            ordinal=self.counts[key],
            model=_string(metadata.get("modelName")),
            occurred_at=at,
            input_tokens=_count(metadata.get("inputTokens")),
            output_tokens=_count(metadata.get("outputTokens")),
            cache_read_tokens=_count(metadata.get("cacheReadTokens")),
            cache_write_tokens=_count(metadata.get("cacheWriteTokens")),
            max_tokens=maximum if maximum else None,
            subagent_call=child,
        )
        target = self.subagent_usage if child is not None else self.usage
        target.setdefault(owner, []).append(line)


class CursorIdeLogs:
    """Every structured log under one logs root, each read from where it was
    last read."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._files: dict[Path, _File] = {}

    def refresh(self) -> list[str]:
        """Read what every log appended; a failure message for each directory
        or log that could not be read. No logs root is no logs, not a failure."""
        failures: list[str] = []
        errors: list[OSError] = []
        present: set[Path] = set()
        for dirpath, dirnames, filenames in os.walk(self.root, onerror=errors.append):
            depth = len(Path(dirpath).relative_to(self.root).parts)
            if depth >= _DEPTH:
                dirnames[:] = []
                if LOG_NAME in filenames:
                    path = Path(dirpath) / LOG_NAME
                    present.add(path)
                    state = self._files.setdefault(path, _File())
                    try:
                        state.read(path)
                    except FileNotFoundError:
                        present.discard(path)
                    except OSError as error:
                        failures.append(f"{path}: {error}; its usage is not read")
        for error in errors:
            if isinstance(error, FileNotFoundError) and Path(str(error.filename)) == self.root:
                continue
            failures.append(f"{error.filename}: {error.strerror or error}")
        for gone in set(self._files) - present:
            del self._files[gone]
        return failures

    def place(self, conversation: str, subagent_call: str | None) -> Placement:
        """The usage and client errors for one desktop session: a main
        conversation, or the sub-agent `subagent_call` started. The desktop
        app's MCP logs are not read, so its state is `not_attempted`."""
        usage: list[Usage] = []
        requests: list[str] = []
        errors: list[Outcome] = []
        ordered = sorted(
            self._files.values(),
            key=lambda f: f.first_at or datetime.min.replace(tzinfo=UTC),
        )
        for state in ordered:
            if subagent_call is not None:
                lines = state.subagent_usage.get(subagent_call, [])
                usage.extend(lines)
                for line in lines:
                    if line.request_id is not None and line.request_id not in requests:
                        requests.append(line.request_id)
                continue
            usage.extend(state.usage.get(conversation, ()))
            requests.extend(state.requests.get(conversation, ()))
            errors.extend(state.errors.get(conversation, ()))
        times = [u.occurred_at for u in usage if u.occurred_at] + [
            e.occurred_at for e in errors if e.occurred_at
        ]
        return Placement(
            usage=tuple(usage),
            requests=tuple(requests),
            errors=tuple(errors),
            state="not_attempted",
            last_at=max(times) if times else None,
        )


def _instant(value: str) -> datetime | None:
    """A local time with no zone, as the machine that wrote it keeps it."""
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f").astimezone(UTC)
    except ValueError:
        return None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _count(value: object) -> int | None:
    """A count the structured log writes as a string, or an integer."""
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
