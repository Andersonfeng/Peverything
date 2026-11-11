"""Small Windows helpers shared by the CLI, the GUI and the scanners."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Iterable

from . import APP_NAME, __version__

INDEX_ENV = "PEVERYTHING_INDEX"


# ---------------------------------------------------------------- platform ---
def is_windows() -> bool:
    return os.name == "nt"


def is_admin() -> bool:
    """True when the current process runs with an elevated token."""
    if not is_windows():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover - defensive
        return False


def enable_debug_privilege() -> bool:
    """Best effort: some volume operations succeed only with this privilege."""
    if not is_windows():
        return False
    try:
        import ctypes.wintypes as wt

        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008
        SE_PRIVILEGE_ENABLED = 0x00000002

        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]

        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", wt.DWORD)]

        class TOKEN_PRIVILEGES(ctypes.Structure):
            _fields_ = [("PrivilegeCount", wt.DWORD), ("Privileges", LUID_AND_ATTRIBUTES * 1)]

        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        token = wt.HANDLE()
        if not advapi.OpenProcessToken(
            kernel.GetCurrentProcess(), TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, ctypes.byref(token)
        ):
            return False
        try:
            luid = LUID()
            if not advapi.LookupPrivilegeValueW(None, "SeManageVolumePrivilege", ctypes.byref(luid)):
                return False
            tp = TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
            advapi.AdjustTokenPrivileges(token, False, ctypes.byref(tp), 0, None, None)
            return ctypes.get_last_error() == 0
        finally:
            kernel.CloseHandle(token)
    except Exception:  # pragma: no cover - defensive
        return False


def relaunch_as_admin(argv: list[str]) -> bool:
    """Re-start the current program through the UAC prompt. True if spawned."""
    if not is_windows():
        return False
    exe = sys.executable
    # Prefer pythonw.exe for the GUI so no console window flashes up.
    if getattr(sys, "frozen", False):
        params = subprocess.list2cmdline(argv)
        target = exe
    else:
        params = subprocess.list2cmdline(["-m", "peverything", *argv])
        target = exe
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", target, params, None, 1)
        return rc > 32
    except Exception:  # pragma: no cover - defensive
        return False


# ------------------------------------------------------------------- paths ---
def app_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / APP_NAME


def default_index_path() -> Path:
    override = os.environ.get(INDEX_ENV)
    if override:
        return Path(override).expanduser()
    return app_dir() / "index.pev"


def settings_path() -> Path:
    return app_dir() / "settings.json"


def ensure_parent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# ------------------------------------------------------------------ format ---
_SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def format_size(size: int | None) -> str:
    """Human readable byte count; '' when the size is unknown (-1)."""
    if size is None or size < 0:
        return ""
    value = float(size)
    for unit in _SIZE_UNITS:
        if value < 1024 or unit == _SIZE_UNITS[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{size} B"


def format_time(epoch: int | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    if epoch is None or epoch < 0:
        return ""
    try:
        return datetime.fromtimestamp(epoch).strftime(fmt)
    except (OverflowError, OSError, ValueError):
        return ""


def format_count(n: int) -> str:
    return f"{n:,}"


def format_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.2f} s"
    minutes, sec = divmod(seconds, 60)
    return f"{int(minutes)}m {sec:.0f}s"


# --------------------------------------------------------------- text width ---
def display_width(text: str) -> int:
    """Terminal display width, counting CJK characters as two cells."""
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text: str, width: int, align: str = "left") -> str:
    if display_width(text) >= width:
        return text
    fill = " " * (width - display_width(text))
    return fill + text if align == "right" else text + fill


def truncate(text: str, width: int, ellipsis: str = "...") -> str:
    """Cut text to a display width, keeping the tail (paths matter at the end)."""
    if display_width(text) <= width:
        return text
    budget = width - display_width(ellipsis)
    kept: list[str] = []
    used = 0
    for ch in reversed(text):
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > budget:
            break
        kept.append(ch)
        used += w
    return ellipsis + "".join(reversed(kept))


def human_rate(count: int, seconds: float) -> str:
    if seconds <= 0:
        return ""
    return f"{format_count(int(count / seconds))}/s"


# ------------------------------------------------------------- shell actions ---
def open_path(path: str | Path) -> None:
    os.startfile(str(path))  # type: ignore[attr-defined]


def reveal_path(path: str | Path) -> None:
    """Open the containing folder in Explorer with the item selected.

    Not done through ``explorer /select,<path>``: Popen quotes the whole
    argument, so a path containing spaces turns into
    ``explorer "/select,C:\\Program Files (x86)\\..."``, Explorer fails to parse
    the switch and silently opens Documents instead.  The shell API takes a
    PIDL, so quoting never enters the picture.
    """
    target = os.path.normpath(str(path))
    if os.path.exists(target) and _reveal_with_shell(target):
        return
    if os.path.isdir(target):
        subprocess.Popen(f'explorer /e,"{target}"')
    else:
        subprocess.Popen(f'explorer /select,"{target}"')


def _reveal_with_shell(target: str) -> bool:
    """SHOpenFolderAndSelectItems: opens the parent folder and selects *target*."""
    if not is_windows():
        return False
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        shell32.SHOpenFolderAndSelectItems.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
        shell32.ILFree.argtypes = [ctypes.c_void_p]
        shell32.ILFree.restype = None
        pidl = shell32.ILCreateFromPathW(target)
        if not pidl:
            return False
        try:
            return shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0
        finally:
            shell32.ILFree(pidl)
    except Exception:  # pragma: no cover - defensive
        return False


class _ShellExecuteInfoW(ctypes.Structure):
    """SHELLEXECUTEINFOW, 64-bit layout (112 bytes with padding)."""

    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("fMask", ctypes.c_ulong),
        ("hwnd", ctypes.c_void_p),
        ("lpVerb", ctypes.c_wchar_p),
        ("lpFile", ctypes.c_wchar_p),
        ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p),
        ("nShow", ctypes.c_int),
        ("hInstApp", ctypes.c_void_p),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", ctypes.c_wchar_p),
        ("hkeyClass", ctypes.c_void_p),
        ("dwHotKey", ctypes.c_ulong),
        ("hIcon", ctypes.c_void_p),
        ("hProcess", ctypes.c_void_p),
    ]


def set_window_icon(hwnd: int, icon_path: str | Path) -> bool:
    """Give a window the real .ico for both the title bar and the taskbar.

    Tk's ``wm iconbitmap`` only sets ``ICON_SMALL`` on some versions, and its
    ``default`` option applies to *future* toplevels, so the main window can be
    left with Tk's feather. Setting both sizes through WM_SETICON is explicit
    and works for the taskbar button (which reads ICON_BIG) too.
    """
    if not is_windows():
        return False
    target = str(Path(icon_path))
    if not os.path.exists(target):
        return False
    WM_SETICON = 0x0080
    ICON_SMALL, ICON_BIG = 0, 1
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    SM_CXICON, SM_CYICON, SM_CXSMICON, SM_CYSMICON = 11, 12, 49, 50
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.LoadImageW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        user32.LoadImageW.restype = ctypes.c_void_p
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
        user32.SendMessageW.restype = ctypes.c_void_p
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        user32.GetSystemMetrics.restype = ctypes.c_int

        small = user32.LoadImageW(
            None,
            target,
            IMAGE_ICON,
            user32.GetSystemMetrics(SM_CXSMICON),
            user32.GetSystemMetrics(SM_CYSMICON),
            LR_LOADFROMFILE,
        )
        big = user32.LoadImageW(
            None,
            target,
            IMAGE_ICON,
            user32.GetSystemMetrics(SM_CXICON),
            user32.GetSystemMetrics(SM_CYICON),
            LR_LOADFROMFILE,
        )
        applied = False
        if small:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
            applied = True
        if big:
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
            applied = True
        return applied
    except Exception:  # pragma: no cover - defensive
        return False


def show_properties(path: str | Path) -> bool:
    """Open the Windows "Properties" dialog for a file or folder.

    This invokes the shell verb, so it behaves exactly like Explorer does:
    size, details, permissions, previous versions, the security tab, ...
    """
    if not is_windows():
        return False
    target = str(Path(path))
    if not os.path.exists(target):
        return False
    # SEE_MASK_INVOKEIDLIST (0x0C) is what makes the "properties" verb work.
    SEE_MASK_INVOKEIDLIST = 0x0000000C
    info = _ShellExecuteInfoW()
    info.cbSize = ctypes.sizeof(_ShellExecuteInfoW)
    info.fMask = SEE_MASK_INVOKEIDLIST
    info.lpVerb = "properties"
    info.lpFile = target
    info.nShow = 1  # SW_SHOWNORMAL
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfoW)]
        shell32.ShellExecuteExW.restype = ctypes.c_int
        return bool(shell32.ShellExecuteExW(ctypes.byref(info)))
    except Exception:  # pragma: no cover - defensive
        return False


def copy_to_clipboard(text: str) -> bool:
    if not is_windows():
        return False
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return True
    except Exception:  # pragma: no cover - defensive
        return False


def write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def one_line(items: Iterable[str]) -> str:
    return ", ".join(items) if items else ""


class Stopwatch:
    def __init__(self) -> None:
        self.start = time.perf_counter()

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self.start

    def __enter__(self) -> "Stopwatch":
        return self

    def __exit__(self, *exc: object) -> None:
        pass


def version_string() -> str:
    return f"{APP_NAME} {__version__} (Python {sys.version.split()[0]})"


# ------------------------------------------------------------------ memory ---
class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def memory_usage() -> tuple[int, int]:
    """Return ``(working_set_bytes, peak_working_set_bytes)`` for this process."""
    if not is_windows():
        return (0, 0)
    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return (0, 0)
        return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)
    except Exception:  # pragma: no cover - defensive
        return (0, 0)
