"""Peverything - a fast file search engine for Windows, written in Python.

The design follows the same idea as Everything: read the NTFS Master File
Table (through the USN journal APIs) to build an in-memory index of every
file name on a volume in a couple of seconds, then answer queries against
that index without touching the disk again.
"""

__version__ = "1.0.2"
__all__ = ["__version__", "APP_NAME"]

APP_NAME = "Peverything"
