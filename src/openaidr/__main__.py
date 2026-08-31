"""Command-line entry point for OpenAIDR."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .collector import collect, default_readers
from .kinds import parse_kind_filter
from .readers.base import Window
from .render import parse_since, render_json, render_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openaidr",
        description="Session collection for AI coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"openaidr {__version__}")
    subcommands = parser.add_subparsers(dest="command")

    sessions = subcommands.add_parser("sessions", help="Print what each agent actually did.")
    sessions.add_argument(
        "--agent-kind",
        action="append",
        help="Only this agent kind. Repeatable. Default: every kind.",
    )
    sessions.add_argument("--since", default="14d", help="Window, as a day count (default: 14d).")
    sessions.add_argument("--format", choices=("text", "json"), default="text")
    sessions.add_argument(
        "--detail",
        action="store_true",
        help="List every tool call. Default: one line per session, plus a summary.",
    )

    args = parser.parse_args(argv)

    if args.command == "sessions":
        return _sessions(args)

    parser.print_help()
    return 1


def _sessions(args: argparse.Namespace) -> int:
    try:
        since = parse_since(args.since)
    except ValueError as error:
        print(f"openaidr: {error}", file=sys.stderr)
        return 2

    root = os.environ.get("OPENAIDR_CLAUDE_ROOT")
    collection = collect(
        parse_kind_filter(args.agent_kind),
        Window(since=since),
        default_readers(root=Path(root) if root else None),
    )
    output = (
        render_json(collection) if args.format == "json" else render_text(collection, args.detail)
    )
    print(output, end="" if output.endswith("\n") else "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
