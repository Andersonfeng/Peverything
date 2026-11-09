"""The index itself: a compact, memory mapped friendly path store.

Design
------
Every file name is stored once, as UTF-8, in one big ``bytes`` blob separated by
``\\n``.  A parallel blob holds an ASCII-lowercased copy (``bytes.lower()`` only
touches A-Z, so both blobs have *identical* byte offsets - one offset array is
enough).  Searching is then ``bytes.find`` / ``re.finditer`` over a contiguous
buffer, which runs at memcpy speed, and the hit offset is mapped back to a file
with ``bisect`` over the offset array.  For a million paths this is a few tens
of milliseconds per query instead of a full directory walk.

On disk the same layout is written verbatim, so loading an index is a single
sequential read.
"""

from __future__ import annotations

import bisect
import json
import os
import re
import struct
import threading
import time
import zlib
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from .usn import FILE_ATTRIBUTE_DIRECTORY

MAGIC = b"PEV1"
FORMAT_VERSION = 2
NEWLINE = b"\n"


class IndexFormatError(ValueError):
    """The file is not a Peverything index, or a newer version than we know."""


@dataclass(frozen=True, slots=True)
class FileRow:
    path: str
    size: int = -1
    mtime: int = -1
    attrs: int = 0

    @property
    def is_dir(self) -> bool:
        return bool(self.attrs & FILE_ATTRIBUTE_DIRECTORY)

    @property
    def name(self) -> str:
        cut = self.path.rfind("\\")
        return self.path[cut + 1 :] if cut >= 0 else self.path

    @property
    def parent(self) -> str:
        cut = self.path.rfind("\\")
        return self.path[:cut] if cut > 0 else self.path

    @property
    def ext(self) -> str:
        name = self.name
        dot = name.rfind(".")
        return name[dot + 1 :].lower() if dot > 0 else ""


def _zeros(typecode: str, count: int) -> array:
    return array(typecode, [0]) * count if count else array(typecode)


def _minus_ones(typecode: str, count: int) -> array:
    return array(typecode, [-1]) * count if count else array(typecode)


class PathTable:
    """Immutable, sorted, searchable collection of file paths."""

    __slots__ = ("data", "low", "offsets", "attrs", "sizes", "mtimes", "meta")

    def __init__(
        self,
        data: bytes,
        low: bytes,
        offsets: array,
        attrs: array,
        sizes: array,
        mtimes: array,
        meta: dict | None = None,
    ) -> None:
        if len(data) != len(low):
            raise IndexFormatError("data/low length mismatch")
        if len(offsets) != len(attrs) + 1:
            raise IndexFormatError("offset table does not match path count")
        if len(sizes) != len(attrs) or len(mtimes) != len(attrs):
            raise IndexFormatError("attribute tables do not match path count")
        self.data = data
        self.low = low
        self.offsets = offsets
        self.attrs = attrs
        self.sizes = sizes
        self.mtimes = mtimes
        self.meta = meta or {}

    # ------------------------------------------------------------- build ---
    @classmethod
    def build(
        cls,
        paths: Sequence[str],
        attrs: Sequence[int] | None = None,
        sizes: Sequence[int] | None = None,
        mtimes: Sequence[int] | None = None,
        meta: dict | None = None,
    ) -> "PathTable":
        count = len(paths)
        order = sorted(range(count), key=paths.__getitem__)
        offsets = array("I", [0])
        data_parts: list[bytes] = []
        low_parts: list[bytes] = []
        append_off = offsets.append
        position = 0
        for index in order:
            encoded = paths[index].encode("utf-8", "surrogatepass")
            data_parts.append(encoded)
            low_parts.append(encoded.lower())
            position += len(encoded) + 1
            append_off(position)
        if position > 0xFFFFFFFF:
            raise IndexFormatError("索引超过 4 GiB 路径数据，请拆分索引")
        data = NEWLINE.join(data_parts)
        low = NEWLINE.join(low_parts)
        del data_parts, low_parts

        if attrs is None:
            attr_array = _zeros("I", count)
        else:
            attr_array = array("I", (int(attrs[i]) for i in order))
        if sizes is None:
            size_array = _minus_ones("q", count)
        else:
            size_array = array("q", (int(sizes[i]) for i in order))
        if mtimes is None:
            mtime_array = _minus_ones("q", count)
        else:
            mtime_array = array("q", (int(mtimes[i]) for i in order))

        resolved_meta = dict(meta or {})
        resolved_meta.setdefault("version", FORMAT_VERSION)
        resolved_meta.setdefault("created", time.time())
        resolved_meta["count"] = count
        resolved_meta["data_len"] = len(data)
        resolved_meta["low_len"] = len(low)
        resolved_meta.setdefault("has_stat", any(v >= 0 for v in size_array))
        return cls(data, low, offsets, attr_array, size_array, mtime_array, resolved_meta)

    @classmethod
    def empty(cls, meta: dict | None = None) -> "PathTable":
        return cls.build([], meta=meta)

    # ---------------------------------------------------------- accessors ---
    def __len__(self) -> int:
        return len(self.attrs)

    @property
    def count(self) -> int:
        return len(self.attrs)

    def path_at(self, index: int) -> str:
        offsets = self.offsets
        start = offsets[index]
        end = offsets[index + 1] - 1
        return self.data[start:end].decode("utf-8", "surrogatepass")

    def raw_at(self, index: int, case: bool = True) -> bytes:
        buffer = self.data if case else self.low
        start = self.offsets[index]
        return buffer[start : self.offsets[index + 1] - 1]

    def row_at(self, index: int) -> FileRow:
        return FileRow(self.path_at(index), self.sizes[index], self.mtimes[index], self.attrs[index])

    def row_bytes(self, index: int) -> tuple[bytes, bytes]:
        offsets = self.offsets
        start = offsets[index]
        end = offsets[index + 1] - 1
        return self.data[start:end], self.low[start:end]

    def iter_rows(self) -> Iterator[FileRow]:
        for index in range(len(self.attrs)):
            yield self.row_at(index)

    def iter_paths(self) -> Iterator[str]:
        for index in range(len(self.attrs)):
            yield self.path_at(index)

    def index_at_offset(self, position: int) -> int:
        return bisect.bisect_right(self.offsets, position) - 1

    # ------------------------------------------------------------ search ---
    def find_substr(self, needle: bytes, case: bool = False) -> Iterator[int]:
        """Yield indices whose path contains *needle* (case per flag)."""
        if not needle:
            yield from range(len(self.attrs))
            return
        if not case:
            needle = needle.lower()
        buffer = self.data if case else self.low
        offsets = self.offsets
        find = buffer.find
        start = 0
        while True:
            position = find(needle, start)
            if position < 0:
                return
            index = bisect.bisect_right(offsets, position) - 1
            yield index
            start = offsets[index + 1]

    def find_regex(self, pattern: "re.Pattern[bytes]", case: bool = False) -> Iterator[int]:
        """Yield indices whose path matches *pattern*.

        The pattern is matched against the whole blob with MULTILINE, so ``^`` and
        ``$`` behave per path; a match that would swallow the separator is
        discarded, which keeps the semantics per-file.
        """
        buffer = self.data if case else self.low
        offsets = self.offsets
        n = len(offsets) - 1
        last = -1
        for match in pattern.finditer(buffer):
            text = match.group(0)
            if NEWLINE in text:
                continue
            position = match.start()
            index = bisect.bisect_right(offsets, position) - 1
            if index <= last or index >= n:
                continue
            last = index
            yield index

    def find_glob(self, glob: bytes, case: bool = False) -> Iterator[int]:
        return self.find_regex(glob_to_regex(glob, case_sensitive=case), case=case)

    # -------------------------------------------------------------- io ----
    def save(self, path: str | os.PathLike[str], compress: bool = True) -> Path:
        """Write the table.

        Only the original paths are stored: the lowercased search copy is
        rebuilt on load with ``bytes.lower()``, which is byte for byte the same
        buffer and halves the payload.
        """
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        meta = dict(self.meta)
        meta["compressed"] = compress
        meta["version"] = FORMAT_VERSION
        meta["count"] = len(self.attrs)
        meta["data_len"] = len(self.data)
        meta["low_len"] = 0
        payload = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        tmp = target.with_name(target.name + ".tmp")
        chunks = (
            self.data,
            self.offsets.tobytes(),
            self.attrs.tobytes(),
            self.sizes.tobytes(),
            self.mtimes.tobytes(),
        )
        with open(tmp, "wb") as handle:
            handle.write(MAGIC)
            handle.write(struct.pack("<I", len(payload)))
            handle.write(payload)
            if compress:
                compressor = zlib.compressobj(6)
                for chunk in chunks:
                    if chunk:
                        handle.write(compressor.compress(chunk))
                handle.write(compressor.flush())
            else:
                for chunk in chunks:
                    handle.write(chunk)
        os.replace(tmp, target)
        self.meta = meta
        return target

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "PathTable":
        target = Path(path)
        with open(target, "rb") as handle:
            magic = handle.read(4)
            if magic != MAGIC:
                raise IndexFormatError(f"{target} 不是 Peverything 索引文件")
            header_len = struct.unpack("<I", handle.read(4))[0]
            header = json.loads(handle.read(header_len).decode("utf-8"))
            version = int(header.get("version", 0))
            if version > FORMAT_VERSION:
                raise IndexFormatError(
                    f"索引版本 {version} 高于当前程序支持的 {FORMAT_VERSION}，请重新扫描"
                )
            if header.get("compressed"):
                try:
                    blob = zlib.decompress(handle.read())
                except zlib.error as exc:
                    raise IndexFormatError(f"索引文件损坏：{exc}") from exc
            else:
                blob = handle.read()

        count = int(header["count"])
        data_len = int(header["data_len"])
        low_len = int(header.get("low_len") or 0)
        expected = data_len + low_len + (count + 1) * 4 + count * (4 + 8 + 8)
        if len(blob) != expected:
            raise IndexFormatError(
                f"索引文件被截断（期望 {expected} 字节，实际 {len(blob)} 字节）"
            )

        def take(typecode: str, items: int, offset: int) -> array:
            size = items * array(typecode).itemsize
            chunk = blob[offset : offset + size]
            result = array(typecode)
            result.frombytes(chunk)
            return result

        # The fixed width tables trail the path blob, so they can be decoded
        # before the (large) path buffer is copied.
        base = data_len + low_len
        offsets_at = base
        attrs_at = offsets_at + (count + 1) * 4
        sizes_at = attrs_at + count * 4
        mtimes_at = sizes_at + count * 8
        offsets = take("I", count + 1, offsets_at)
        attrs = take("I", count, attrs_at)
        sizes = take("q", count, sizes_at)
        mtimes = take("q", count, mtimes_at)
        if low_len:
            data = blob[:data_len]
            low = blob[data_len : data_len + low_len]
        else:
            data = blob[:data_len]
            low = b""
        del blob
        if not low:
            low = data.lower()
        return cls(data, low, offsets, attrs, sizes, mtimes, header)


def glob_to_regex(glob: str | bytes, case_sensitive: bool = False) -> "re.Pattern[bytes]":
    """Translate ``*``/``?`` glob syntax into a bytes regex for one path."""
    raw = glob.encode("utf-8") if isinstance(glob, str) else glob
    out = bytearray(b"(?m)" if case_sensitive else b"(?mi)")
    index = 0
    length = len(raw)
    while index < length:
        char = raw[index : index + 1]
        if char == b"*":
            out += b"[^\\n]*"
        elif char == b"?":
            out += b"[^\\n]"
        else:
            out += re.escape(char)
        index += 1
    return re.compile(bytes(out))


class SearchIndex:
    """A PathTable plus a small mutable delta layer.

    Live USN updates are cheap: new or renamed files land in ``delta`` (a tiny
    PathTable rebuilt in milliseconds) and deletions are held in a set that is
    filtered out at query time.  :meth:`compact` folds everything back into one
    immutable table, which is what the scanner writes to disk.
    """

    def __init__(
        self,
        base: PathTable,
        pending: dict[str, tuple[int, int, int]] | None = None,
        removed: set[str] | None = None,
    ) -> None:
        self.base = base
        self._pending: dict[str, tuple[int, int, int]] = dict(pending or {})
        self._removed: set[str] = set(removed or ())
        self._delta: PathTable | None = None
        self._delta_dirty = True
        # Searches run in a worker thread while the journal watcher applies
        # changes, so every mutation and every read is serialised.
        self._lock = threading.RLock()

    # ---------------------------------------------------------- accessors ---
    @property
    def meta(self) -> dict:
        return self.base.meta

    @property
    def count(self) -> int:
        with self._lock:
            return len(self.base) + len(self._pending)

    @property
    def removed_count(self) -> int:
        with self._lock:
            return len(self._removed)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def _delta_table(self) -> PathTable | None:
        if not self._pending:
            self._delta = None
            return None
        if self._delta is None or self._delta_dirty:
            paths = list(self._pending.keys())
            attrs = [value[0] for value in self._pending.values()]
            sizes = [value[1] for value in self._pending.values()]
            mtimes = [value[2] for value in self._pending.values()]
            self._delta = PathTable.build(paths, attrs, sizes, mtimes, {"count": len(paths)})
            self._delta_dirty = False
        return self._delta

    # ------------------------------------------------------------ updates ---
    def apply(self, adds: Iterable[tuple[str, int, int, int]] = (), removes: Iterable[str] = ()) -> int:
        """Queue additions ``(path, attrs, size, mtime)`` and removals."""
        changed = 0
        with self._lock:
            for path, attrs, size, mtime in adds:
                if not path:
                    continue
                self._removed.discard(path)
                entry = (int(attrs), int(size), int(mtime))
                if self._pending.get(path) != entry:
                    self._pending[path] = entry
                    changed += 1
            for path in removes:
                if self._pending.pop(path, None) is not None:
                    changed += 1
                if path not in self._removed:
                    self._removed.add(path)
                    changed += 1
            if changed:
                self._delta_dirty = True
        return changed

    def compact(self) -> None:
        """Merge delta into the base table (expensive: rebuilds the blob)."""
        with self._lock:
            self._compact_locked()

    def _compact_locked(self) -> None:
        if not self._pending and not self._removed:
            return
        paths: list[str] = []
        attrs: list[int] = []
        sizes: list[int] = []
        mtimes: list[int] = []
        removed = self._removed
        base = self.base
        for index in range(len(base)):
            path = base.path_at(index)
            if path in removed:
                continue
            paths.append(path)
            attrs.append(base.attrs[index])
            sizes.append(base.sizes[index])
            mtimes.append(base.mtimes[index])
        for path, (attr, size, mtime) in self._pending.items():
            if path in removed:
                continue
            paths.append(path)
            attrs.append(attr)
            sizes.append(size)
            mtimes.append(mtime)
        meta = dict(base.meta)
        meta["count"] = len(paths)
        self.base = PathTable.build(paths, attrs, sizes, mtimes, meta)
        self._pending.clear()
        self._removed.clear()
        self._delta = None
        self._delta_dirty = True

    # ------------------------------------------------------------- query ---
    def search(self, parsed, limit: int | None = None) -> list[FileRow]:
        from .query import iter_query  # local import: avoids a cycle

        with self._lock:
            results: list[FileRow] = []
            removed = self._removed
            table = self.base
            for index in iter_query(table, parsed):
                if removed and table.path_at(index) in removed:
                    continue
                results.append(table.row_at(index))
                if limit is not None and len(results) >= limit:
                    break
            if limit is None or len(results) < limit:
                delta = self._delta_table()
                if delta is not None:
                    for index in iter_query(delta, parsed):
                        row = delta.row_at(index)
                        if removed and row.path in removed:
                            continue
                        results.append(row)
                        if limit is not None and len(results) >= limit:
                            break
            if self._delta is not None or removed:
                results.sort(key=lambda row: row.path)
            return results

    def save(self, path: str | os.PathLike[str]) -> Path:
        with self._lock:
            if self._pending or self._removed:
                self._compact_locked()
            return self.base.save(path)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "SearchIndex":
        return cls(PathTable.load(path))
