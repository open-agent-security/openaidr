"""The console script declared in pyproject must actually import and run.

Declaring `[project.scripts]` without the module it names installs a launcher
that fails at runtime, and nothing else in the gate set catches it.
"""

import subprocess
import sys

import pytest

from openaidr import __version__
from openaidr.__main__ import main


def test_version_flag_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_installed_console_script_runs() -> None:
    """Exercises the real launcher, not just the importable function."""
    result = subprocess.run(
        [sys.executable, "-m", "openaidr", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_bare_invocation_prints_help_and_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main([]) == 1
    assert "usage" in capsys.readouterr().out.lower()


def test_sessions_subcommand_prints_collected_sessions(
    tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.fixtures.claude_jsonl import user_text, write_session

    write_session(
        tmp_path,
        "-p",
        [user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "look at the repository")],
    )
    monkeypatch.setenv("OPENAIDR_CLAUDE_ROOT", str(tmp_path))
    assert main(["sessions", "--since", "3650d", "--detail"]) == 0
    assert "claude-code:s1" in capsys.readouterr().out


def test_an_unrecognised_agent_kind_collects_nothing_without_crashing(
    tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--agent-kind` filters on any string rather than validating it against
    the known vocabulary, so a typo collects zero sessions rather than failing
    loudly. This test records that as a deliberate baseline, not an oversight —
    narrowing it is future work."""
    from tests.fixtures.claude_jsonl import user_text, write_session

    write_session(
        tmp_path,
        "-p",
        [user_text("s1", "u1", "2026-08-01T10:00:00.000Z", "look at the repository")],
    )
    monkeypatch.setenv("OPENAIDR_CLAUDE_ROOT", str(tmp_path))
    assert main(["sessions", "--since", "3650d", "--agent-kind", "not-a-real-kind"]) == 0
    assert "claude-code:s1" not in capsys.readouterr().out
