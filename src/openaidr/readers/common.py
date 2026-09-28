"""Shared filesystem and timestamp mechanics; agent formats stay in their readers."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path


def timestamp(value: object) -> datetime | None:
    """Normalize recorded ISO times to UTC, treating a missing zone as UTC.

    ADR-0010 applies to every reader. Invalid values, including offsets that
    overflow datetime's range during conversion, leave the time absent.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
        return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def directory_present(root: Path) -> bool:
    """Only a genuinely absent root returns False; unreadable roots raise.

    Boolean Path probes suppress filesystem errors on some Python versions.
    stat/lstat distinguish a missing agent directory from a broken symlink.
    """
    try:
        mode = root.stat().st_mode
    except FileNotFoundError:
        try:
            root.lstat()
        except FileNotFoundError:
            return False
        raise FileNotFoundError("broken symlink") from None
    if not stat.S_ISDIR(mode):
        raise NotADirectoryError("not a directory")
    return True


def discover_files(
    root: Path, *, prefix: str = "", suffixes: tuple[str, ...] = (".jsonl",)
) -> tuple[list[Path], list[OSError]]:
    """Return matching paths and every failed subtree scan (ADR-0009).

    glob/rglob can silently suppress scan errors. Each reader must report them
    and decide which facts an incomplete discovery prevents it from asserting.
    """
    files: list[Path] = []
    failures: list[OSError] = []
    for directory, _dirs, names in os.walk(root, onerror=failures.append):
        files.extend(
            Path(directory) / name
            for name in names
            if name.startswith(prefix) and name.endswith(suffixes)
        )
    return sorted(files), failures
