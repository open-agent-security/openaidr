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


def test_bare_invocation_reports_not_implemented(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 1
    assert "not implemented" in capsys.readouterr().err


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
