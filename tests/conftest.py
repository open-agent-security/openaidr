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
