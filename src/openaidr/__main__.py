"""Command-line entry point for OpenAIDR."""

import argparse
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openaidr",
        description="Session collection for AI coding agents.",
    )
    parser.add_argument("--version", action="version", version=f"openaidr {__version__}")
    parser.parse_args(argv)

    print(
        "openaidr is pre-alpha: session collection is not implemented yet.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
