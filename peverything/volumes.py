"""Enumerate the mounted volumes on this machine."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

_DRIVE_KIND = {
    DRIVE_REMOVABLE: "可移动",
    DRIVE_FIXED: "本地磁盘",
    DRIVE_REMOTE: "网络",
    DRIVE_CDROM: "光驱",
    DRIVE_RAMDISK: "内存盘",
}

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None  # type: ignore[attr-defined]

if _kernel32 is not None:  # pragma: no cover - Windows only
    _kernel32.GetLogicalDriveStringsW.argtypes = [wintypes.DWORD, wintypes.LPWSTR]
    _kernel32.GetLogicalDriveStringsW.restype = wintypes.DWORD
    _kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetDriveTypeW.restype = wintypes.UINT
    _kernel32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    _kernel32.GetVolumeInformationW.restype = wintypes.BOOL


@dataclass(frozen=True)
class VolumeInfo:
    letter: str
    root: str
    kind: str
    filesystem: str
    label: str

    @property
    def is_ntfs(self) -> bool:
        return self.filesystem.upper() == "NTFS"

    @property
    def is_fixed(self) -> bool:
        return self.kind == "本地磁盘"

    def __str__(self) -> str:  # pragma: no cover - display helper
        label = f" {self.label}" if self.label else ""
        return f"{self.letter}: [{self.filesystem or '?'}] {self.kind}{label}"


def list_volumes(include_removable: bool = False, include_network: bool = False) -> list[VolumeInfo]:
    """Return the ready volumes, fixed disks first."""
    if _kernel32 is None:
        return []
    length = _kernel32.GetLogicalDriveStringsW(0, None)
    if not length:
        return []
    buffer = ctypes.create_unicode_buffer(length + 1)
    _kernel32.GetLogicalDriveStringsW(length + 1, buffer)
    raw = buffer[:length]
    result: list[VolumeInfo] = []
    for entry in raw.split("\x00"):
        entry = entry.strip()
        if len(entry) < 2 or entry[1] != ":":
            continue
        root = entry if entry.endswith("\\") else entry + "\\"
        drive_type = _kernel32.GetDriveTypeW(root)
        if drive_type == DRIVE_CDROM:
            continue
        if drive_type == DRIVE_REMOTE and not include_network:
            continue
        if drive_type == DRIVE_REMOVABLE and not include_removable:
            continue
        if drive_type not in _DRIVE_KIND:
            continue
        fs_name = ctypes.create_unicode_buffer(256)
        vol_name = ctypes.create_unicode_buffer(256)
        serial = wintypes.DWORD(0)
        max_len = wintypes.DWORD(0)
        flags = wintypes.DWORD(0)
        ok = _kernel32.GetVolumeInformationW(
            root,
            vol_name,
            256,
            ctypes.byref(serial),
            ctypes.byref(max_len),
            ctypes.byref(flags),
            fs_name,
            256,
        )
        result.append(
            VolumeInfo(
                letter=root[0].upper(),
                root=root,
                kind=_DRIVE_KIND[drive_type],
                filesystem=fs_name.value if ok else "",
                label=vol_name.value if ok else "",
            )
        )
    result.sort(key=lambda v: (v.letter != "C", v.letter))
    return result


def fixed_ntfs_volumes() -> list[VolumeInfo]:
    return [v for v in list_volumes() if v.is_fixed and v.is_ntfs]


def all_drive_letters() -> list[str]:
    return [v.letter for v in list_volumes(include_removable=True)]
