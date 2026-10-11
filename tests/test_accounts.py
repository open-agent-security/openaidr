"""Who each agent kind is signed in as (ADR-0027)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from openaidr import accounts
from openaidr.accounts import SignInQuery, sign_in_query, sign_in_records, signed_in
from openaidr.model import LOCAL_ONLY, Account

_CLAUDE = {
    "accountUuid": "4b0c1d2e-7f3a-4c5d-9e8f-0a1b2c3d4e5f",
    "emailAddress": "jane.doe@example.com",
    "displayName": "Jane",
    "fullName": "Jane Doe",
    "billingType": "stripe_subscription",
    "organizationUuid": "9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a",
}


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture(autouse=True)
def _no_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


# --- Claude Code -------------------------------------------------------------


def test_claude_code_reads_its_account_from_the_file_beside_its_directory(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": _CLAUDE}), "utf-8")

    assert signed_in("claude-code", home=home) == Account(
        kind="claude-code",
        account_id="4b0c1d2e-7f3a-4c5d-9e8f-0a1b2c3d4e5f",
        display_name="Jane",
        email="jane.doe@example.com",
        plan="stripe_subscription",
    )


def test_a_moved_claude_directory_keeps_its_file_inside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With `CLAUDE_CONFIG_DIR` set, Claude Code writes nothing to the home
    directory; a stale `~/.claude.json` there is not the signed-in account."""
    home = _home(tmp_path)
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {**_CLAUDE, "accountUuid": "stale"}}), "utf-8"
    )
    (moved / ".claude.json").write_text(json.dumps({"oauthAccount": _CLAUDE}), "utf-8")

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(moved))
    by_env = signed_in("claude-code", home=home)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    by_argument = signed_in("claude-code", config_dir=moved, home=home)

    assert by_env is not None and by_env.account_id == _CLAUDE["accountUuid"]
    assert by_argument == by_env


def test_a_legacy_claude_config_file_wins(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / ".claude").mkdir()
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": _CLAUDE}), "utf-8")
    (home / ".claude" / ".config.json").write_text(
        json.dumps({"oauthAccount": {**_CLAUDE, "accountUuid": "legacy"}}), "utf-8"
    )

    account = signed_in("claude-code", home=home)

    assert account is not None and account.account_id == "legacy"


def test_sign_in_records_names_the_legacy_file_before_it_exists(tmp_path: Path) -> None:
    """A consumer following the watch contract needs the legacy path to watch
    even before Claude Code has written it, or it has no way to notice the
    legacy file appear and take over."""
    home = _home(tmp_path)
    (home / ".claude").mkdir()
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": _CLAUDE}), "utf-8")

    assert sign_in_records("claude-code", home=home) == (
        home / ".claude" / ".config.json",
        home / ".claude.json",
    )


@pytest.mark.parametrize(
    "document",
    [
        "not json",
        json.dumps([]),
        json.dumps({"oauthAccount": None}),
        json.dumps({"oauthAccount": {**_CLAUDE, "accountUuid": ""}}),
        json.dumps({"oauthAccount": {"displayName": "Jane"}}),
    ],
)
def test_claude_code_without_an_account_id_is_signed_in_as_no_one(
    tmp_path: Path, document: str
) -> None:
    home = _home(tmp_path)
    (home / ".claude.json").write_text(document, "utf-8")

    assert signed_in("claude-code", home=home) is None


def test_no_claude_file_is_no_account(tmp_path: Path) -> None:
    assert signed_in("claude-code", home=_home(tmp_path)) is None


def test_an_empty_name_is_no_name(tmp_path: Path) -> None:
    home = _home(tmp_path)
    account = {**_CLAUDE, "displayName": "  ", "emailAddress": ""}
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": account}), "utf-8")

    found = signed_in("claude-code", home=home)

    assert found is not None
    assert (found.display_name, found.email) == (None, None)


# --- every kind --------------------------------------------------------------


# --- devin -------------------------------------------------------------------

#: `devin auth status` as Devin 3000.11.3 prints it, the values invented.
_DEVIN_STATUS = """Logged in (via Devin).

Credentials:
  File:              {home}/.local/share/devin/credentials.toml
  API server:        https://server.codeium.com

User:
  Name:              Jane Doe
  Email:             jane.doe@example.com
  User ID:           user-0123456789abcdef0123456789abcdef

Account:
  Tier:              Devin Teams
  Plan:              Teams
  Team ID:           devin-team$account-0123456789abcdef0123456789abcdef
"""


def _fake_devin(
    home: Path, output: str, *, exit_code: int = 0, sleep: float = 0, data: Path | None = None
) -> Path:
    """A `devin` where the CLI installs itself, recording how it was called."""
    root = data if data is not None else home / ".local" / "share"
    binary = root / "devin" / "cli" / "_versions" / "current" / "bin" / "devin"
    binary.parent.mkdir(parents=True)
    printed = home / "printed.txt"
    printed.write_text(output.format(home=home), "utf-8")
    binary.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> "{home}/argv.txt"\n'
        f"sleep {sleep}\n"
        f'cat "{printed}"\n'
        f"exit {exit_code}\n",
        "utf-8",
    )
    binary.chmod(0o755)
    return binary


@pytest.fixture
def no_other_devin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the fake in the test's home may answer: not one on PATH, not the
    machine's own Devin.app."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(accounts, "_DEVIN_APP_BINARIES", ())


def _asked(query: SignInQuery) -> str:
    """What a consumer that chooses to ask does: run the query, bounded."""
    finished = subprocess.run(
        list(query.argv),
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, **query.environment},
        check=False,
    )
    return finished.stdout if finished.returncode == 0 else ""


@pytest.mark.usefixtures("no_other_devin")
def test_devin_is_never_run_by_signed_in(tmp_path: Path) -> None:
    """Sensing here is read-only (AGENTS.md). Devin's answer comes from its
    server, so `signed_in` reports what is on disk, which for Devin is no one,
    and the binary is never started."""
    home = _home(tmp_path)
    _fake_devin(home, _DEVIN_STATUS)

    assert signed_in("devin", home=home) is None
    assert not (home / "argv.txt").exists()


@pytest.mark.usefixtures("no_other_devin")
def test_devin_says_how_to_ask_and_reads_the_answer(tmp_path: Path) -> None:
    """Devin's own `auth status` asks its server with its own credential and
    prints the user. A consumer runs it; this package reads what it printed."""
    home = _home(tmp_path)
    binary = _fake_devin(home, _DEVIN_STATUS)

    query = sign_in_query("devin", home=home)

    assert query is not None
    assert query.argv == (str(binary), "auth", "status")
    assert query.environment["NO_COLOR"] == "1"
    assert query.read(_asked(query)) == Account(
        kind="devin",
        account_id="user-0123456789abcdef0123456789abcdef",
        display_name="Jane Doe",
        email="jane.doe@example.com",
        plan="Teams",
    )
    assert (home / "argv.txt").read_text("utf-8").split() == ["auth", "status"]


@pytest.mark.usefixtures("no_other_devin")
def test_devin_is_found_under_xdg_data_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path)
    data = tmp_path / "data"
    binary = _fake_devin(home, _DEVIN_STATUS, data=data)
    monkeypatch.setenv("XDG_DATA_HOME", str(data))

    query = sign_in_query("devin", home=home)

    assert query is not None and query.argv[0] == str(binary)


@pytest.mark.usefixtures("no_other_devin")
def test_asking_devin_never_needs_its_credentials_file(tmp_path: Path) -> None:
    """The credential is Devin's to use. Unreadable to everyone, the query and
    its answer are the same, because nothing here opens it."""
    home = _home(tmp_path)
    _fake_devin(home, _DEVIN_STATUS)
    credentials = home / ".local" / "share" / "devin" / "credentials.toml"
    credentials.write_text('windsurf_api_key = "sk-API-SECRET"\n', "utf-8")
    credentials.chmod(0)
    try:
        query = sign_in_query("devin", home=home)
        assert query is not None
        found = query.read(_asked(query))
    finally:
        credentials.chmod(0o600)

    assert found is not None
    assert "sk-API-SECRET" not in repr(found) + repr(query)


def test_devin_colours_are_not_part_of_the_answer() -> None:
    query = SignInQuery(kind="devin", argv=("devin", "auth", "status"))
    coloured = _DEVIN_STATUS.format(home="/h").replace("Jane Doe", "\x1b[1mJane Doe\x1b[0m")

    found = query.read(coloured)

    assert found is not None and found.display_name == "Jane Doe"


@pytest.mark.parametrize(
    "output",
    [
        "Not logged in. Run `devin auth login`.\n",
        _DEVIN_STATUS.replace("  User ID:           user-0123456789abcdef0123456789abcdef\n", ""),
        (
            "Logged in (via Devin).\n\nUser / team info:\n"
            "  Failed to fetch from server: Connection failed\n"
        ),
        "",
    ],
    ids=["signed-out", "no-user-id", "offline", "silent"],
)
def test_an_answer_naming_no_user_id_is_no_one(output: str) -> None:
    """Offline, Devin cannot say who: it asks its server. That is no account,
    never a guess, and a consumer keeps whatever it last knew."""
    query = SignInQuery(kind="devin", argv=("devin", "auth", "status"))

    assert query.read(output.format(home="/h")) is None


@pytest.mark.usefixtures("no_other_devin")
def test_no_devin_installed_cannot_be_asked(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / ".config" / "devin").mkdir(parents=True)
    (home / ".config" / "devin" / "config.json").write_text(
        json.dumps({"devin": {"org_id": "org-1"}}), "utf-8"
    )

    assert sign_in_query("devin", home=home) is None


@pytest.mark.parametrize("kind", ["claude-code", "cursor", "codex"])
def test_a_kind_with_nothing_to_ask_has_no_query(tmp_path: Path, kind: str) -> None:
    assert sign_in_query(kind, home=_home(tmp_path)) is None


# --- what to watch -----------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "relatives"),
    [
        ("claude-code", (".claude/.config.json", ".claude.json")),
        ("devin", (".local/share/devin/credentials.toml",)),
    ],
)
def test_each_kind_names_every_file_its_sign_in_depends_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, relatives: tuple[str, ...]
) -> None:
    """A consumer asks again when any of these changes. Devin's is a network
    call, so asking only then is the difference between once and on every
    pass. Named whether or not each exists yet."""
    for variable in ("CLAUDE_CONFIG_DIR", "XDG_DATA_HOME"):
        monkeypatch.delenv(variable, raising=False)
    home = _home(tmp_path)

    assert sign_in_records(kind, home=home) == tuple(home / r for r in relatives)


@pytest.mark.parametrize("kind", ["cursor", "codex"])
def test_a_kind_with_no_reader_names_no_file(tmp_path: Path, kind: str) -> None:
    """Codex's sign-in is read with its sessions, which this package does not
    read yet (ADR-0027 clause 1)."""
    assert sign_in_records(kind, home=_home(tmp_path)) == ()


@pytest.mark.parametrize("kind", ["cursor", "codex"])
def test_a_kind_with_no_reader_is_signed_in_as_no_one(tmp_path: Path, kind: str) -> None:
    assert signed_in(kind, home=_home(tmp_path)) is None


@pytest.mark.parametrize(
    ("kind", "relative", "document"),
    [
        ("claude-code", ".claude.json", {"oauthAccount": _CLAUDE}),
    ],
)
def test_one_call_is_one_read_of_the_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    relative: str,
    document: dict[str, object],
) -> None:
    """Fields used together come from one read: read apart, a sign-in between
    the reads would pair one account's id with another's name."""
    home = _home(tmp_path)
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), "utf-8")
    real = Path.read_text
    reads: list[Path] = []

    def counted(self: Path, *args: object, **kwargs: object) -> str:
        if self == path:
            reads.append(self)
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", counted)

    assert signed_in(kind, home=home) is not None
    assert len(reads) == 1


def test_a_persons_name_and_address_are_local_only() -> None:
    assert {"display_name", "email"} <= LOCAL_ONLY


@pytest.mark.parametrize("how", ["environment", "argument"])
def test_claude_config_dir_named_as_the_default_is_still_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """Claude Code puts `.claude.json` inside `CLAUDE_CONFIG_DIR` whenever it
    is set, even to `~/.claude`. Telling named from unnamed by comparing the
    path with the default read the home directory's file instead."""
    home = _home(tmp_path)
    directory = home / ".claude"
    directory.mkdir()
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {**_CLAUDE, "accountUuid": "the-home-file"}}), "utf-8"
    )
    (directory / ".claude.json").write_text(json.dumps({"oauthAccount": _CLAUDE}), "utf-8")
    if how == "environment":
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(directory))
        found = signed_in("claude-code", home=home)
        records = sign_in_records("claude-code", home=home)
    else:
        found = signed_in("claude-code", config_dir=directory, home=home)
        records = sign_in_records("claude-code", config_dir=directory, home=home)

    assert found is not None and found.account_id == _CLAUDE["accountUuid"]
    assert records == (directory / ".config.json", directory / ".claude.json")
