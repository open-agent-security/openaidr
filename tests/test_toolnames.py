import pytest

from openaidr.toolnames import split_tool_name


def test_claude_code_mcp_tool_splits_on_the_doubled_underscore() -> None:
    assert split_tool_name("claude-code", "mcp__github__get_issue") == ("github", "get_issue")


def test_claude_code_builtin_tool_has_no_server() -> None:
    assert split_tool_name("claude-code", "Read") == (None, "Read")


def test_server_name_containing_a_single_underscore_survives() -> None:
    """A general pattern would mis-split here and name the *wrong* server."""
    assert split_tool_name("claude-code", "mcp__my_server__do_thing") == ("my_server", "do_thing")


def test_tool_name_containing_the_delimiter_keeps_its_tail() -> None:
    assert split_tool_name("claude-code", "mcp__srv__a__b") == ("srv", "a__b")


def test_unknown_kind_never_guesses_a_server() -> None:
    """No global fallback pattern: a silent wrong answer is worse than a gap."""
    assert split_tool_name(None, "mcp__github__get_issue") == (None, "mcp__github__get_issue")


@pytest.mark.parametrize("raw", ["mcp__", "mcp__github", "mcp____x"])
def test_malformed_claude_names_are_left_whole(raw: str) -> None:
    assert split_tool_name("claude-code", raw) == (None, raw)
