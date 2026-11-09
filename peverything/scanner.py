"""Volume scanners: the fast MFT path and the portable directory walk."""

from __future__ import annotations

import fnmatch
import os
import threading
import time
from array import array
from dataclasses import dataclass, field
from typing import Callable, Sequence

from . import util
from .index import PathTable
from .usn import (
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_REPARSE_POINT,
    ROOT_FILE_REFERENCE,
    UsnError,
    Volume,
)
from .volumes import fixed_ntfs_volumes, list_volumes

ProgressFn = Callable[[str, int], None]

#: Files that only exist to describe the NTFS volume itself; Everything hides
#: them too, and they would just pollute the index.
NTFS_METADATA = {
    "$MFT",
    "$MFTMirr",
    "$LogFile",
    "$Volume",
    "$AttrDef",
    "$Bitmap",
    "$Boot",
    "$BadClus",
    "$Secure",
    "$UpCase",
    "$Extend",
    "$Quota",
    "$ObjId",
    "$Reparse",
}


@dataclass
class ScanResult:
    paths: list[str] = field(default_factory=list)
    attrs: array = field(default_factory=lambda: array("I"))
    sizes: array = field(default_factory=lambda: array("q"))
    mtimes: array = field(default_factory=lambda: array("q"))
    volumes: list[str] = field(default_factory=list)
    source: str = "walk"
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)
    stats_available: bool = False

    def __len__(self) -> int:
        return len(self.paths)

    def to_table(self, extra_meta: dict | None = None) -> PathTable:
        meta = {
            "volumes": self.volumes,
            "source": self.source,
            "created": time.time(),
            "scan_seconds": round(self.seconds, 3),
            "has_stat": self.stats_available,
            "app_version": util.version_string(),
        }
        if extra_meta:
            meta.update(extra_meta)
        return PathTable.build(self.paths, self.attrs, self.sizes, self.mtimes, meta)


# --------------------------------------------------------------------- MFT ---
def build_paths(
    names: Sequence[str],
    file_refs: Sequence[int],
    parents: Sequence[int],
    attrs: Sequence[int],
    root: str,
    dirs_only: bool = False,
) -> tuple[list[str], array, dict[int, str]]:
    """Rebuild full paths from flat MFT records (the pure part of an MFT scan).

    ``names[i]`` belongs to file reference ``file_refs[i]`` whose parent is
    ``parents[i]``. Every directory is resolved once and cached for its whole
    subtree, which keeps the walk linear instead of re-walking every ancestor.
    Records whose parent is not a known directory (deleted, or filtered out)
    are dropped.
    """
    count = len(names)
    refs = list(file_refs)
    dir_cache: dict[int, str] = {ROOT_FILE_REFERENCE: root}
    # Only directories ever need to be looked up as a parent, so the index map
    # stays small even on volumes with millions of files.
    by_frn: dict[int, int] = {
        refs[index]: index for index in range(count) if attrs[index] & FILE_ATTRIBUTE_DIRECTORY
    }

    def resolve(frn: int) -> str | None:
        trail: list[int] = []
        current = frn
        for _ in range(512):
            cached = dir_cache.get(current)
            if cached is not None:
                base = cached
                for index in reversed(trail):
                    base = base + names[index] + "\\"
                    dir_cache[refs[index]] = base
                return base
            index = by_frn.get(current)
            if index is None:
                return None
            if not attrs[index] & FILE_ATTRIBUTE_DIRECTORY:
                return None
            trail.append(index)
            current = parents[index]
        return None

    paths: list[str] = []
    out_attrs = array("I")
    append_path = paths.append
    append_attr = out_attrs.append
    for index in range(count):
        if refs[index] == ROOT_FILE_REFERENCE:
            continue
        parent = parents[index]
        if parent == 0:
            continue
        base = dir_cache.get(parent)
        if base is None:
            base = resolve(parent)
            if base is None:
                continue
        path = base + names[index]
        is_dir = bool(attrs[index] & FILE_ATTRIBUTE_DIRECTORY)
        if is_dir or not dirs_only:
            append_path(path)
            append_attr(attrs[index])
        if is_dir:
            dir_cache[refs[index]] = path + "\\"
    return paths, out_attrs, dir_cache


def scan_mft_volume(
    letter: str,
    progress: ProgressFn | None = None,
    dirs_only: bool = False,
) -> tuple[list[str], array, dict[int, str]]:
    """Read every file name of an NTFS volume straight from the MFT.

    Returns ``(paths, attributes, directory_map)``. Sizes and timestamps are not
    part of the MFT enumeration, so the caller marks them unknown. The
    directory map (file reference -> path) is what the journal watcher needs to
    turn USN records back into full paths; ``dirs_only`` skips file records
    entirely and is much lighter when only that map is wanted.
    """
    volume = Volume(letter)
    root = volume.root
    names: list[str] = []
    file_refs: list[int] = []
    parents: list[int] = []
    attrs = array("I")

    report = progress or (lambda message, count: None)
    with volume:
        for record in volume.iter_mft_records():
            name = record.name
            if not name or name == ".":
                continue
            if dirs_only and not record.attributes & FILE_ATTRIBUTE_DIRECTORY:
                continue
            if record.parent_frn == ROOT_FILE_REFERENCE and name in NTFS_METADATA:
                continue
            names.append(name)
            file_refs.append(record.frn)
            parents.append(record.parent_frn)
            attrs.append(record.attributes & 0xFFFF)
            if len(names) % 20000 == 0:
                report(f"读取 MFT {letter}: {len(names):,}", len(names))

    report(f"重建路径 {letter}: {len(names):,}", len(names))
    paths, out_attrs, dir_cache = build_paths(names, file_refs, parents, attrs, root, dirs_only)
    report(f"{letter}: 完成，共 {len(names):,} 个对象", len(paths))
    del names, file_refs, parents, attrs
    return paths, out_attrs, dir_cache


# -------------------------------------------------------------------- walk ---
class _WalkPool:
    """Directory walker using a small pool of threads sharing one work stack."""

    def __init__(
        self,
        roots: Sequence[str],
        progress: ProgressFn | None,
        exclude: Sequence[str] = (),
        workers: int = 8,
    ) -> None:
        self.roots = list(roots)
        self.progress = progress or (lambda message, count: None)
        self.exclude = [pattern.lower() for pattern in exclude]
        self.workers = max(1, workers)
        self.paths: list[str] = []
        self.attrs = array("I")
        self.sizes = array("q")
        self.mtimes = array("q")
        self.errors: list[str] = []
        self._stack: list[str] = []
        self._pending = 0
        self._finished = False
        self._cond = threading.Condition()
        self._lock = threading.Lock()
        self._count = 0
        self._last_report = 0.0

    # -- helpers -----------------------------------------------------------
    def _excluded(self, path: str) -> bool:
        if not self.exclude:
            return False
        lowered = path.lower()
        return any(fnmatch.fnmatch(lowered, pattern) for pattern in self.exclude)

    def _push(self, directory: str) -> None:
        with self._cond:
            self._stack.append(directory)
            self._pending += 1
            self._cond.notify()

    def _done_one(self) -> None:
        with self._cond:
            self._pending -= 1
            if self._pending <= 0:
                self._finished = True
                self._cond.notify_all()

    def _report(self, force: bool = False) -> None:
        now = time.perf_counter()
        if not force and now - self._last_report < 0.2:
            return
        self._last_report = now
        with self._lock:
            count = len(self.paths)
        self.progress(f"扫描目录: {count:,} 个对象", count)

    def _scan_dir(self, directory: str) -> None:
        local_paths: list[str] = []
        local_attrs: list[int] = []
        local_sizes: list[int] = []
        local_mtimes: list[int] = []
        subdirs: list[str] = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                        stat = entry.stat(follow_symlinks=False)
                        attributes = int(getattr(stat, "st_file_attributes", 0))
                        if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                            continue
                        path = entry.path
                        local_paths.append(path)
                        local_attrs.append(attributes & 0xFFFF)
                        local_sizes.append(-1 if is_dir else int(stat.st_size))
                        local_mtimes.append(int(stat.st_mtime))
                        if is_dir and not self._excluded(path):
                            subdirs.append(path)
                    except OSError:
                        continue
        except OSError as exc:
            with self._lock:
                if len(self.errors) < 50:
                    self.errors.append(f"{directory}: {exc.strerror or exc}")
            return
        finally:
            if local_paths:
                with self._lock:
                    self.paths.extend(local_paths)
                    self.attrs.extend(local_attrs)
                    self.sizes.extend(local_sizes)
                    self.mtimes.extend(local_mtimes)
            for subdir in subdirs:
                self._push(subdir)
            self._report()

    def _worker(self) -> None:
        while True:
            with self._cond:
                while not self._stack and not self._finished:
                    self._cond.wait()
                if self._finished:
                    return
                directory = self._stack.pop()
            self._scan_dir(directory)
            self._done_one()

    def run(self) -> None:
        for root in self.roots:
            if os.path.isdir(root):
                self._push(root)
        with self._cond:
            if self._pending == 0:
                return
        threads = [
            threading.Thread(target=self._worker, name=f"pev-walk-{i}", daemon=True)
            for i in range(self.workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self._report(force=True)


def scan_walk(
    roots: Sequence[str],
    progress: ProgressFn | None = None,
    exclude: Sequence[str] = (),
    workers: int = 8,
) -> ScanResult:
    pool = _WalkPool(roots, progress, exclude, workers)
    started = time.perf_counter()
    pool.run()
    return ScanResult(
        paths=pool.paths,
        attrs=pool.attrs,
        sizes=pool.sizes,
        mtimes=pool.mtimes,
        volumes=sorted({root[0].upper() for root in roots if len(root) > 1}),
        source="walk",
        seconds=time.perf_counter() - started,
        errors=pool.errors,
        stats_available=True,
    )


# ------------------------------------------------------------------ driver ---
def default_roots(include_removable: bool = False) -> list[str]:
    return [info.root for info in list_volumes(include_removable=include_removable)]


def scan(
    volumes: Sequence[str] | None = None,
    directories: Sequence[str] | None = None,
    method: str = "auto",
    exclude: Sequence[str] = (),
    progress: ProgressFn | None = None,
    workers: int = 8,
) -> ScanResult:
    """Build a scan result for the requested targets.

    ``method``: ``mft`` reads the MFT (needs admin, NTFS), ``walk`` uses
    ``os.scandir`` (works everywhere), ``auto`` tries MFT and falls back.
    """
    report = progress or (lambda message, count: None)
    started = time.perf_counter()

    if directories:
        result = scan_walk(directories, progress, exclude, workers)
        result.seconds = time.perf_counter() - started
        return result

    if volumes:
        letters = [letter.strip().rstrip("\\").rstrip(":")[0].upper() for letter in volumes if letter.strip()]
        infos = list_volumes(include_removable=True)
    else:
        infos = list_volumes()
        letters = [info.letter for info in infos if info.is_fixed]
    by_letter = {info.letter: info for info in infos}
    if not letters:
        letters = [info.letter for info in fixed_ntfs_volumes()]

    use_mft = method == "mft" or (method == "auto" and util.is_admin())
    if method == "mft" and not util.is_admin():
        raise UsnError("MFT 直读需要管理员权限，请在管理员命令行中运行，或使用 --method walk")

    paths: list[str] = []
    attrs = array("I")
    sizes = array("q")
    mtimes = array("q")
    errors: list[str] = []
    volumes_done: list[str] = []
    saw_walk = False
    saw_mft = False

    for letter in letters:
        info = by_letter.get(letter)
        if info is not None and info.is_ntfs and use_mft:
            report(f"读取 {letter}: 的 MFT ...", len(paths))
            try:
                volume_paths, volume_attrs, _dirs = scan_mft_volume(letter, progress)
            except Exception as exc:  # noqa: BLE001 - degrade to the walk in auto mode
                if method == "mft" or isinstance(exc, MemoryError):
                    raise
                errors.append(f"{letter}: {exc}")
                report(f"{letter}: MFT 直读失败，改用目录遍历（{exc}）", len(paths))
            else:
                paths.extend(volume_paths)
                attrs.extend(volume_attrs)
                sizes.extend(array("q", [-1]) * len(volume_paths))
                mtimes.extend(array("q", [-1]) * len(volume_paths))
                volumes_done.append(letter)
                saw_mft = True
                report(f"{letter}: 完成，MFT 共 {len(volume_paths):,} 个对象", len(paths))
                continue
        root = (info.root if info is not None else f"{letter}:\\")
        report(f"遍历 {root} ...", len(paths))
        walked = scan_walk([root], progress, exclude, workers)
        paths.extend(walked.paths)
        attrs.extend(walked.attrs)
        sizes.extend(walked.sizes)
        mtimes.extend(walked.mtimes)
        volumes_done.append(letter)
        errors.extend(walked.errors[:10])
        saw_walk = True
        report(f"{root}: 完成，共 {len(walked.paths):,} 个对象", len(paths))

    if saw_mft and not saw_walk:
        source = "mft"
    elif saw_mft:
        source = "mixed"
    else:
        source = "walk"

    return ScanResult(
        paths=paths,
        attrs=attrs,
        sizes=sizes,
        mtimes=mtimes,
        volumes=sorted(volumes_done),
        source=source,
        seconds=time.perf_counter() - started,
        errors=errors,
        stats_available=saw_walk,
    )


def scan_directory_tree(root: str, progress: ProgressFn | None = None) -> ScanResult:
    return scan_walk([root], progress)
