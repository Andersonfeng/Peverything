"""Low level NTFS access through DeviceIoControl.

Everything (the original program) does not walk directories: it asks NTFS for
the Master File Table, which already contains every file name on the volume,
and rebuilds the directory tree from the parent references.  That is what this
module exposes:

* :meth:`Volume.iter_mft_records` - FSCTL_ENUM_USN_DATA, the whole MFT.
* :meth:`Volume.read_journal`     - FSCTL_READ_USN_JOURNAL, incremental changes.
* :meth:`Volume.query_journal`    - FSCTL_QUERY_USN_JOURNAL, journal state.

All of the above need an elevated (administrator) process; the callers fall
back to a directory walk when this raises :class:`UsnError`.
"""

from __future__ import annotations

import ctypes
import struct
from ctypes import wintypes
from dataclasses import dataclass
from typing import Iterator

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400

FSCTL_ENUM_USN_DATA = 0x000900B3
FSCTL_QUERY_USN_JOURNAL = 0x000900F4
FSCTL_READ_USN_JOURNAL = 0x000900BB

ERROR_HANDLE_EOF = 38
ERROR_INVALID_PARAMETER = 87
ERROR_ACCESS_DENIED = 5
ERROR_JOURNAL_NOT_ACTIVE = 1179
ERROR_JOURNAL_DELETE_IN_PROGRESS = 1178
ERROR_JOURNAL_ENTRY_DELETED = 1181

ROOT_FILE_REFERENCE = 5

#: USN_RECORD_V2: 60 byte fixed header, UTF-16 file name right behind it.
USN_RECORD = struct.Struct("<IHHQQqqIIIIHH")
USN_RECORD_SIZE = USN_RECORD.size  # 60

#: USN_JOURNAL_DATA_V0, 56 bytes.
JOURNAL_DATA = struct.Struct("<QQQQQQQ")

#: MFT_ENUM_DATA_V0, 24 bytes.
MFT_ENUM_DATA = struct.Struct("<QQQ")

#: READ_USN_JOURNAL_DATA_V0, 40 bytes.
READ_USN_JOURNAL_DATA = struct.Struct("<QIIQQQ")

# USN_REASON_* flags we care about.
REASON_DATA_OVERWRITE = 0x00000001
REASON_DATA_EXTEND = 0x00000002
REASON_DATA_TRUNCATION = 0x00000004
REASON_FILE_CREATE = 0x00000100
REASON_FILE_DELETE = 0x00000200
REASON_RENAME_OLD_NAME = 0x00001000
REASON_RENAME_NEW_NAME = 0x00002000
REASON_BASIC_INFO_CHANGE = 0x00008000
REASON_HARD_LINK_CHANGE = 0x00010000
REASON_CLOSE = 0x80000000
REASON_FILE_MODIFIED = (
    REASON_DATA_OVERWRITE | REASON_DATA_EXTEND | REASON_DATA_TRUNCATION
)

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None  # type: ignore[attr-defined]

if _kernel32 is not None:  # pragma: no cover - Windows only
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    _kernel32.DeviceIoControl.restype = wintypes.BOOL


class UsnError(OSError):
    """The volume cannot be read directly (not NTFS, or not elevated)."""

    def __init__(self, message: str, winerror: int = 0) -> None:
        super().__init__(message)
        self.winerror = winerror


@dataclass(frozen=True)
class JournalInfo:
    journal_id: int
    first_usn: int
    next_usn: int
    lowest_valid_usn: int
    max_usn: int
    maximum_size: int
    allocation_delta: int


@dataclass(frozen=True)
class UsnChange:
    """One parsed USN_RECORD_V2."""

    frn: int
    parent_frn: int
    usn: int
    timestamp: int
    reason: int
    attributes: int
    name: str

    @property
    def is_dir(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_DIRECTORY)

    @property
    def is_deleted(self) -> bool:
        return bool(self.reason & REASON_FILE_DELETE)

    @property
    def is_created(self) -> bool:
        return bool(self.reason & REASON_FILE_CREATE)

    @property
    def is_rename_old(self) -> bool:
        return bool(self.reason & REASON_RENAME_OLD_NAME)

    @property
    def is_rename_new(self) -> bool:
        return bool(self.reason & REASON_RENAME_NEW_NAME)


def parse_usn_records(buf: bytes, start: int = 0, end: int | None = None) -> Iterator[UsnChange]:
    """Decode USN_RECORD_V2 entries from *buf* between byte offsets."""
    if end is None:
        end = len(buf)
    offset = start
    unpack = USN_RECORD.unpack_from
    while offset + USN_RECORD_SIZE <= end:
        (
            record_length,
            major,
            _minor,
            frn,
            parent_frn,
            usn,
            timestamp,
            reason,
            _source,
            _security,
            attributes,
            name_length,
            name_offset,
        ) = unpack(buf, offset)
        if record_length < USN_RECORD_SIZE or offset + record_length > end:
            break
        if major != 2:
            # USN_RECORD_V3 carries 128 bit file ids (ReFS); skip gracefully.
            offset += record_length
            continue
        raw = buf[offset + name_offset : offset + name_offset + name_length]
        name = raw.decode("utf-16-le", "replace")
        yield UsnChange(
            frn=frn,
            parent_frn=parent_frn,
            usn=usn,
            timestamp=timestamp,
            reason=reason,
            attributes=attributes,
            name=name,
        )
        offset += record_length
        if record_length == 0:  # pragma: no cover - corrupt buffer guard
            break


class Volume:
    """A mounted NTFS volume that can be queried through its ``\\\\.\\X:`` handle."""

    def __init__(self, drive: str) -> None:
        drive = drive.strip().rstrip("\\").rstrip(":")
        if not drive:
            raise ValueError("empty drive letter")
        self.drive = drive[0].upper()
        self.root = f"{self.drive}:\\"
        self._handle: int | None = None

    # ------------------------------------------------------------- handle ---
    def open(self) -> None:
        if self._handle is not None:
            return
        if _kernel32 is None:
            raise UsnError("NTFS 直读仅支持 Windows")
        path = f"\\\\.\\{self.drive}:"
        handle = _kernel32.CreateFileW(
            path,
            GENERIC_READ,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            0,
            None,
        )
        if handle == INVALID_HANDLE_VALUE or handle is None:
            err = ctypes.get_last_error()
            if err == ERROR_ACCESS_DENIED:
                raise UsnError(
                    f"无法以管理员权限打开卷 {self.root}（错误 5）："
                    "MFT 直读需要管理员权限", err
                )
            raise UsnError(f"无法打开卷 {self.root}：Windows 错误 {err}", err)
        self._handle = handle

    def close(self) -> None:
        if self._handle is not None and _kernel32 is not None:
            _kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "Volume":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Volume({self.root!r})"

    # ------------------------------------------------------------ ioctls ---
    def _ioctl(self, code: int, in_buffer: bytes, out_size: int) -> bytes:
        if self._handle is None:
            raise UsnError("volume is not open")
        in_buf = ctypes.create_string_buffer(in_buffer, len(in_buffer))
        out_buf = ctypes.create_string_buffer(out_size)
        returned = wintypes.DWORD(0)
        ok = _kernel32.DeviceIoControl(  # type: ignore[union-attr]
            self._handle,
            code,
            ctypes.cast(in_buf, wintypes.LPVOID),
            len(in_buffer),
            ctypes.cast(out_buf, wintypes.LPVOID),
            out_size,
            ctypes.byref(returned),
            None,
        )
        if not ok:
            err = ctypes.get_last_error()
            raise UsnError(f"DeviceIoControl(0x{code:08X}) 失败：Windows 错误 {err}", err)
        return out_buf.raw[: returned.value]

    def query_journal(self) -> JournalInfo:
        self.open()
        data = self._ioctl(FSCTL_QUERY_USN_JOURNAL, b"", 56)
        if len(data) < JOURNAL_DATA.size:
            raise UsnError("FSCTL_QUERY_USN_JOURNAL 返回的数据不完整")
        values = JOURNAL_DATA.unpack_from(data, 0)
        return JournalInfo(*values)

    def iter_mft_records(self, buffer_size: int = 1 << 20) -> Iterator[UsnChange]:
        """Yield every file name record in the MFT of this volume."""
        self.open()
        journal = self.query_journal()
        start_frn = 0
        high_usn = journal.next_usn
        while True:
            request = MFT_ENUM_DATA.pack(start_frn, 0, high_usn)
            try:
                data = self._ioctl(FSCTL_ENUM_USN_DATA, request, buffer_size)
            except UsnError as exc:
                if exc.winerror in (ERROR_HANDLE_EOF, ERROR_JOURNAL_ENTRY_DELETED):
                    return
                raise
            if len(data) < 8:
                return
            next_frn = struct.unpack_from("<Q", data, 0)[0]
            yield from parse_usn_records(data, 8, len(data))
            if next_frn == 0 or next_frn == start_frn:
                return
            start_frn = next_frn

    def read_journal(
        self,
        start_usn: int,
        journal_id: int,
        *,
        reason_mask: int = 0xFFFFFFFF,
        timeout_ms: int = 0,
        max_bytes: int = 1 << 20,
    ) -> tuple[int, list[UsnChange]]:
        """Read journal entries from *start_usn*; returns (next_usn, records)."""
        self.open()
        bytes_to_wait = 1 if timeout_ms > 0 else 0
        request = READ_USN_JOURNAL_DATA.pack(
            start_usn, reason_mask, 0, timeout_ms * 10_000, bytes_to_wait, journal_id
        )
        try:
            data = self._ioctl(FSCTL_READ_USN_JOURNAL, request, max_bytes)
        except UsnError as exc:
            if exc.winerror in (ERROR_HANDLE_EOF, ERROR_JOURNAL_ENTRY_DELETED):
                return start_usn, []
            raise
        if len(data) < 8:
            return start_usn, []
        next_usn = struct.unpack_from("<Q", data, 0)[0]
        return next_usn, list(parse_usn_records(data, 8, len(data)))


def enum_mft(volume_letter: str) -> Iterator[UsnChange]:
    """Convenience wrapper: open, enumerate, close."""
    with Volume(volume_letter) as volume:
        yield from volume.iter_mft_records()
