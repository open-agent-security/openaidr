"""Compressed Codex rollouts (`docs/specs/codex.md#compressed-rollouts`)."""

from __future__ import annotations

import ast
import os
import sys
import tomllib
import tracemalloc
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


@pytest.mark.parametrize("between", [b"", b"\x50\x2a\x4d\x18\x03\x00\x00\x00abc"])
def test_a_rollout_in_several_zstd_frames_reads_as_one(tmp_path: Path, between: bytes) -> None:
    """Frames follow one another, and a skippable frame between them is
    skipped (RFC 8878 section 3.1.2)."""
    rollout = _rollout("t")
    rollout.write(tmp_path / "plain")
    lines = rollout.text().encode().splitlines(keepends=True)
    path = rollout.write(tmp_path / "framed", compressed=True)
    path.write_bytes(zstd(b"".join(lines[:3])) + between + zstd(b"".join(lines[3:])))

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


def _without(monkeypatch: pytest.MonkeyPatch, *modules: str) -> None:
    """Make each module fail to import, as on a Python built without it."""
    for module in modules:
        monkeypatch.setitem(sys.modules, module, None)


def test_the_standard_librarys_decoder_is_used_where_python_has_one() -> None:
    zstd_module = pytest.importorskip("compression.zstd")

    assert codex._zstd_decoder() == (zstd_module.ZstdFile, zstd_module.ZstdError)


def test_zstandard_decodes_where_the_standard_library_has_no_zstd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zstandard = pytest.importorskip("zstandard")
    rollout = _rollout("t")
    rollout.write(tmp_path / "plain")
    rollout.write(tmp_path / "compressed", compressed=True)
    _without(monkeypatch, "compression", "compression.zstd")

    decoder = codex._zstd_decoder()

    assert decoder is not None and decoder[1] is zstandard.ZstdError
    assert _read(tmp_path / "compressed") == _read(tmp_path / "plain")


def test_with_no_zstd_decoder_each_compressed_rollout_is_one_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Python 3.14 build can omit `compression.zstd`, and the dependency's
    marker leaves `zstandard` out there."""
    _rollout("plain").write(tmp_path)
    compressed = _rollout("compressed").write(tmp_path, compressed=True)
    _without(monkeypatch, "compression", "compression.zstd", "zstandard")

    sessions, failures = _read(tmp_path)

    assert [s.session_id for s in sessions] == ["codex:plain"]
    assert failures == [f"{compressed}: no zstd decoder is available; session withheld"]


def test_no_zstd_decoder_is_imported_with_the_reader() -> None:
    """A missing decoder fails one compressed file, never the import of the
    reader that every kind and command loads."""
    tree = ast.parse(Path(codex.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert not imported & {"compression", "zstandard"}


def test_the_dependency_marker_installs_zstandard_where_python_may_lack_zstd() -> None:
    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    [requirement] = [d for d in project["project"]["dependencies"] if d.startswith("zstandard")]
    assert requirement.endswith("; python_full_version < '3.14'")


def test_a_frame_that_expands_far_is_decoded_a_bounded_piece_at_a_time(tmp_path: Path) -> None:
    """Whitespace lines between the records: 32 MiB that compress to a few
    KiB. A decoder that expands a whole input chunk at once holds all of it."""
    rollout = _rollout("t")
    padding = " " * (64 * 1024 - 1) + "\n"
    lines = rollout.text().splitlines(keepends=True)
    text = lines[0] + padding * 512 + "".join(lines[1:])
    plain = rollout.write(tmp_path / "plain")
    plain.write_text(text, encoding="utf-8")
    compressed = rollout.write(tmp_path / "compressed", compressed=True)
    compressed.write_bytes(zstd(text.encode()))
    assert compressed.stat().st_size < 64 * 1024

    tracemalloc.start()
    try:
        sessions = _read(tmp_path / "compressed")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert sessions == _read(tmp_path / "plain")
    assert peak < 4 * 1024 * 1024
