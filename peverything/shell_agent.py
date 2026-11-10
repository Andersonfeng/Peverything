"""Windows shell integration: a global hotkey and a notification-area icon.

Both need a Win32 window with its own message loop, which Tk does not give us.
This module runs one hidden window on a background thread and forwards events
to the GUI through plain callbacks; the GUI marshals them onto the Tk thread
through its existing queue, so no Tk call ever happens off-thread.
"""

from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable

# --------------------------------------------------------------- constants ---
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_NULL = 0x0000
WM_HOTKEY = 0x0312
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_REGISTER_HOTKEY = WM_APP + 2
WM_UNREGISTER_HOTKEY = WM_APP + 3

SHOW_MESSAGE_NAME = "PeverythingShowWindow"
MUTEX_NAME = "Local\\Peverything.SingleInstance"
HWND_BROADCAST = 0xFFFF
ERROR_ALREADY_EXISTS = 183

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x1
NIF_ICON = 0x2
NIF_TIP = 0x4
NIF_INFO = 0x10
NIIF_INFO = 0x1

MF_STRING = 0x0000
MF_SEPARATOR = 0x0800
TPM_RETURNCMD = 0x0100
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
SM_CXSMICON = 49
SM_CYSMICON = 50

HOTKEY_ID = 1
TRAY_ID = 1

MENU_SHOW = 1
MENU_INDEX_DIR = 2
MENU_QUIT = 3

WS_POPUP = 0x80000000
WS_EX_TOOLWINDOW = 0x00000080


# -------------------------------------------------------------- hotkey text ---
_NAMED_KEYS: dict[str, int] = {
    "space": 0x20,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "tab": 0x09,
    "backspace": 0x08,
    "insert": 0x2D,
    "ins": 0x2D,
    "delete": 0x2E,
    "del": 0x2E,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pgup": 0x21,
    "pagedown": 0x22,
    "pgdn": 0x22,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "printscreen": 0x2C,
    "pause": 0x13,
    "`": 0xC0,
    "-": 0xBD,
    "=": 0xBB,
    "[": 0xDB,
    "]": 0xDD,
    "\\": 0xDC,
    ";": 0xBA,
    "'": 0xDE,
    ",": 0xBC,
    ".": 0xBE,
    "/": 0xBF,
}

_MODIFIER_ALIASES = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "windows": MOD_WIN,
    "meta": MOD_WIN,
}

_MODIFIER_ORDER = ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"), (MOD_SHIFT, "Shift"), (MOD_WIN, "Win"))


def parse_hotkey(spec: str) -> tuple[int, int, str]:
    """``"Ctrl+Alt+E"`` -> ``(modifiers, virtual_key, canonical_text)``.

    Raises ``ValueError`` with a Chinese message for anything unusable.
    """
    text = (spec or "").strip()
    if not text:
        raise ValueError("热键不能为空")
    parts = [part.strip() for part in text.replace(" ", "").split("+") if part.strip()]
    if not parts:
        raise ValueError("热键不能为空")

    modifiers = 0
    key: str | None = None
    for part in parts:
        lowered = part.lower()
        if lowered in _MODIFIER_ALIASES:
            modifiers |= _MODIFIER_ALIASES[lowered]
            continue
        if key is not None:
            raise ValueError(f"热键里只能有一个主键：{spec}")
        key = part
    if key is None:
        raise ValueError("热键缺少主键，例如 Ctrl+Alt+E")

    lowered = key.lower()
    if len(lowered) == 1:
        char = lowered.upper()
        if "A" <= char <= "Z" or "0" <= char <= "9":
            vk = ord(char)
        else:
            vk = _NAMED_KEYS.get(lowered, 0)
        if not vk:
            raise ValueError(f"无法识别的按键：{key}")
    elif lowered.startswith("f") and lowered[1:].isdigit() and 1 <= int(lowered[1:]) <= 24:
        vk = 0x70 + int(lowered[1:]) - 1
    elif lowered in _NAMED_KEYS:
        vk = _NAMED_KEYS[lowered]
    else:
        raise ValueError(f"无法识别的按键：{key}")

    names = [name for mask, name in _MODIFIER_ORDER if modifiers & mask]
    display = "+".join(names + [key.upper() if len(key) == 1 else key.capitalize()])
    return modifiers, vk, display


# ------------------------------------------------------------------ wintypes ---
_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", _WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]


class NOTIFYICONIDENTIFIER(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("guidItem", ctypes.c_byte * 16),
    ]


_user32 = ctypes.WinDLL("user32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None  # type: ignore[attr-defined]
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None  # type: ignore[attr-defined]
_shell32 = ctypes.WinDLL("shell32", use_last_error=True) if hasattr(ctypes, "WinDLL") else None  # type: ignore[attr-defined]

if _user32 is not None:  # pragma: no cover - Windows only
    _user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.WORD
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.HMENU,
        wintypes.HINSTANCE,
        wintypes.LPVOID,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.DefWindowProcW.restype = ctypes.c_ssize_t
    _user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.SendMessageW.restype = ctypes.c_ssize_t
    _user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.PostQuitMessage.argtypes = [ctypes.c_int]
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    _user32.RegisterHotKey.restype = wintypes.BOOL
    _user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE,
        wintypes.LPCWSTR,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    _user32.LoadImageW.restype = wintypes.HANDLE
    _user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
    _user32.LoadIconW.restype = wintypes.HICON
    _user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    _user32.GetSystemMetrics.restype = ctypes.c_int
    _user32.CreatePopupMenu.restype = wintypes.HMENU
    _user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    _user32.DestroyMenu.argtypes = [wintypes.HMENU]
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        ctypes.c_void_p,
    ]
    _user32.TrackPopupMenu.restype = wintypes.BOOL
    _shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    _shell32.Shell_NotifyIconGetRect.argtypes = [
        ctypes.POINTER(NOTIFYICONIDENTIFIER),
        ctypes.POINTER(wintypes.RECT),
    ]
    _shell32.Shell_NotifyIconGetRect.restype = ctypes.c_long
    _user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    _user32.RegisterWindowMessageW.restype = wintypes.UINT


def broadcast_show() -> None:
    """Ask the already-running instance to show its window."""
    if _user32 is None:
        return
    message = _user32.RegisterWindowMessageW(SHOW_MESSAGE_NAME)  # type: ignore[union-attr]
    if message:
        _user32.PostMessageW(HWND_BROADCAST, message, 0, 0)  # type: ignore[union-attr]


class SingleInstance:
    """Named mutex guard so a second launch just reveals the running window."""

    def __init__(self, name: str = MUTEX_NAME) -> None:
        self._handle = None
        self.already_running = False
        if _kernel32 is None:
            return
        _kernel32.CreateMutexW.restype = wintypes.HANDLE
        handle = _kernel32.CreateMutexW(None, False, name)
        self._handle = handle
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS


class ShellAgent:
    """One hidden window that owns the global hotkey and the tray icon."""

    _counter = 0

    def __init__(
        self,
        icon_path: str | Path | None = None,
        tooltip: str = "Peverything",
        on_hotkey: Callable[[], None] | None = None,
        on_tray_activate: Callable[[], None] | None = None,
        on_tray_menu: Callable[[int], None] | None = None,
    ) -> None:
        self.icon_path = str(icon_path) if icon_path else ""
        self.tooltip = tooltip
        self.on_hotkey = on_hotkey or (lambda: None)
        self.on_tray_activate = on_tray_activate or (lambda: None)
        self.on_tray_menu = on_tray_menu or (lambda command: None)
        # A window class is process-wide, and registering the same name twice
        # would silently reuse the *first* instance's WndProc. Keep it unique.
        ShellAgent._counter += 1
        self._class_name = f"PeverythingShellAgent{ShellAgent._counter}"

        self.hwnd: int | None = None
        self.thread_id: int | None = None
        self.hotkey: str | None = None
        self.tray_visible = False
        self.error: str | None = None
        self._hotkey_result = False
        self._show_message = 0

        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._ok = False
        self._hicon: int | None = None
        self._wndproc_ref = None
        self._nid = NOTIFYICONDATAW()

    # ------------------------------------------------------------- lifecycle ---
    def start(self) -> bool:
        if _user32 is None:
            self.error = "仅支持 Windows"
            return False
        if self._thread is not None:
            return self._ok
        self._thread = threading.Thread(target=self._run, name="pev-shell", daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            self.error = "创建后台消息窗口超时"
            return False
        return self._ok

    def stop(self) -> None:
        if _user32 is None:
            return
        self.set_tray_visible(False)
        if self.hwnd:
            try:
                _user32.SendMessageW(self.hwnd, WM_UNREGISTER_HOTKEY, 0, 0)  # type: ignore[union-attr]
            except Exception:
                pass
        if self.thread_id:
            _user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self.hwnd = None
        self.thread_id = None

    # ---------------------------------------------------------------- thread ---
    def _run(self) -> None:
        try:
            self.thread_id = _kernel32.GetCurrentThreadId()  # type: ignore[union-attr]
            instance = _kernel32.GetModuleHandleW(None)  # type: ignore[union-attr]
            self._wndproc_ref = _WNDPROC(self._wndproc)

            wc = WNDCLASSW()
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = instance
            wc.lpszClassName = self._class_name
            _user32.RegisterClassW(ctypes.byref(wc))  # type: ignore[union-attr]
            hwnd = _user32.CreateWindowExW(  # type: ignore[union-attr]
                WS_EX_TOOLWINDOW,
                self._class_name,
                "Peverything",
                WS_POPUP,
                0,
                0,
                0,
                0,
                None,
                None,
                instance,
                None,
            )
            if not hwnd:
                self.error = f"CreateWindowExW 失败：{ctypes.get_last_error()}"
                return
            self.hwnd = hwnd
            self._show_message = _user32.RegisterWindowMessageW(SHOW_MESSAGE_NAME)  # type: ignore[union-attr]
            self._ok = True
        finally:
            self._ready.set()

        if not self._ok:
            return
        msg = MSG()
        while True:
            result = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)  # type: ignore[union-attr]
            if result <= 0:
                break
            _user32.TranslateMessage(ctypes.byref(msg))  # type: ignore[union-attr]
            _user32.DispatchMessageW(ctypes.byref(msg))  # type: ignore[union-attr]

        self.set_tray_visible(False)
        if self.hwnd:
            _user32.DestroyWindow(self.hwnd)  # type: ignore[union-attr]
            self.hwnd = None

    def _wndproc(self, hwnd, msg, wparam, lparam):  # noqa: ANN001 - Win32 callback
        try:
            if self._show_message and msg == self._show_message:
                # Another launch asked us to come to the front.
                self.on_tray_activate()
                return 0
            if msg == WM_TRAYICON:
                event = int(lparam) & 0xFFFF
                if event == WM_LBUTTONUP:
                    self.on_tray_activate()
                elif event == WM_RBUTTONUP:
                    self._popup_menu()
                return 0
            if msg == WM_HOTKEY:
                if int(wparam) == HOTKEY_ID:
                    self.on_hotkey()
                return 0
            if msg == WM_REGISTER_HOTKEY:
                self._hotkey_result = bool(
                    _user32.RegisterHotKey(  # type: ignore[union-attr]
                        hwnd, HOTKEY_ID, int(wparam) | MOD_NOREPEAT, int(lparam)
                    )
                )
                return 0
            if msg == WM_UNREGISTER_HOTKEY:
                _user32.UnregisterHotKey(hwnd, HOTKEY_ID)  # type: ignore[union-attr]
                return 0
            if msg == WM_CLOSE:
                _user32.DestroyWindow(hwnd)  # type: ignore[union-attr]
                return 0
            if msg == WM_DESTROY:
                _user32.PostQuitMessage(0)  # type: ignore[union-attr]
                return 0
        except Exception:  # pragma: no cover - never raise into Win32
            pass
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)  # type: ignore[union-attr]

    # ----------------------------------------------------------------- hotkey ---
    def register_hotkey(self, spec: str) -> bool:
        """Returns False when Windows refuses it (already taken by someone).

        ``RegisterHotKey`` fails with ERROR_WINDOW_OF_OTHER_THREAD unless the
        calling thread owns the window, so the actual call is marshalled onto
        the agent thread with SendMessage (which blocks until it has run).
        """
        if _user32 is None or not self.hwnd:
            return False
        modifiers, vk, canonical = parse_hotkey(spec)
        self.unregister_hotkey()
        self._hotkey_result = False
        _user32.SendMessageW(self.hwnd, WM_REGISTER_HOTKEY, modifiers, vk)  # type: ignore[union-attr]
        if not self._hotkey_result:
            self.error = f"热键 {canonical} 注册失败（可能已被其它程序占用）"
            return False
        self.hotkey = canonical
        self.error = None
        return True

    def unregister_hotkey(self) -> None:
        if _user32 is not None and self.hwnd:
            _user32.SendMessageW(self.hwnd, WM_UNREGISTER_HOTKEY, 0, 0)  # type: ignore[union-attr]
        self.hotkey = None

    def simulate_hotkey(self) -> None:
        """Test hook: post the same message the OS sends on a hotkey press."""
        if self.hwnd:
            _user32.PostMessageW(self.hwnd, WM_HOTKEY, HOTKEY_ID, 0)  # type: ignore[union-attr]

    def simulate_tray_click(self) -> None:
        if self.hwnd:
            _user32.PostMessageW(self.hwnd, WM_TRAYICON, TRAY_ID, WM_LBUTTONUP)  # type: ignore[union-attr]

    # ------------------------------------------------------------------- tray ---
    def set_tray_visible(self, visible: bool) -> bool:
        if _shell32 is None or not self.hwnd:
            return False
        if visible and not self.tray_visible:
            if not self._nid.hWnd:
                self._fill_icon_data()
            ok = bool(_shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid)))
            self.tray_visible = ok
            if not ok:
                self.error = "添加托盘图标失败"
            return ok
        if not visible and self.tray_visible:
            _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
            self.tray_visible = False
        return True

    def tray_rect(self) -> tuple[int, int, int, int] | None:
        """Screen rect of the icon, or None when it is not registered.

        ``S_FALSE`` still counts: the icon exists but Windows parked it in the
        overflow flyout, and the rect then points at the flyout button.
        """
        if _shell32 is None or not self.hwnd or not self.tray_visible:
            return None
        identifier = NOTIFYICONIDENTIFIER()
        identifier.cbSize = ctypes.sizeof(NOTIFYICONIDENTIFIER)
        identifier.hWnd = self.hwnd
        identifier.uID = TRAY_ID
        rect = wintypes.RECT()
        try:
            result = _shell32.Shell_NotifyIconGetRect(ctypes.byref(identifier), ctypes.byref(rect))
        except Exception:  # pragma: no cover - older Windows
            return None
        if result not in (0, 1):  # S_OK / S_FALSE
            return None
        return (rect.left, rect.top, rect.right, rect.bottom)

    def tray_in_overflow(self) -> bool:
        """True when the icon lives in the 'hidden icons' flyout."""
        if _shell32 is None or not self.hwnd or not self.tray_visible:
            return False
        identifier = NOTIFYICONIDENTIFIER()
        identifier.cbSize = ctypes.sizeof(NOTIFYICONIDENTIFIER)
        identifier.hWnd = self.hwnd
        identifier.uID = TRAY_ID
        rect = wintypes.RECT()
        try:
            return _shell32.Shell_NotifyIconGetRect(ctypes.byref(identifier), ctypes.byref(rect)) == 1
        except Exception:  # pragma: no cover - defensive
            return False

    def show_balloon(self, title: str, message: str) -> bool:
        """Notify the user through the tray icon (works even in the overflow)."""
        if _shell32 is None or not self.tray_visible:
            return False
        nid = self._nid
        nid.uFlags = NIF_INFO
        nid.szInfoTitle = title[:63]
        nid.szInfo = message[:255]
        nid.dwInfoFlags = NIIF_INFO
        ok = bool(_shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid)))
        nid.uFlags = 0
        return ok

    def _fill_icon_data(self) -> None:
        nid = self._nid
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = TRAY_ID
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self._load_icon()
        nid.szTip = self.tooltip[:127]

    def _load_icon(self) -> int:
        if self._hicon:
            return self._hicon
        if self.icon_path and Path(self.icon_path).exists():
            width = _user32.GetSystemMetrics(SM_CXSMICON)  # type: ignore[union-attr]
            height = _user32.GetSystemMetrics(SM_CYSMICON)  # type: ignore[union-attr]
            handle = _user32.LoadImageW(  # type: ignore[union-attr]
                None, self.icon_path, IMAGE_ICON, width, height, LR_LOADFROMFILE
            )
            if handle:
                self._hicon = handle
                return handle
        self._hicon = _user32.LoadIconW(None, ctypes.c_wchar_p(32512))  # type: ignore[union-attr]
        return self._hicon or 0

    def _popup_menu(self) -> None:
        menu = _user32.CreatePopupMenu()  # type: ignore[union-attr]
        if not menu:
            return
        command = 0
        try:
            _user32.AppendMenuW(menu, MF_STRING, MENU_SHOW, "显示 Peverything")  # type: ignore[union-attr]
            _user32.AppendMenuW(menu, MF_STRING, MENU_INDEX_DIR, "打开索引所在目录")  # type: ignore[union-attr]
            _user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)  # type: ignore[union-attr]
            _user32.AppendMenuW(menu, MF_STRING, MENU_QUIT, "退出")  # type: ignore[union-attr]
            point = wintypes.POINT()
            _user32.GetCursorPos(ctypes.byref(point))  # type: ignore[union-attr]
            _user32.SetForegroundWindow(self.hwnd)  # type: ignore[union-attr]
            command = _user32.TrackPopupMenu(  # type: ignore[union-attr]
                menu, TPM_RETURNCMD | TPM_RIGHTBUTTON | TPM_NONOTIFY, point.x, point.y, 0, self.hwnd, None
            )
            _user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)  # type: ignore[union-attr]
        finally:
            _user32.DestroyMenu(menu)  # type: ignore[union-attr]
        if command:
            self.on_tray_menu(int(command))
