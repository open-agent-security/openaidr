"""Who each agent kind is signed in as on this machine (ADR-0027).

Each agent keeps its own sign-in record, in its own file and shape. This module
reads it, one file read per call, and answers in one vocabulary, so a consumer
asks `signed_in(kind)` and never learns where any agent keeps it.

It reports what the agent wrote down and nothing more: no lookup elsewhere, no
matching of one agent's account to another's. A kind with no local record of
the person returns None rather than a guess.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
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
    root = config_dir if config_dir is not None else _env_dir("CLAUDE_CONFIG_DIR", home / ".claude")
    legacy = root / ".config.json"
    if legacy.is_file():
        path = legacy
    elif root == home / ".claude":
        path = home / ".claude.json"
    else:
        path = root / ".claude.json"
    document = _json(path)
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
    root = config_dir if config_dir is not None else _env_dir("CODEX_HOME", home / ".codex")
    document = _json(root / "auth.json")
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


def _devin(config_dir: Path | None, home: Path) -> Account | None:
    """Devin keeps no record of the person on this machine.

    Its configuration names the organization (`org_id`) and its session store
    has no user column, so there is nothing to report until Devin writes one.
    """
    del config_dir, home
    return None


_READERS: dict[str, Callable[[Path | None, Path], Account | None]] = {
    "claude-code": _claude_code,
    "codex": _codex,
    "devin": _devin,
}


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
