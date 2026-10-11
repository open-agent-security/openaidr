"""Who each agent kind is signed in as (ADR-0027)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from openaidr.accounts import signed_in
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


def test_codex_falls_back_to_the_tokens_subject_for_its_account_id(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / ".codex").mkdir()
    claims: dict[str, object] = {"sub": "auth0|abc", "email": "jane.doe@example.com"}
    (home / ".codex" / "auth.json").write_text(json.dumps(_codex_auth(claims)), "utf-8")

    found = signed_in("codex", home=home)

    assert found is not None and (found.account_id, found.plan) == ("auth0|abc", None)


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


def test_devin_keeps_no_record_of_the_person(tmp_path: Path) -> None:
    """Its configuration names the organization and its session store has no
    user column: nothing to report."""
    home = _home(tmp_path)
    (home / ".config" / "devin").mkdir(parents=True)
    (home / ".config" / "devin" / "config.json").write_text(
        json.dumps({"devin": {"org_id": "org-1"}}), "utf-8"
    )

    assert signed_in("devin", home=home) is None


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
