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
None rather than a guess. A kind is answered here once its sessions are read
here (ADR-0027 clause 1).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .model import Account


def signed_in(
    kind: str, *, config_dir: Path | None = None, home: Path | None = None
) -> Account | None:
    """The account `kind` is signed in as now, or None.

    `config_dir` is the agent's own configuration directory when it is not the
    default: Claude Code's `~/.claude` (or `CLAUDE_CONFIG_DIR`). `home` stands
    in for the home directory.
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
    "devin": _devin,
}


def sign_in_records(
    kind: str, *, config_dir: Path | None = None, home: Path | None = None
) -> tuple[Path, ...]:
    """Every file whose change can change who `kind` is signed in as, existing
    or not; empty for a kind with no reader. A consumer asks again when any
    of them is created, changed or removed, which for Devin's `sign_in_query`
    is the difference between one call to its server and one per pass. Only
    their existence and times are a consumer's business: some hold the
    credential.

    Claude Code's are the legacy `.config.json` and the file it falls back to:
    the legacy file winning is a change the fallback file need not show
    (ADR-0027 clause 5)."""
    home = Path.home() if home is None else home
    if kind == "claude-code":
        legacy, modern = _claude_paths(config_dir, home)
        return (legacy, modern)
    if kind == "devin":
        return (_devin_data_root(home) / "credentials.toml",)
    return ()


def _claude_record(config_dir: Path | None, home: Path) -> Path:
    """Claude Code's configuration file, where Claude Code itself looks: the
    legacy `.config.json` in the configuration directory when it is there, as
    Claude Code reads it first; else inside that directory whenever one is
    named, by `CLAUDE_CONFIG_DIR` or by the caller, even when it names
    `~/.claude`; else `~/.claude.json`."""
    legacy, modern = _claude_paths(config_dir, home)
    return legacy if legacy.is_file() else modern


def _claude_paths(config_dir: Path | None, home: Path) -> tuple[Path, Path]:
    """`(legacy, modern)`: the legacy `.config.json` Claude Code reads first
    when it is there, and the file it reads otherwise. Both are named
    whether or not each exists, so a consumer sees the legacy file appear.
    Named or not is the test for the directory, never whether its path
    equals the default."""
    named = config_dir if config_dir is not None else _named_dir("CLAUDE_CONFIG_DIR")
    root = named if named is not None else home / ".claude"
    # Claude Code names the file `.claude${suffix}.json`, the suffix
    # `-custom-oauth` when `CLAUDE_CODE_CUSTOM_OAUTH_URL` is set and empty in
    # every shipped build otherwise (ADR-0027 clause 3).
    suffix = "-custom-oauth" if os.environ.get("CLAUDE_CODE_CUSTOM_OAUTH_URL") else ""
    modern = (named if named is not None else home) / f".claude{suffix}.json"
    # Claude Code looks for the legacy file in its configuration directory
    # normalised to NFC, and for the other file in the directory as given.
    legacy = Path(unicodedata.normalize("NFC", str(root))) / ".config.json"
    return legacy, modern


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
