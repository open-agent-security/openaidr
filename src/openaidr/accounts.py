"""Who each agent kind is signed in as on this machine (ADR-0027).

Each agent keeps its own sign-in record, in its own file and shape. This module
reads it, one file read per call, and answers in one vocabulary, so a consumer
asks `signed_in(kind)` and never learns where any agent keeps it. Devin keeps
no record of the person on disk, so it is asked through its own `devin auth
status`, which uses Devin's own credential.

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
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from .model import Account

#: Codex's claims about the ChatGPT account live under this one key of its
#: identity token.
_OPENAI_AUTH_CLAIM = "https://api.openai.com/auth"


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
    the person is. Signed in with an API key there is no person to name.
    """
    document = _json(_codex_record(config_dir, home))
    tokens = document.get("tokens") if isinstance(document, dict) else None
    id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
    claims = _token_claims(id_token)
    auth = _strings(claims.get(_OPENAI_AUTH_CLAIM))
    names = _strings(claims)
    account_id = auth.get("chatgpt_user_id") or names.get("sub")
    if account_id is None:
        return None
    return Account(
        kind="codex",
        account_id=account_id,
        display_name=names.get("name"),
        email=names.get("email"),
        plan=auth.get("chatgpt_plan_type"),
    )


#: Where Devin Desktop ships its copy of the CLI. A machine with Desktop alone
#: has no other.
_DEVIN_APP_BINARIES: tuple[Path, ...] = (
    Path("/Applications/Devin.app/Contents/Resources/app/extensions/windsurf/devin/bin/devin"),
)

#: `devin auth status` asks Devin's server, so it is bounded.
_DEVIN_TIMEOUT_SECONDS = 15.0

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _devin(config_dir: Path | None, home: Path) -> Account | None:
    """Who Devin's own `devin auth status` says is signed in.

    Devin's files name only the organization, and its session store has no
    user column. Its credential file holds the API key alone, which is
    Devin's to use, not this package's to read. `auth status` uses it and
    reports the user. That costs a call to Devin's server, so a consumer asks
    when the sign-in changes (`sign_in_record`), not on every pass. Offline,
    Devin cannot say who, and the answer is None.
    """
    del config_dir
    binary = _devin_binary(home)
    if binary is None:
        return None
    environment = {**os.environ, "HOME": str(home), "NO_COLOR": "1", "TERM": "dumb"}
    try:
        finished = subprocess.run(
            [str(binary), "auth", "status"],
            capture_output=True,
            text=True,
            timeout=_DEVIN_TIMEOUT_SECONDS,
            env=environment,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if finished.returncode != 0:
        return None
    sections = _sections(_ANSI.sub("", finished.stdout))
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
    reader. A consumer asks `signed_in` again when it changes, which for Devin
    is the difference between one call to its server and one per pass. Only
    the file's existence and time are a consumer's business: for Devin it
    holds the credential."""
    home = Path.home() if home is None else home
    if kind == "claude-code":
        return _claude_record(config_dir, home)
    if kind == "codex":
        return _codex_record(config_dir, home)
    if kind == "devin":
        return _devin_data_root(home) / "credentials.toml"
    return None


def _claude_record(config_dir: Path | None, home: Path) -> Path:
    """Claude Code's configuration file: `~/.claude.json` beside the default
    directory, inside `CLAUDE_CONFIG_DIR` when set, and a legacy `.config.json`
    in the directory over either, as Claude Code reads it first."""
    root = config_dir if config_dir is not None else _env_dir("CLAUDE_CONFIG_DIR", home / ".claude")
    legacy = root / ".config.json"
    if legacy.is_file():
        return legacy
    return home / ".claude.json" if root == home / ".claude" else root / ".claude.json"


def _codex_record(config_dir: Path | None, home: Path) -> Path:
    root = config_dir if config_dir is not None else _env_dir("CODEX_HOME", home / ".codex")
    return root / "auth.json"


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
