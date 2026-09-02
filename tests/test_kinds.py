from openaidr.kinds import map_source, parse_kind_filter


def test_known_source_maps_to_an_agent_kind() -> None:
    assert map_source("claude") == "claude-code"


def test_unknown_source_is_unmapped_rather_than_guessed() -> None:
    """Unmapped is a coverage statement, not an error and not a guess."""
    assert map_source("some-new-agent") is None


def test_no_filter_includes_every_kind_including_the_unmapped() -> None:
    selection = parse_kind_filter(None)
    assert selection.includes("claude-code")
    assert selection.includes(None)


def test_explicit_filter_excludes_other_kinds_and_the_unmapped() -> None:
    selection = parse_kind_filter(["claude-code"])
    assert selection.includes("claude-code")
    assert not selection.includes("cursor")
    assert not selection.includes(None)
