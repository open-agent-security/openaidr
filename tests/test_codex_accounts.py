"""Codex's sign-in, read from its `auth.json` (ADR-0027; `docs/specs/codex.md`)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from openaidr import accounts
from openaidr.accounts import sign_in_query, sign_in_records, signed_in
from openaidr.model import Account


@pytest.fixture(autouse=True)
def _no_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)


_SECRETS = ("ACCESS-SECRET", "REFRESH-SECRET", "sk-API-SECRET")

_AUTH = "https://api.openai.com/auth"

_PROFILE = "https://api.openai.com/profile"

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


def _codex_signed_in_on_disk(home: Path, config: str | None) -> None:
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(json.dumps(_codex_auth(_CODEX_CLAIMS)), "utf-8")
    if config is not None:
        (home / ".codex" / "config.toml").write_text(config, "utf-8")


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


_READ_FOR_DISPLAY_ONLY = {
    "name": "The OpenID `name` claim the token carries. A display name only; "
    "it never identifies the account.",
}


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def _jwt(claims: dict[str, object]) -> str:
    def part(value: object) -> str:
        raw = json.dumps(value).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature-not-checked"


def _codex_auth(claims: dict[str, object] | None) -> dict[str, object]:
    tokens = {"access_token": _SECRETS[0], "refresh_token": _SECRETS[1], "account_id": "ws-1"}
    if claims is not None:
        tokens["id_token"] = _jwt(claims)
    return {"auth_mode": "chatgpt", "OPENAI_API_KEY": _SECRETS[2], "tokens": tokens}


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
        {"tokens": {"id_token": f".{_jwt(_CODEX_CLAIMS).split('.')[1]}.sig"}},
        {"tokens": {"id_token": f"header.{_jwt(_CODEX_CLAIMS).split('.')[1]}."}},
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


def test_a_payload_nested_past_the_decoders_limit_is_a_malformed_token(tmp_path: Path) -> None:
    """A payload nested deep enough makes `json.loads` raise `RecursionError`,
    not `ValueError`. `signed_in` never raises for a malformed record, so this
    is a malformed token like any other, not an exception that escapes it
    (P2 Codex review finding on PR #76)."""
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    header, _, signature = _jwt(_CODEX_CLAIMS).split(".")
    depth = 100_000
    nested = (b"[" * depth) + (b"]" * depth)
    payload = base64.urlsafe_b64encode(nested).decode("ascii").rstrip("=")
    tampered = f"{header}.{payload}.{signature}"
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


def test_codex_is_read_from_disk_and_has_no_query(tmp_path: Path) -> None:
    assert sign_in_query("codex", home=_home(tmp_path)) is None


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


@pytest.mark.parametrize("config", ['cli_auth_credentials_store = "file"\n', 'model = "o3"\n'])
def test_codex_reads_auth_json_when_that_is_its_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: str
) -> None:
    """`file` is Codex's default."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(json.dumps(_codex_auth(_CODEX_CLAIMS)), "utf-8")
    (home / ".codex" / "config.toml").write_text(config, "utf-8")

    found = signed_in("codex", home=home)

    assert found is not None and found.kind == "codex"


def test_a_missing_codex_config_defaults_to_the_file_store(tmp_path: Path) -> None:
    """Codex's own loader treats a missing `config.toml` as `Ok(None)` and
    falls back to its defaults, `file` among them."""
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(json.dumps(_codex_auth(_CODEX_CLAIMS)), "utf-8")

    found = signed_in("codex", home=home)

    assert found is not None and found.kind == "codex"


def test_an_existing_but_malformed_codex_config_is_not_defaulted(tmp_path: Path) -> None:
    """Codex's own loader errors on an existing `config.toml` it cannot parse
    rather than falling back to its defaults; it never gets far enough to use
    `auth.json` either, so this reads as no sign-in, not a guessed store."""
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(json.dumps(_codex_auth(_CODEX_CLAIMS)), "utf-8")
    (home / ".codex" / "config.toml").write_text("not = [toml", "utf-8")

    assert signed_in("codex", home=home) is None


def test_a_config_nested_past_the_decoders_limit_is_a_malformed_config(tmp_path: Path) -> None:
    """`tomllib.loads` on a sufficiently deep but syntactically valid array
    raises `RecursionError`, not `TOMLDecodeError`; this is a malformed
    config like any other, not an exception that escapes `signed_in`
    (P2 Codex review finding on PR #76)."""
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    (home / ".codex" / "auth.json").write_text(json.dumps(_codex_auth(_CODEX_CLAIMS)), "utf-8")
    depth = 2000
    nested = "a = " + ("[" * depth) + ("]" * depth)
    (home / ".codex" / "config.toml").write_text(nested, "utf-8")

    assert signed_in("codex", home=home) is None


def test_codex_names_every_file_its_sign_in_depends_on(tmp_path: Path) -> None:
    """Named whether or not each exists yet."""
    home = _home(tmp_path)

    assert sign_in_records("codex", home=home) == (
        home / ".codex" / "config.toml",
        home / ".codex" / "auth.json",
    )


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


@pytest.mark.parametrize(
    ("kind", "relative", "document"),
    [("codex", ".codex/auth.json", _codex_auth(_CODEX_CLAIMS))],
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
