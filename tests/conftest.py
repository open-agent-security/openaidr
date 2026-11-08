import pytest


@pytest.fixture(autouse=True)
def _no_real_devin_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Point Devin's default data root at an empty directory for every test.

    `default_readers()` reads Devin from its default root unless a caller passes
    `devin_root=`, so on a machine with Devin installed a test about Claude Code
    collects that machine's real sessions too. CI has no Devin install, which is
    why the leak only ever shows up locally.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path_factory.mktemp("xdg-data")))
    monkeypatch.delenv("OPENAIDR_DEVIN_ROOT", raising=False)


@pytest.fixture(autouse=True)
def _no_real_codex_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Point Codex's home at an empty directory for every test, for the same
    reason: `default_readers()` reads `CODEX_HOME`, else `~/.codex`."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path_factory.mktemp("codex-home")))
    monkeypatch.delenv("OPENAIDR_CODEX_ROOT", raising=False)


@pytest.fixture(autouse=True)
def _no_real_cursor_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Point Cursor's roots and its debug log directory at empty directories for
    every test: `default_readers()` reads `CURSOR_CONFIG_DIR`, else `~/.cursor`,
    the debug log under `TMPDIR`, and the desktop app's user data under
    `VSCODE_APPDATA`, else `~/.config/Cursor`."""
    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(tmp_path_factory.mktemp("cursor-config")))
    monkeypatch.setenv("CURSOR_DATA_DIR", str(tmp_path_factory.mktemp("cursor-data")))
    monkeypatch.setenv("TMPDIR", str(tmp_path_factory.mktemp("tmp")))
    monkeypatch.setenv("VSCODE_APPDATA", str(tmp_path_factory.mktemp("appdata")))
    monkeypatch.delenv("VSCODE_PORTABLE", raising=False)
    monkeypatch.delenv("OPENAIDR_CURSOR_ROOT", raising=False)
