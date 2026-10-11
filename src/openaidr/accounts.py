"""Who each agent kind is signed in as on this machine (ADR-0027).

Each agent keeps its own sign-in record, in its own file and shape. This module
reads it, one file read per call, and answers in one vocabulary, so a consumer
asks `signed_in(kind)` and never learns where any agent keeps it.

Devin keeps no record of the person on disk; only its own `devin auth status`
can say, by asking Devin's server. This package never runs it: its sensing is
read-only. `sign_in_query(kind)` says how to ask and reads the answer, and a
consumer that chooses to ask runs it.

It reports what the agent says and nothing more: no lookup elsewhere, no
matching of one agent's account to another's. A kind that cannot say returns
None rather than a guess.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .model import Account

#: Codex's claims about the ChatGPT account, and its profile, live under these
#: keys of its identity token.
_OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"
_OPENAI_PROFILE_CLAIM = "https://api.openai.com/profile"


def signed_in(
    kind: str, *, config_dir: Path | None = None, home: Path | None = None
) -> Account | None:
    """The account `kind` is signed in as now, or None.

    `config_dir` is the agent's own configuration directory when it is not the
    default: Claude Code's `~/.claude` (or `CLAUDE_CONFIG_DIR`), Codex's
    `~/.codex` (or `CODEX_HOME`). `home` stands in for the home directory.
    Never raises: a missing, unreadable or malformed record is no account.
    """
    reader = _READERS.get(kind)
    if reader is None:
        return None
    return reader(config_dir, Path.home() if home is None else home)


def _claude_code(config_dir: Path | None, home: Path) -> Account | None:
    """`oauthAccount` in Claude Code's configuration file.

    With the default directory the file is `~/.claude.json`, beside it; with
    `CLAUDE_CONFIG_DIR` set Claude Code keeps it inside that directory and
    writes nothing to the home directory. A legacy `.config.json` in the
    directory wins over either, as Claude Code reads it first.
    """
    document = _json(_claude_record(config_dir, home))
    fields = _strings(document.get("oauthAccount") if isinstance(document, dict) else None)
    account_id = fields.get("accountUuid")
    if account_id is None:
        return None
    return Account(
        kind="claude-code",
        account_id=account_id,
        display_name=fields.get("displayName"),
        email=fields.get("emailAddress"),
        plan=fields.get("billingType"),
    )


def _codex(config_dir: Path | None, home: Path) -> Account | None:
    """The identity token in Codex's `auth.json`, when signed in with ChatGPT.

    Only the token's payload is decoded, and only its identity claims are kept.
    The same file holds the access token, the refresh token and any API key;
    none of them is read into anything this returns. The signature is not
    checked: this reports what Codex holds on this machine, not a proof of who
    the person is. Signed in with an API key there is no person to name, and
    where Codex keeps the sign-in off `auth.json` there is none to read here
    (`_codex_record`).
    """
    record = _codex_record(config_dir, home)
    if record is None:
        return None
    document = _json(record)
    if not isinstance(document, dict) or not _codex_signs_in_with_chatgpt(document):
        return None
    tokens = document.get("tokens")
    id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
    claims = _token_claims(id_token)
    auth = _strings(claims.get(_OPENAI_AUTH_CLAIM))
    profile = _strings(claims.get(_OPENAI_PROFILE_CLAIM))
    names = _strings(claims)
    # Codex's own reading of the same token (`parse_chatgpt_jwt_claims`): the
    # user is `chatgpt_user_id`, else `user_id`, and the email is the
    # top-level claim, else the profile's. `sub` is the last resort, for a
    # token with neither user claim.
    account_id = auth.get("chatgpt_user_id") or auth.get("user_id") or names.get("sub")
    if account_id is None:
        return None
    return Account(
        kind="codex",
        account_id=account_id,
        display_name=names.get("name"),
        email=names.get("email") or profile.get("email"),
        plan=auth.get("chatgpt_plan_type"),
    )


#: Fields of Codex's `auth.json` that, present without an explicit
#: `auth_mode`, make Codex sign in with something other than ChatGPT, in
#: Codex's own order (`resolved_mode`).
_CODEX_NON_CHATGPT_FIELDS = (
    "personal_access_token",
    "bedrock_api_key",
    "bedrock_access_keys",
    "OPENAI_API_KEY",
)


def _codex_signs_in_with_chatgpt(document: Mapping[str, object]) -> bool:
    """Whether Codex uses the file's ChatGPT tokens, as its `resolved_mode`
    decides: an explicit `auth_mode` wins, else any key field present means a
    key, else ChatGPT. Only the ChatGPT modes carry a person. A key field is
    asked whether it is set, never what it holds."""
    mode = document.get("auth_mode")
    if isinstance(mode, str):
        return mode.lower().startswith("chatgpt")
    return not any(document.get(name) is not None for name in _CODEX_NON_CHATGPT_FIELDS)


#: Where Devin Desktop ships its copy of the CLI. A machine with Desktop alone
#: has no other.
_DEVIN_APP_BINARIES: tuple[Path, ...] = (
    Path("/Applications/Devin.app/Contents/Resources/app/extensions/windsurf/devin/bin/devin"),
)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _devin(config_dir: Path | None, home: Path) -> Account | None:
    """Devin keeps no record of the person on disk: its configuration names
    the organization, its session store has no user column, and its
    credentials file holds the API key alone. `sign_in_query("devin")` says
    how to ask Devin itself."""
    del config_dir, home
    return None


@dataclass(frozen=True)
class SignInQuery:
    """How to ask an agent who is signed in, for a kind that keeps no record
    of the person on disk (ADR-0027).

    This package never runs it. The agent answers by asking its own server
    with its own credential, which is not read-only sensing. A consumer that
    chooses to ask runs `argv` with `environment` added to its own, bounded
    by a timeout of its own choosing, and hands what it printed to `read`.
    """

    kind: str
    argv: tuple[str, ...]
    environment: Mapping[str, str] = field(default_factory=dict)

    def read(self, output: str) -> Account | None:
        """The account the agent's answer names, or None for one that names
        no user: signed out, offline, or a layout this package does not know."""
        return _QUERY_READERS[self.kind](output)


def sign_in_query(kind: str, *, home: Path | None = None) -> SignInQuery | None:
    """How to ask `kind` who is signed in, or None for a kind that is read
    from disk (`signed_in`) or that cannot be asked."""
    if kind != "devin":
        return None
    explicit_home = home is not None
    home = Path.home() if home is None else home
    binary = _devin_binary(home)
    if binary is None:
        return None
    environment = {"NO_COLOR": "1", "TERM": "dumb"}
    if explicit_home:
        environment["HOME"] = str(home)
    return SignInQuery(kind="devin", argv=(str(binary), "auth", "status"), environment=environment)


def _devin_status(output: str) -> Account | None:
    """`devin auth status`'s `User` and `Account` sections. Devin prints them
    only once its server has answered; offline it prints neither."""
    sections = _sections(_ANSI.sub("", output))
    user = sections.get("User", {})
    account_id = user.get("User ID")
    if account_id is None:
        return None
    return Account(
        kind="devin",
        account_id=account_id,
        display_name=user.get("Name"),
        email=user.get("Email"),
        plan=sections.get("Account", {}).get("Plan"),
    )


_QUERY_READERS: dict[str, Callable[[str], Account | None]] = {"devin": _devin_status}


def _devin_data_root(home: Path) -> Path:
    return _env_dir("XDG_DATA_HOME", home / ".local" / "share") / "devin"


def _devin_binary(home: Path) -> Path | None:
    """The CLI where it installs itself, else on PATH, else Desktop's copy."""
    installed = _devin_data_root(home) / "cli" / "_versions" / "current" / "bin" / "devin"
    on_path = shutil.which("devin")
    for candidate in (installed, Path(on_path) if on_path else None, *_DEVIN_APP_BINARIES):
        if candidate is not None and candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _sections(text: str) -> dict[str, dict[str, str]]:
    """`Heading:` lines, each followed by indented `Key: value` lines."""
    sections: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        if line and not line[0].isspace() and line.rstrip().endswith(":"):
            current = sections.setdefault(line.strip()[:-1], {})
        elif current is not None and line[:1].isspace() and ":" in line:
            key, _, value = line.strip().partition(":")
            if value.strip():
                current[key.strip()] = value.strip()
    return sections


_READERS: dict[str, Callable[[Path | None, Path], Account | None]] = {
    "claude-code": _claude_code,
    "codex": _codex,
    "devin": _devin,
}


def sign_in_record(
    kind: str, *, config_dir: Path | None = None, home: Path | None = None
) -> Path | None:
    """The file a new sign-in rewrites for `kind`, or None for a kind with no
    reader or a sign-in kept off disk. A consumer asks again when it changes,
    which for Devin's `sign_in_query` is the difference between one call to
    its server and one per pass. Only the file's existence and time are a
    consumer's business: for Devin it holds the credential."""
    home = Path.home() if home is None else home
    if kind == "claude-code":
        return _claude_record(config_dir, home)
    if kind == "codex":
        return _codex_record(config_dir, home)
    if kind == "devin":
        return _devin_data_root(home) / "credentials.toml"
    return None


def _claude_record(config_dir: Path | None, home: Path) -> Path:
    """Claude Code's configuration file, where Claude Code itself looks: inside
    the configuration directory whenever one is named, by `CLAUDE_CONFIG_DIR`
    or by the caller, even when it names `~/.claude`; else `~/.claude.json`.
    A legacy `.config.json` in the directory wins, as Claude Code reads it
    first. Named or not is the test, never whether the path equals the
    default."""
    named = config_dir if config_dir is not None else _named_dir("CLAUDE_CONFIG_DIR")
    root = named if named is not None else home / ".claude"
    legacy = root / ".config.json"
    if legacy.is_file():
        return legacy
    return root / ".claude.json" if named is not None else home / ".claude.json"


def _codex_record(config_dir: Path | None, home: Path) -> Path | None:
    """`auth.json`, where Codex's configured store can keep the sign-in, else
    None (ADR-0027 clause 5, checked against Codex's auth storage).

    `cli_auth_credentials_store` in Codex's `config.toml` chooses the store.
    `file`, the default, and `auto` use `auth.json`: `auto` tries the keyring
    first, falls back to the file, and deletes the file on every keyring
    save, so a file that is there is the one in use. The path is returned
    whether or not the file exists yet, so a sign-in that creates it is seen.
    `keyring`, `ephemeral` and any other value keep the sign-in off disk. A
    `config.toml` that does not parse leaves the default.
    """
    root = config_dir if config_dir is not None else _env_dir("CODEX_HOME", home / ".codex")
    try:
        config = tomllib.loads((root / "config.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        config = {}
    store = config.get("cli_auth_credentials_store", "file")
    return root / "auth.json" if store in ("file", "auto") else None


def _named_dir(variable: str) -> Path | None:
    value = os.environ.get(variable)
    return Path(value) if value else None


def _env_dir(variable: str, default: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value) if value else default


def _json(path: Path) -> object:
    """One read of `path` as JSON, or None. One read, so fields used together
    come from the same account even while the agent rewrites the file."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _strings(value: object) -> dict[str, str]:
    """The non-empty string fields of a JSON object; empty for anything else."""
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items() if isinstance(item, str) and item.strip()}


def _token_claims(token: object) -> dict[str, object]:
    """The payload of a JWT, decoded but not verified; empty when malformed."""
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    try:
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        claims = json.loads(decoded)
    except (binascii.Error, ValueError):
        return {}
    return claims if isinstance(claims, dict) else {}
