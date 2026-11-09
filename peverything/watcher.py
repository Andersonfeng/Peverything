"""Live index updates driven by the NTFS USN change journal.

A USN record does not contain a full path, only the file reference of the
parent directory.  So a watcher bootstraps a ``file reference -> directory``
map (a directory-only MFT pass, a second or two) and then translates every new
record into a path.  That is the same trick Everything uses for real-time
updates without re-scanning.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .scanner import NTFS_METADATA, scan_mft_volume
from .usn import (
    ERROR_JOURNAL_ENTRY_DELETED,
    REASON_FILE_CREATE,
    REASON_FILE_DELETE,
    REASON_HARD_LINK_CHANGE,
    REASON_RENAME_NEW_NAME,
    REASON_RENAME_OLD_NAME,
    ROOT_FILE_REFERENCE,
    JournalInfo,
    UsnError,
    Volume,
)

WATCH_REASON_MASK = (
    REASON_FILE_CREATE
    | REASON_FILE_DELETE
    | REASON_RENAME_OLD_NAME
    | REASON_RENAME_NEW_NAME
    | REASON_HARD_LINK_CHANGE
)


class JournalReset(RuntimeError):
    """The journal was deleted/restarted; a full rescan is required."""


@dataclass
class ChangeSet:
    added: list[tuple[str, int, int, int]] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    volumes: set[str] = field(default_factory=set)
    skipped: int = 0

    def __bool__(self) -> bool:
        return bool(self.added or self.removed)

    def __len__(self) -> int:
        return len(self.added) + len(self.removed)

    def describe(self, limit: int = 20) -> str:
        lines = [f"+ {path}" for path, *_ in self.added[:limit]]
        lines += [f"- {path}" for path in self.removed[:limit]]
        extra = len(self) - len(lines)
        if extra > 0:
            lines.append(f"... 另有 {extra} 项变更")
        return "\n".join(lines)


class VolumeWatch:
    """Tracks the journal of one volume and resolves paths for new records."""

    def __init__(self, volume: Volume, directory_map: dict[int, str], journal: JournalInfo) -> None:
        self.volume = volume
        self.letter = volume.drive
        self.directory_map = directory_map
        self.journal = journal
        self.next_usn = journal.next_usn
        self.skipped = 0

    @classmethod
    def bootstrap(cls, letter: str, progress=None) -> "VolumeWatch":
        volume = Volume(letter)
        volume.open()
        try:
            journal = volume.query_journal()
            _paths, _attrs, directory_map = scan_mft_volume(letter, progress, dirs_only=True)
        except Exception:
            volume.close()
            raise
        return cls(volume, directory_map, journal)

    def close(self) -> None:
        self.volume.close()

    # ---------------------------------------------------------------------
    def _path_of(self, parent_frn: int, name: str) -> str | None:
        if parent_frn == ROOT_FILE_REFERENCE:
            if name in NTFS_METADATA:
                return None
            return f"{self.letter}:\\" + name
        base = self.directory_map.get(parent_frn)
        if base is None:
            return None
        return base + name

    def poll(self, timeout_ms: int = 0) -> ChangeSet:
        changes = ChangeSet(volumes={self.letter})
        next_usn, records = self.volume.read_journal(
            self.next_usn,
            self.journal.journal_id,
            reason_mask=WATCH_REASON_MASK,
            timeout_ms=timeout_ms,
        )
        self.next_usn = next_usn
        if not records:
            return changes

        for record in records:
            path = self._path_of(record.parent_frn, record.name)
            if path is None:
                self.skipped += 1
                changes.skipped += 1
                continue
            is_dir = record.is_dir
            if record.is_deleted:
                changes.removed.append(path)
                if is_dir:
                    self.directory_map.pop(record.frn, None)
            elif record.is_rename_old:
                changes.removed.append(path)
                if is_dir:
                    self.directory_map.pop(record.frn, None)
            elif record.is_created or record.is_rename_new:
                changes.added.append((path, record.attributes & 0xFFFF, -1, -1))
                if is_dir:
                    self.directory_map[record.frn] = path + "\\"
        return changes


class JournalWatcher:
    """Watches several volumes and accumulates their changes."""

    def __init__(self, letters: list[str] | None = None) -> None:
        self.letters = letters or []
        self.trackers: list[VolumeWatch] = []
        self.warnings: list[str] = []

    def start(self, progress=None) -> "JournalWatcher":
        letters: list[str] = list(self.letters)
        if not letters:
            from .volumes import fixed_ntfs_volumes

            letters = [info.letter for info in fixed_ntfs_volumes()]
            self.letters = letters
        for letter in letters:
            try:
                tracker = VolumeWatch.bootstrap(letter, progress)
            except (UsnError, OSError) as exc:
                self.warnings.append(f"{letter}: {exc}")
                continue
            self.trackers.append(tracker)
        return self

    def close(self) -> None:
        for tracker in self.trackers:
            tracker.close()
        self.trackers.clear()

    def __enter__(self) -> "JournalWatcher":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def active(self) -> bool:
        return bool(self.trackers)

    def poll(self, timeout_ms: int = 0) -> ChangeSet:
        total = ChangeSet()
        resets: list[str] = []
        for tracker in self.trackers:
            try:
                changes = tracker.poll(timeout_ms)
            except JournalReset as exc:
                resets.append(str(exc))
                continue
            except UsnError as exc:
                if exc.winerror == ERROR_JOURNAL_ENTRY_DELETED:
                    resets.append(f"{tracker.letter}: 变更日志已被重置")
                else:
                    self.warnings.append(f"{tracker.letter}: {exc}")
                continue
            total.added.extend(changes.added)
            total.removed.extend(changes.removed)
            total.volumes |= changes.volumes
            total.skipped += changes.skipped
        if resets:
            raise JournalReset("; ".join(resets))
        return total

    def watch(
        self,
        on_change,
        stop_event: threading.Event | None = None,
        interval: float = 0.5,
    ) -> None:
        """Blocking loop; *on_change* receives ``(ChangeSet, error)``."""
        stop_event = stop_event or threading.Event()
        timeout_ms = max(50, int(interval * 1000))
        while not stop_event.is_set():
            try:
                changes = self.poll(timeout_ms=timeout_ms)
            except JournalReset as exc:
                on_change(ChangeSet(), str(exc))
                return
            except UsnError as exc:
                on_change(ChangeSet(), str(exc))
                return
            if changes:
                on_change(changes, None)
            elif not self.trackers:
                time.sleep(interval)
