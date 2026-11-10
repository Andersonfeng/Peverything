"""Entry point for the windowed executable (``dist\\Peverything.exe``).

Double-clicking the exe passes no arguments, but the CLI expects a sub-command,
so ``gui`` is injected when the first argument is not one of them.
"""

from __future__ import annotations

import sys

from .cli import main

SUBCOMMANDS = frozenset({"scan", "search", "info", "watch", "gui"})


def main_gui(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("--version", "-h", "--help"):
        return main(args)
    if not args or args[0] not in SUBCOMMANDS:
        args = ["gui", *args]
    return main(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main_gui())
