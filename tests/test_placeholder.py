"""Placeholder so the pytest gate has something to run before collection lands."""

import openaidr


def test_version_is_exposed() -> None:
    assert openaidr.__version__
