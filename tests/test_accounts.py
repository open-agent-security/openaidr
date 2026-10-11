"""Who each agent kind is signed in as (ADR-0027)."""

from __future__ import annotations

import base64
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

_SECRETS = ("ACCESS-SECRET", "REFRESH-SECRET", "sk-API-SECRET")


_AUTH = "https://api.openai.com/auth"
_PROFILE = "https://api.openai.com/profile"


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def _jwt(claims: dict[str, object]) -> str:
    def part(value: object) -> str:
        raw = json.dumps(value).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature-not-checked"


def _codex_auth(claims: dict[str, object] | None, *, mode: str = "chatgpt") -> dict[str, object]:
    tokens = {"access_token": _SECRETS[0], "refresh_token": _SECRETS[1], "account_id": "ws-1"}
    if claims is not None:
        tokens["id_token"] = _jwt(claims)
    return {"auth_mode": mode, "OPENAI_API_KEY": _SECRETS[2], "tokens": tokens}


@pytest.fixture(autouse=True)
def _no_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)


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


# --- Codex -------------------------------------------------------------------


_CODEX_CLAIMS: dict[str, object] = {
    "sub": "auth0|abc",
    "email": "jane.doe@example.com",
    "name": "Jane Doe",
    "https://api.openai.com/auth": {
        "chatgpt_user_id": "user-123",
        "chatgpt_account_id": "ws-1",
        "chatgpt_plan_type": "plus",
    },
}


def test_codex_signed_in_with_chatgpt_names_the_person_from_its_identity_token(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(json.dumps(_codex_auth(_CODEX_CLAIMS)), "utf-8")

    assert signed_in("codex", home=home) == Account(
        kind="codex",
        account_id="user-123",
        display_name="Jane Doe",
        email="jane.doe@example.com",
        plan="plus",
    )


def test_codex_reads_codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path)
    moved = tmp_path / "codex-home"
    moved.mkdir()
    (moved / "auth.json").write_text(json.dumps(_codex_auth(_CODEX_CLAIMS)), "utf-8")
    monkeypatch.setenv("CODEX_HOME", str(moved))

    found = signed_in("codex", home=home)

    assert found is not None and found.account_id == "user-123"


@pytest.mark.parametrize(
    "auth",
    [
        # Signed in with an API key: there is a key and no person.
        {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-API-SECRET", "tokens": None},
        _codex_auth(None),
        {"tokens": {"id_token": "not-a-jwt"}},
        {"tokens": {"id_token": "a.!!!.c"}},
        {"tokens": {"id_token": _jwt({"email": "jane.doe@example.com"})}},
    ],
)
def test_codex_without_an_identity_is_signed_in_as_no_one(
    tmp_path: Path, auth: dict[str, object]
) -> None:
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(json.dumps(auth), "utf-8")

    assert signed_in("codex", home=home) is None


def test_a_character_outside_the_base64_alphabet_is_a_malformed_token(
    tmp_path: Path,
) -> None:
    """Codex's own decoder (Rust's `URL_SAFE_NO_PAD`) refuses a payload with a
    character outside the base64 alphabet. Python's lenient decoder would
    silently drop a handful of them and decode the token anyway, naming an
    account from a credential Codex itself cannot load."""
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    header, payload, signature = _jwt(_CODEX_CLAIMS).split(".")
    tampered = f"{header}.{payload}!!!.{signature}"
    (home / ".codex" / "auth.json").write_text(
        json.dumps(_codex_auth(None) | {"tokens": {"id_token": tampered}}), "utf-8"
    )

    assert signed_in("codex", home=home) is None


def test_a_noncanonical_final_symbol_is_a_malformed_token(tmp_path: Path) -> None:
    """Codex's decoder (`URL_SAFE_NO_PAD`'s default config) rejects a final
    base64 symbol whose unused bits are not zero. A lenient decoder discards
    those bits and decodes anyway, so two different tokens would read as the
    same account — one of them a credential Codex itself cannot load."""
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    header, payload, signature = _jwt(_CODEX_CLAIMS).split(".")
    last = payload[-1]
    tampered_last = "B" if last != "B" else "C"
    tampered = f"{header}.{payload[:-1]}{tampered_last}.{signature}"
    (home / ".codex" / "auth.json").write_text(
        json.dumps(_codex_auth(None) | {"tokens": {"id_token": tampered}}), "utf-8"
    )

    assert signed_in("codex", home=home) is None


def test_a_token_payload_with_embedded_padding_is_malformed(tmp_path: Path) -> None:
    """Codex's `URL_SAFE_NO_PAD` never has padding; a payload that already
    carries `=` is not the canonical encoding it would load."""
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    header, payload, signature = _jwt(_CODEX_CLAIMS).split(".")
    tampered = f"{header}.{payload}=.{signature}"
    (home / ".codex" / "auth.json").write_text(
        json.dumps(_codex_auth(None) | {"tokens": {"id_token": tampered}}), "utf-8"
    )

    assert signed_in("codex", home=home) is None


def test_no_codex_secret_reaches_the_account(tmp_path: Path) -> None:
    """The file holds the access token, the refresh token and any API key
    beside the identity token. None of them is in what comes back."""
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(json.dumps(_codex_auth(_CODEX_CLAIMS)), "utf-8")

    found = signed_in("codex", home=home)

    assert found is not None
    assert not any(secret in repr(found) for secret in _SECRETS)


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


@pytest.mark.parametrize("kind", ["claude-code", "codex", "cursor"])
def test_a_kind_read_from_disk_has_no_query(tmp_path: Path, kind: str) -> None:
    assert sign_in_query(kind, home=_home(tmp_path)) is None


# --- codex's credential store ------------------------------------------------


@pytest.mark.parametrize("store", ["keyring", "ephemeral", "something-new"])
def test_a_codex_sign_in_kept_off_disk_is_not_read_from_a_stale_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: str
) -> None:
    """`cli_auth_credentials_store` may keep the sign-in in the OS keyring or
    in memory. Codex deletes `auth.json` on every save to the keyring, so one
    left behind is an earlier sign-in: no account, never a stale one."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(json.dumps(_codex_auth(_CODEX_CLAIMS)), "utf-8")
    (home / ".codex" / "config.toml").write_text(
        f'cli_auth_credentials_store = "{store}"\n', "utf-8"
    )

    assert signed_in("codex", home=home) is None


@pytest.mark.parametrize(
    "config", ['cli_auth_credentials_store = "file"\n', 'model = "o3"\n', "not = [toml"]
)
def test_codex_reads_auth_json_when_that_is_its_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str
) -> None:
    """`file` is Codex's default, and a `config.toml` that does not parse
    leaves the default."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(json.dumps(_codex_auth(_CODEX_CLAIMS)), "utf-8")
    (home / ".codex" / "config.toml").write_text(config, "utf-8")

    found = signed_in("codex", home=home)

    assert found is not None and found.kind == "codex"


# --- what to watch -----------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "relatives"),
    [
        ("claude-code", (".claude/.config.json", ".claude.json")),
        ("codex", (".codex/config.toml", ".codex/auth.json")),
        ("devin", (".local/share/devin/credentials.toml",)),
    ],
)
def test_each_kind_names_every_file_its_sign_in_depends_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, relatives: tuple[str, ...]
) -> None:
    """A consumer asks again when any of these changes. Devin's is a network
    call, so asking only then is the difference between once and on every
    pass. Named whether or not each exists yet."""
    for variable in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(variable, raising=False)
    home = _home(tmp_path)

    assert sign_in_records(kind, home=home) == tuple(home / r for r in relatives)


@pytest.mark.parametrize("store", ["file", "keyring", "ephemeral", "auto"])
def test_codexs_store_selector_is_watched_whatever_it_selects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store: str
) -> None:
    """Switching `cli_auth_credentials_store` moves the sign-in on or off
    `auth.json` without that file changing, either way, so both files are
    always named."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(
        f'cli_auth_credentials_store = "{store}"\n', "utf-8"
    )

    assert sign_in_records("codex", home=home) == (
        home / ".codex" / "config.toml",
        home / ".codex" / "auth.json",
    )


def test_a_kind_with_no_reader_names_no_file(tmp_path: Path) -> None:
    assert sign_in_records("cursor", home=_home(tmp_path)) == ()


def test_a_kind_with_no_reader_is_signed_in_as_no_one(tmp_path: Path) -> None:
    assert signed_in("cursor", home=_home(tmp_path)) is None


@pytest.mark.parametrize(
    ("kind", "relative", "document"),
    [
        ("claude-code", ".claude.json", {"oauthAccount": _CLAUDE}),
        ("codex", ".codex/auth.json", _codex_auth(_CODEX_CLAIMS)),
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


def _codex_signed_in_on_disk(home: Path, config: str | None) -> None:
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(json.dumps(_codex_auth(_CODEX_CLAIMS)), "utf-8")
    if config is not None:
        (home / ".codex" / "config.toml").write_text(config, "utf-8")


def test_codex_auto_reads_the_file_it_fell_back_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under `auto` Codex tries the keyring, then `auth.json`, and deletes the
    file on every keyring save: a file that is there is the store in use."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = _home(tmp_path)
    _codex_signed_in_on_disk(home, 'cli_auth_credentials_store = "auto"\n')

    assert signed_in("codex", home=home) is not None

    (home / ".codex" / "auth.json").unlink()
    assert signed_in("codex", home=home) is None
    # Still watched: a later sign-in the keyring refuses creates it.
    assert home / ".codex" / "auth.json" in sign_in_records("codex", home=home)


def test_an_api_key_in_the_environment_is_outside_the_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`codex exec` prefers `CODEX_API_KEY` and the interactive Codex ignores
    it, per process. Which process ran a session is not on disk, so the
    stored login is the answer (ADR-0027 clause 5)."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("CODEX_API_KEY", "sk-API-SECRET")
    home = _home(tmp_path)
    _codex_signed_in_on_disk(home, None)

    found = signed_in("codex", home=home)

    assert found is not None and "sk-API-SECRET" not in repr(found)


def test_a_profile_choosing_a_store_is_outside_the_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0027 clause 5 reads the top-level store only. A profile's choice is
    applied when Codex runs with that profile, which this package cannot see,
    so the documented path is the top-level answer."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    home = _home(tmp_path)
    _codex_signed_in_on_disk(home, '[profiles.work]\ncli_auth_credentials_store = "keyring"\n')

    assert signed_in("codex", home=home) is not None


@pytest.mark.parametrize(
    ("claims", "account_id", "email"),
    [
        (
            {"sub": "s", "email": "a@x.com", _AUTH: {"chatgpt_user_id": "cu", "user_id": "u"}},
            "cu",
            "a@x.com",
        ),
        ({"sub": "s", _AUTH: {"user_id": "u"}}, "u", None),
        ({"sub": "s", _AUTH: {"user_id": "u"}, _PROFILE: {"email": "p@x.com"}}, "u", "p@x.com"),
        (
            {
                "sub": "s",
                "email": "a@x.com",
                _AUTH: {"user_id": "u"},
                _PROFILE: {"email": "p@x.com"},
            },
            "u",
            "a@x.com",
        ),
    ],
    ids=["chatgpt-user-id", "user-id", "profile-email", "top-level-email-wins"],
)
def test_codex_is_read_as_codex_reads_its_own_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    claims: dict[str, object],
    account_id: str,
    email: str | None,
) -> None:
    """Codex's `parse_chatgpt_jwt_claims` takes the user from
    `chatgpt_user_id`, else `user_id`, and the email from the top level, else
    the profile claim. Read any other way, one person's sessions split across
    two ids, or their email goes missing."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(json.dumps(_codex_auth(claims)), "utf-8")

    found = signed_in("codex", home=home)

    assert found is not None
    assert (found.account_id, found.email) == (account_id, email)


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


@pytest.mark.parametrize(
    ("fields", "named"),
    [
        ({"auth_mode": "chatgpt", "OPENAI_API_KEY": "sk-API-SECRET"}, True),
        ({"auth_mode": "chatgptAuthTokens"}, True),
        ({"auth_mode": "apikey"}, False),
        ({"auth_mode": "agentIdentity"}, False),
        ({"auth_mode": "chatgpt-old"}, False),
        ({"auth_mode": "ChatGPT"}, False),
        ({"auth_mode": 1}, False),
        ({"auth_mode": ["chatgpt"]}, False),
        ({"auth_mode": {"mode": "chatgpt"}}, False),
        ({"OPENAI_API_KEY": None}, True),
        ({"OPENAI_API_KEY": "sk-API-SECRET"}, False),
        ({"personal_access_token": "pat-SECRET"}, False),
        ({"bedrock_api_key": {"key": "SECRET"}}, False),
        ({}, True),
    ],
    ids=[
        "explicit-chatgpt-wins",
        "chatgpt-tokens",
        "explicit-apikey",
        "other-explicit-mode",
        "unknown-mode",
        "wrong-case",
        "not-a-string",
        "unhashable-list",
        "unhashable-dict",
        "null-key-is-chatgpt",
        "older-file-with-a-key",
        "personal-access-token",
        "bedrock-key",
        "nothing-but-tokens",
    ],
)
def test_codex_names_a_person_only_when_it_signs_in_with_chatgpt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fields: dict[str, object], named: bool
) -> None:
    """Codex's `resolved_mode`: an explicit `auth_mode` wins, else a key field
    that is set means a key, else ChatGPT. Tokens left in a file that signs in
    with a key belong to no one signed in now."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    document = {"tokens": {"id_token": _jwt(_CODEX_CLAIMS)}, **fields}
    (home / ".codex" / "auth.json").write_text(json.dumps(document), "utf-8")

    found = signed_in("codex", home=home)

    assert (found is not None) == named
    assert "SECRET" not in repr(found)


# --- the codex reader reads only what codex defines --------------------------

#: Every key Codex's own code reads from the files this reader reads, as of
#: Codex `main` on 2026-10-11: `config.toml`'s `cli_auth_credentials_store`;
#: `AuthDotJson` (`login/src/auth/manager.rs`, with `resolved_mode`'s fields);
#: and `IdClaims`, `AuthClaims` and `ProfileClaims`
#: (`login/src/token_data.rs`, `parse_chatgpt_jwt_claims`).
_CODEX_DEFINES = frozenset(
    {
        "cli_auth_credentials_store",
        "auth_mode",
        "tokens",
        "id_token",
        "OPENAI_API_KEY",
        "personal_access_token",
        "bedrock_api_key",
        "bedrock_access_keys",
        "chatgpt_user_id",
        "user_id",
        "chatgpt_plan_type",
        "email",
    }
)

#: Read beyond what Codex defines, each with why it cannot split or invent an
#: identity.
_READ_FOR_DISPLAY_ONLY = {
    "name": "The OpenID `name` claim the token carries. A display name only; "
    "it never identifies the account.",
}


def _codex_keys_read() -> set[str]:
    """Every string key the Codex reader passes to `.get`, or lists as a field
    it checks, in `accounts.py`."""
    import ast

    tree = ast.parse(Path(accounts.__file__).read_text(encoding="utf-8"))
    keys: set[str] = set()
    for node in tree.body:
        named = getattr(node, "name", None) or next(
            (t.id for t in getattr(node, "targets", []) if isinstance(t, ast.Name)), ""
        )
        if not named.lower().startswith(("_codex", "codex")):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "get"
                and inner.args
                and isinstance(inner.args[0], ast.Constant)
                and isinstance(inner.args[0].value, str)
            ):
                keys.add(inner.args[0].value)
        if named == "_CODEX_NON_CHATGPT_FIELDS":
            keys |= {
                c.value
                for c in ast.walk(node)
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            }
    return keys


def test_the_codex_reader_reads_only_what_codex_itself_defines() -> None:
    """Four review rounds found one class: this reader taking a Codex field
    Codex does not read (`sub` as the account), or reading one Codex reads in
    another order. Mirroring Codex's own types (ADR-0027 clause 5) is held
    here for every key at once: a key Codex does not define fails, unless it
    is for display only and says why."""
    unexplained = _codex_keys_read() - _CODEX_DEFINES - set(_READ_FOR_DISPLAY_ONLY)

    assert not unexplained, (
        f"the Codex reader reads {sorted(unexplained)}, which Codex itself does "
        "not; mirror Codex's own reading, or list it as display only with why"
    )


def test_the_codex_key_scan_sees_the_reader() -> None:
    """The scan above passes vacuously if it finds nothing."""
    assert {"chatgpt_user_id", "auth_mode", "cli_auth_credentials_store"} <= _codex_keys_read()


def test_a_codex_token_with_no_user_claim_names_no_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex reads no user claim but `chatgpt_user_id` and `user_id`. A token
    with only `sub` names no account Codex would recognise."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(
        json.dumps(_codex_auth({"sub": "s", "email": "a@x.com", _AUTH: {}})), "utf-8"
    )

    assert signed_in("codex", home=home) is None


def _payload_ending_in(remainder: int) -> tuple[str, str, str]:
    """A token's header, payload and signature, the payload's length leaving
    `remainder` after division by 4, so it ends in a symbol with unused bits,
    and holding a URL-safe `-` or `_` (`~~~` encodes as `fn5-`)."""
    for pad in range(16):
        claims = {**_CODEX_CLAIMS, "pad": "~~~" + "x" * pad}
        header, payload, signature = _jwt(claims).split(".")
        if len(payload) % 4 == remainder and set("-_") & set(payload):
            return header, payload, signature
    raise AssertionError("no such payload found")


@pytest.mark.parametrize("remainder", [2, 3])
@pytest.mark.parametrize(
    "spoil",
    ["canonical padding", "a non-canonical final symbol", "the standard alphabet"],
)
def test_a_payload_codex_would_refuse_names_no_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, remainder: int, spoil: str
) -> None:
    """Codex decodes the payload with Rust's `URL_SAFE_NO_PAD`, which refuses
    padding, the standard alphabet's `+` and `/`, and a final symbol whose
    unused bits are set. Python accepts all three, so each must be refused
    here, or an account comes from a token Codex cannot load."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    header, payload, signature = _payload_ending_in(remainder)
    if spoil == "canonical padding":
        spoiled = payload + "=" * (-len(payload) % 4)
    elif spoil == "a non-canonical final symbol":
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        spoiled = payload[:-1] + alphabet[alphabet.index(payload[-1]) ^ 1]
    else:
        spoiled = payload.replace("-", "+").replace("_", "/")
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    for token, named in (
        (f"{header}.{payload}.{signature}", True),
        (f"{header}.{spoiled}.{signature}", False),
    ):
        document = {"auth_mode": "chatgpt", "tokens": {"id_token": token}}
        (home / ".codex" / "auth.json").write_text(json.dumps(document), "utf-8")
        assert (signed_in("codex", home=home) is not None) == named, token
