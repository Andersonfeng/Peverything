"""PyInstaller entry script for the windowed executable (``Peverything.exe``)."""

import sys

from peverything.gui_entry import main_gui

if __name__ == "__main__":
    sys.exit(main_gui())
