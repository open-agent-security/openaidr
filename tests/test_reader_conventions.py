"""Invariants over the readers' source, not over one call site's behaviour.

Three review rounds found the same two shapes at different sites: a filesystem
probe that cannot distinguish "does not exist" from "could not be read", and an
I/O failure caught and dropped. Each was fixed where it was found, and the next
round found the next site — an enumeration, not a bug. These assert the property
over the whole module, the way `test_every_mcp_log_state_is_explained` asserts
the explanation table covers the model rather than covering one state (ADR-0009).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_READERS = sorted((Path(__file__).parent.parent / "src" / "openaidr" / "readers").glob("*.py"))

#: Every one of these answers a filesystem question with a bool and no way to
#: say "I could not tell". `Path.is_dir()`/`is_file()`/`exists()` propagate some
#: `OSError`s and swallow others before Python 3.14, and from 3.14 swallow every
#: one and report `False` -- indistinguishable from absence. `glob()`/`rglob()`
#: suppress every `OSError` raised while scanning as of 3.13, so a directory
#: this process cannot list vanishes without even yielding a name. `stat()` and
#: `os.walk(onerror=...)` are the primitives that still raise or name what
#: failed, which is what lets a failure be reported and its consequences stated.
_BLIND_PROBES = ("is_dir", "is_file", "exists", "glob", "rglob")

#: `contextlib.suppress` is the same defect as a bare `except: pass`, spelled so
#: that a handler-body check would not see it.
_SUPPRESSORS = ("suppress",)


def _reader_sources() -> list[tuple[str, ast.Module]]:
    return [(path.name, ast.parse(path.read_text(encoding="utf-8"))) for path in _READERS]


@pytest.mark.parametrize(
    "name,tree", _reader_sources(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_no_reader_asks_the_filesystem_a_question_it_cannot_answer(
    name: str, tree: ast.Module
) -> None:
    """A probe that reports `False` for a directory it could not stat turns a
    permission failure into an absence -- and an absence, in this reader, is a
    claimant silently leaving the set that establishes identity."""
    found = [
        f"{name}:{node.lineno} {node.func.attr}()"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _BLIND_PROBES
    ]
    assert not found, (
        "use stat()/S_ISDIR or os.walk(onerror=...) so the failure can be "
        f"reported and its consequences stated: {found}"
    )


@pytest.mark.parametrize(
    "name,tree", _reader_sources(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_no_reader_catches_a_failure_and_says_nothing_about_it(name: str, tree: ast.Module) -> None:
    """Every I/O failure here has two obligations: report it, and say what it
    makes unknowable. A handler whose whole body is `pass` discharges neither,
    and the session it degrades is shaped exactly like one where nothing went
    wrong."""
    silent = [
        f"{name}:{handler.lineno}"
        for handler in ast.walk(tree)
        if isinstance(handler, ast.ExceptHandler)
        and all(isinstance(statement, ast.Pass) for statement in handler.body)
    ]
    suppressed = [
        f"{name}:{call.lineno}"
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr in _SUPPRESSORS
    ]
    assert not silent + suppressed, (
        "report the failure or state what it withholds; a dropped read is the "
        f"defect this module keeps rediscovering: {silent + suppressed}"
    )
