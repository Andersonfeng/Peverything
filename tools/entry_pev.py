"""PyInstaller entry script for the console executable (``pev.exe``)."""

import sys

from peverything.cli import main

if __name__ == "__main__":
    sys.exit(main())
