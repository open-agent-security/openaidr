"""Default collection in tests must never discover the developer's Codex history."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_codex_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAIDR_CODEX_ROOT", str(tmp_path / "no-codex-sessions"))
