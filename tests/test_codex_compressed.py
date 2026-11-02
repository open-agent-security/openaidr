"""Compressed Codex rollouts (`docs/specs/codex.md#compressed-rollouts`)."""

from __future__ import annotations

import os
import sys
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openaidr.model import Session
from openaidr.readers import codex
from openaidr.readers.base import Window
from openaidr.readers.codex import CodexReader
from tests.fixtures.codex_rollout import Rollout, zstd

_ALL = Window(since=None)


def _read(home: Path, window: Window = _ALL) -> tuple[list[Session], list[str]]:
    sessions, failures = CodexReader(home).collect(window)
    return sorted(sessions, key=lambda s: s.session_id), [f.message for f in failures]


def _rollout(thread: str) -> Rollout:
    rollout = Rollout(thread)
    rollout.turn("t1")
    rollout.user("list the files")
    rollout.exec("call-1", "ls")
    rollout.usage("resp-1")
    rollout.assistant("done")
    rollout.complete()
    return rollout


def _age(path: Path, moment: datetime) -> None:
    os.utime(path, (moment.timestamp(), moment.timestamp()))


def test_a_compressed_rollout_reads_as_the_same_session_as_its_plain_form(
    tmp_path: Path,
) -> None:
    """Codex compresses a cold rollout in place, so a thread keeps its session
    and span identity across the change."""
    rollout = _rollout("t")
    rollout.write(tmp_path / "plain")
    rollout.write(tmp_path / "compressed", compressed=True)

    plain, plain_failures = _read(tmp_path / "plain")
    compressed, compressed_failures = _read(tmp_path / "compressed")

    assert plain_failures == compressed_failures == []
    assert compressed == plain
    assert [s.session_id for s in compressed] == ["codex:t"]
    assert compressed[0].turns[1].tool_calls[0].span == "codex:t:o5:0"


def test_a_rollout_in_several_zstd_frames_reads_as_one(tmp_path: Path) -> None:
    rollout = _rollout("t")
    rollout.write(tmp_path / "plain")
    lines = rollout.text().encode().splitlines(keepends=True)
    path = rollout.write(tmp_path / "framed", compressed=True)
    path.write_bytes(zstd(b"".join(lines[:3])) + zstd(b"".join(lines[3:])))

    assert _read(tmp_path / "framed") == _read(tmp_path / "plain")


def test_compressed_and_plain_rollouts_are_read_side_by_side(tmp_path: Path) -> None:
    _rollout("plain").write(tmp_path)
    _rollout("compressed").write(tmp_path, compressed=True)
    _rollout("archived").write(tmp_path, archived=True, compressed=True)

    sessions, failures = _read(tmp_path)

    assert failures == []
    assert [s.session_id for s in sessions] == ["codex:archived", "codex:compressed", "codex:plain"]


def test_a_rollout_in_both_forms_is_read_once_from_its_plain_form(tmp_path: Path) -> None:
    """Codex writes the new form before it removes the old one, in both
    directions, and reads the plain form first."""
    rollout = _rollout("t")
    rollout.write(tmp_path, compressed=True)
    rollout.user("appended after the thread was resumed")
    rollout.write(tmp_path)

    sessions, failures = _read(tmp_path)

    assert failures == []
    assert len(sessions) == 1
    assert sessions[0].turns[-1].text == "appended after the thread was resumed"


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        (lambda frame: frame[: len(frame) // 2], "ends inside a zstd frame"),
        (lambda frame: frame[:-1], "ends inside a zstd frame"),
        (lambda frame: b"not zstd at all\n", "does not decode as zstd"),
        (lambda frame: frame + b"trailing bytes", "does not decode as zstd"),
    ],
    ids=["truncated", "last-byte-missing", "not-zstd", "trailing-bytes"],
)
def test_a_damaged_compressed_rollout_is_one_failure_and_the_rest_are_read(
    tmp_path: Path, damage: Callable[[bytes], bytes], reason: str
) -> None:
    _rollout("good").write(tmp_path, compressed=True)
    _rollout("plain").write(tmp_path)
    bad = _rollout("bad").write(tmp_path, compressed=True)
    bad.write_bytes(damage(bad.read_bytes()))

    sessions, failures = _read(tmp_path)

    assert [s.session_id for s in sessions] == ["codex:good", "codex:plain"]
    assert failures == [f"{bad}: compressed rollout {reason}; session withheld"]


def test_a_compressed_rollout_last_written_before_the_window_is_not_opened(
    tmp_path: Path,
) -> None:
    """Codex gives the compressed file the plain file's mtime, so the plain
    rule holds: a file last written before the window is skipped unread. An
    undecodable file proves it was never opened."""
    since = datetime(2026, 10, 20, tzinfo=UTC)
    old = _rollout("old").write(tmp_path, compressed=True)
    old.write_bytes(b"never decoded")
    _age(old, datetime(2026, 10, 16, 3, tzinfo=UTC))

    assert _read(tmp_path, Window(since=since)) == ([], [])


def test_a_compressed_rollout_written_inside_the_window_is_read(tmp_path: Path) -> None:
    """The thread started before the window and was last written inside it."""
    since = datetime(2026, 10, 16, 2, 0, 5, tzinfo=UTC)
    path = _rollout("t").write(tmp_path, compressed=True)
    _age(path, datetime(2026, 10, 16, 2, 0, 9, tzinfo=UTC))

    sessions, failures = _read(tmp_path, Window(since=since))

    assert failures == []
    assert [s.session_id for s in sessions] == ["codex:t"]


def test_collect_file_reads_a_compressed_rollout(tmp_path: Path) -> None:
    rollout = _rollout("t")
    rollout.write(tmp_path / "plain")
    path = rollout.write(tmp_path / "compressed", compressed=True)
    reader = CodexReader(tmp_path / "compressed")

    first = reader.collect_file(path)
    again = reader.collect_file(path)

    assert first == again == CodexReader(tmp_path / "plain").collect(_ALL)


def test_collect_file_leaves_a_compressed_rollout_to_its_plain_form(tmp_path: Path) -> None:
    rollout = _rollout("t")
    compressed = rollout.write(tmp_path, compressed=True)
    rollout.write(tmp_path)

    assert CodexReader(tmp_path).collect_file(compressed) == ([], [])


def test_the_decoder_is_the_standard_librarys_from_python_3_14() -> None:
    """`compression.zstd` arrived in 3.14; before it, `zstandard` decodes, and
    the dependency's marker installs it exactly there."""
    module = type(codex._frame_decoder()).__module__
    if sys.version_info >= (3, 14):
        assert module.startswith("compression.zstd")
    else:
        assert module.startswith("zstandard")
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    [requirement] = [d for d in project["project"]["dependencies"] if d.startswith("zstandard")]
    assert requirement.endswith("; python_full_version < '3.14'")
