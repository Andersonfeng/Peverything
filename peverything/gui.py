"""Tkinter front-end that mimics the Everything window.

Threading rule: Tk is only touched from the main thread.  Scans, searches and
journal polling run in worker threads and report back through a
``queue.Queue`` that the main thread drains with ``after()``.
"""

from __future__ import annotations

import csv
import ctypes
import queue
import sys
import threading
import time
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

from . import APP_NAME, __version__, query, scanner, util
from .index import FileRow, SearchIndex
from .settings import CLOSE_ACTIONS, DEFAULT_HOTKEY, Settings
from .shell_agent import (
    MENU_INDEX_DIR,
    MENU_QUIT,
    MENU_SHOW,
    ShellAgent,
    SingleInstance,
    broadcast_show,
    parse_hotkey,
)

MAX_ROWS = 5000
SEARCH_DEBOUNCE_MS = 120
POLL_MS = 40

#: Tk event.state bits for the modifiers we care about.
_CTRL_MASK = 0x0004
_ALT_MASK = 0x0008


def asset_path(name: str) -> Path:
    """Locate a bundled asset both from source and from a PyInstaller bundle.

    ``--add-data`` may put the folder at the bundle root or keep the package
    layout, so both candidates are probed; missing this made the frozen exe fall
    back to Tk's default icon for the window and the tray.
    """
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    candidates = (base / "assets" / name, base / "peverything" / "assets" / name)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]

COLUMNS = (
    ("name", "名称", 360, "w"),
    ("path", "路径", 560, "w"),
    ("size", "大小", 95, "e"),
    ("mtime", "修改时间", 135, "w"),
)

HELP_TEXT = """搜索语法（与 Everything 基本一致）

  空格            同时满足多个条件        报告 2024
  |               或者                    jpg|png
  !               排除                    !node_modules
  "..."           精确短语                "setup log"
  *  ?            通配符                  *.log
  ext:py;js       扩展名                  ext:mp4
  path:temp       目录部分包含            path:AppData
  nopath:readme   只匹配文件名            nopath:todo
  wfn:*.txt       整个文件名匹配          wfn:a*.docx
  regex:^C:\\\\tmp 正则表达式              regex:\\\\d{4}\\\\.log$
  size:>10mb      大小（>,<,..,=）        size:1kb..1mb
  dm:today        修改日期（today/yesterday/thisweek/
                  thismonth/thisyear/2024/2024-05/2024-05-01）
  file: folder:   只看文件 / 只看文件夹
  parent:C:\\Windows  父目录正好是
  case:           区分大小写

快捷键：Ctrl+F 聚焦搜索框 · Enter 打开 · Ctrl+Enter 打开所在文件夹
        Alt+Enter 属性 · Ctrl+C 复制路径 · F5 重新扫描 · Esc 清空搜索
"""


class PeverythingApp:
    def __init__(
        self,
        root: tk.Tk,
        index_path: Path,
        initial_query: str = "",
        settings: Settings | None = None,
        enable_shell: bool = True,
    ) -> None:
        self.root = root
        self.index_path = index_path
        self.settings = settings or Settings.load()
        self.enable_shell = enable_shell
        self.agent: ShellAgent | None = None
        self.index: SearchIndex | None = None
        self.index_lock = threading.Lock()
        self.events: queue.Queue[tuple] = queue.Queue()
        self.rows: list[FileRow] = []
        self.row_keys: list[str] = []
        self.sort_column: str | None = None
        self.sort_reverse = False
        self.search_token = 0
        self.search_job: str | None = None
        self.watcher_thread: threading.Thread | None = None
        self.watcher_stop = threading.Event()
        self.watcher_active = False
        self.scan_thread: threading.Thread | None = None
        self.scanning = False
        self.last_search_seconds = 0.0
        self.initial_query = initial_query
        self._settings_window: tk.Toplevel | None = None
        self._tray_hint_shown = False
        self._topmost_job: str | None = None

        self._build_ui()
        self._pump_job: str | None = self.root.after(POLL_MS, self._drain_events)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if initial_query:
            self.search_var.set(initial_query)
        self.root.after(80, self._load_index_async)
        if enable_shell:
            self.root.after(150, self._start_shell_agent)

    # ------------------------------------------------------------ lifecycle ---
    def close(self) -> None:
        """The window's X button: honour the configured close action."""
        action = self.settings.close_action
        if action == "tray" and self.agent is not None and self.agent.tray_visible:
            self.hide_window()
            return
        if action == "taskbar":
            self.root.iconify()
            return
        self.quit_app()

    def quit_app(self) -> None:
        """Really exit: stop background work and remove the tray icon."""
        self.watcher_stop.set()
        self.scanning = False
        if self.agent is not None:
            self.agent.stop()
            self.agent = None
        for job in (self._pump_job, self.search_job, self._topmost_job):
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:  # pragma: no cover - already gone
                    pass
        self._pump_job = None
        self.search_job = None
        self._topmost_job = None
        try:
            self.root.destroy()
        except Exception:  # pragma: no cover - already destroyed
            pass

    # --------------------------------------------------------- shell agent ---
    def _start_shell_agent(self) -> None:
        if not util.is_windows():
            return
        self.agent = ShellAgent(
            icon_path=asset_path("peverything.ico"),
            tooltip="Peverything 文件检索",
            on_hotkey=lambda: self._post("hotkey", None),
            on_tray_activate=lambda: self._post("tray_show", None),
            on_tray_menu=lambda command: self._post("tray_menu", command),
        )
        if not self.agent.start():
            self.status_label.config(text=f"全局热键不可用：{self.agent.error or '初始化失败'}")
            self.agent = None
            return
        self.apply_shell_settings()

    def apply_shell_settings(self) -> None:
        """Push the current hotkey / tray preferences to the shell agent."""
        if self.agent is None:
            return
        if self.settings.hotkey_enabled and self.settings.hotkey:
            if not self.agent.register_hotkey(self.settings.hotkey):
                self.hint_label.config(
                    text=f"热键 {self.settings.hotkey} 注册失败（可能被占用），可在「设置」里换一个"
                )
        else:
            self.agent.unregister_hotkey()
        self.agent.set_tray_visible(self.settings.tray_icon)
        self._refresh_hotkey_hint()

    def _refresh_hotkey_hint(self) -> None:
        if self.agent is not None and self.agent.hotkey:
            self.hint_label.config(text=f"按 {self.agent.hotkey} 显示 / 隐藏窗口")

    def toggle_window(self) -> None:
        if self.root.state() == "withdrawn" or self.root.state() == "iconic":
            self.show_window()
        else:
            self.hide_window()

    def hide_window(self) -> None:
        self.root.withdraw()
        if self.agent is not None and self.agent.tray_visible and not self._tray_hint_shown:
            self._tray_hint_shown = True
            hotkey = self.agent.hotkey or "热键"
            self.agent.show_balloon(
                "Peverything 仍在后台运行",
                f"按 {hotkey} 重新显示窗口；右键托盘图标可以退出。",
            )

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:  # pragma: no cover - defensive
            pass
        # A background process may not be allowed to steal focus; a short
        # topmost pulse is the usual workaround.
        self.root.attributes("-topmost", True)
        if self._topmost_job:
            try:
                self.root.after_cancel(self._topmost_job)
            except Exception:  # pragma: no cover - already fired
                pass
        self._topmost_job = self.root.after(150, self._drop_topmost)
        self.search_entry.focus_set()
        self.search_entry.select_range(0, "end")

    def _drop_topmost(self) -> None:
        self._topmost_job = None
        try:
            self.root.attributes("-topmost", False)
        except Exception:  # pragma: no cover - window already gone
            pass

    def _on_hotkey(self) -> None:
        self.toggle_window()

    def _on_tray_menu(self, command: int) -> None:
        if command == MENU_SHOW:
            self.show_window()
        elif command == MENU_INDEX_DIR:
            util.reveal_path(self.index_path)
        elif command == MENU_QUIT:
            self.quit_app()

    def _apply_window_icon(self) -> None:
        """Same .ico everywhere: title bar, Alt+Tab and the taskbar button."""
        icon = asset_path("peverything.ico")
        if not icon.exists():
            return
        try:
            # `default=` covers future toplevels (the settings dialog); the
            # plain call is what actually sets the main window's icon.
            self.root.iconbitmap(str(icon))
            self.root.iconbitmap(default=str(icon))
        except Exception:  # pragma: no cover - icon is cosmetic
            pass
        # Let Tk finish creating the toplevel, then force both ICON_SMALL
        # (title bar) and ICON_BIG (taskbar) to our file.
        self.root.after(30, lambda: self._force_native_icon(icon))

    def _force_native_icon(self, icon: Path) -> None:
        try:
            hwnd = self.root.winfo_id()
            util.set_window_icon(hwnd, icon)
            # Tk toplevels are wrapped on Windows; the outer window is the one
            # the taskbar and Alt+Tab list.
            parent = ctypes.windll.user32.GetParent(hwnd)
            if parent:
                util.set_window_icon(parent, icon)
        except Exception:  # pragma: no cover - cosmetic
            pass

    # ------------------------------------------------------------------ ui ---
    def _build_ui(self) -> None:
        self.root.title(f"{APP_NAME} {__version__} - 极速文件检索")
        self.root.geometry("1180x700")
        self.root.minsize(820, 460)
        self._apply_window_icon()

        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        style.configure("Treeview", rowheight=22, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Search.TEntry", padding=4)

        menubar = tk.Menu(self.root)
        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="打开", accelerator="Enter", command=self.open_selected)
        file_menu.add_command(label="打开所在文件夹", accelerator="Ctrl+Enter", command=self.reveal_selected)
        file_menu.add_command(label="属性", accelerator="Alt+Enter", command=self.show_properties)
        file_menu.add_separator()
        file_menu.add_command(label="复制完整路径", accelerator="Ctrl+C", command=self.copy_path)
        file_menu.add_command(label="复制文件名", command=self.copy_name)
        file_menu.add_command(label="导出结果为 CSV ...", command=self.export_csv)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.quit_app)
        menubar.add_cascade(label="文件", menu=file_menu)

        index_menu = tk.Menu(menubar, tearoff=False)
        index_menu.add_command(label="立即扫描全部磁盘", accelerator="F5", command=self.scan_all)
        index_menu.add_command(label="扫描指定文件夹 ...", command=self.scan_folder)
        index_menu.add_separator()
        self.watch_var = tk.BooleanVar(value=False)
        index_menu.add_checkbutton(
            label="实时监控文件变化（USN 日志，需管理员）",
            variable=self.watch_var,
            command=self.toggle_watch,
        )
        index_menu.add_command(label="以管理员身份重启（启用 MFT 直读）", command=self.restart_elevated)
        index_menu.add_separator()
        index_menu.add_command(label="索引信息 ...", command=self.show_index_info)
        menubar.add_cascade(label="索引", menu=index_menu)

        settings_menu = tk.Menu(menubar, tearoff=False)
        settings_menu.add_command(label="首选项 ...", accelerator="Ctrl+,", command=self.open_settings)
        settings_menu.add_command(label="显示 / 隐藏窗口", accelerator="全局热键", command=self.toggle_window)
        menubar.add_cascade(label="设置", menu=settings_menu)

        help_menu = tk.Menu(menubar, tearoff=False)
        help_menu.add_command(label="搜索语法", command=lambda: messagebox.showinfo("搜索语法", HELP_TEXT))
        help_menu.add_command(
            label="关于",
            command=lambda: messagebox.showinfo(
                "关于",
                f"{APP_NAME} {__version__}\n\nEverything 的 Python 实现："
                "NTFS MFT 直读 + USN 日志增量更新。\n"
                f"Python {__import__('sys').version.split()[0]} · {util.now_iso()}",
            ),
        )
        menubar.add_cascade(label="帮助", menu=help_menu)
        self.root.config(menu=menubar)

        top = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        top.pack(fill="x")
        ttk.Label(top, text="搜索", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left", padx=(0, 6))
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(
            top, textvariable=self.search_var, font=("Microsoft YaHei UI", 12), style="Search.TEntry"
        )
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<KeyRelease>", self._on_search_key)
        self.search_entry.bind("<Return>", self._on_open_key)
        self.search_entry.bind("<Control-Return>", self._on_reveal_key)
        self.search_entry.bind("<Alt-Return>", self._on_properties_key)
        self.search_entry.bind("<Escape>", lambda _event: self.clear_search())
        self.search_var.trace_add("write", lambda *_: self._schedule_search())
        self.count_label = ttk.Label(top, text="", font=("Microsoft YaHei UI", 9))
        self.count_label.pack(side="left", padx=(10, 0))

        table_frame = ttk.Frame(self.root, padding=(8, 0, 8, 0))
        table_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            table_frame, columns=[column[0] for column in COLUMNS], show="headings", selectmode="extended"
        )
        for key, title, width, anchor in COLUMNS:
            self.tree.heading(key, text=title, anchor=anchor, command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "path"))
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.tag_configure("dir", foreground="#0a5c9e")
        self.tree.tag_configure("odd", background="#f7f9fb")
        self.tree.bind("<Double-1>", lambda _event: self.open_selected())
        self.tree.bind("<Return>", self._on_open_key)
        self.tree.bind("<Control-Return>", self._on_reveal_key)
        self.tree.bind("<Alt-Return>", self._on_properties_key)
        self.tree.bind("<Control-c>", lambda _event: self.copy_path())

        self.menu = tk.Menu(self.root, tearoff=False)
        self.menu.add_command(label="打开", command=self.open_selected)
        self.menu.add_command(label="打开所在文件夹", command=self.reveal_selected)
        self.menu.add_command(label="属性", command=self.show_properties)
        self.menu.add_separator()
        self.menu.add_command(label="复制完整路径", command=self.copy_path)
        self.menu.add_command(label="复制文件名", command=self.copy_name)
        self.tree.bind("<Button-3>", self._show_menu)

        bottom = ttk.Frame(self.root, padding=(8, 4, 8, 6))
        bottom.pack(fill="x")
        self.status_label = ttk.Label(bottom, text="正在加载索引 ...", font=("Microsoft YaHei UI", 9))
        self.status_label.pack(side="left")
        self.hint_label = ttk.Label(bottom, text="", font=("Microsoft YaHei UI", 9), foreground="#666666")
        self.hint_label.pack(side="right")
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=140)

        self.root.bind("<Control-f>", lambda _event: self.search_entry.focus_set())
        self.root.bind("<F5>", lambda _event: self.scan_all())
        self.root.bind("<Alt-Return>", lambda _event: self.show_properties())
        self.root.bind("<Control-comma>", lambda _event: self.open_settings())
        self.search_entry.focus_set()

    # --------------------------------------------------------------- utils ---
    def _post(self, kind: str, payload: object = None) -> None:
        self.events.put((kind, payload))

    def _drain_events(self) -> None:
        self._pump_job = None
        try:
            while True:
                kind, payload = self.events.get_nowait()
                try:
                    self._handle_event(kind, payload)
                except Exception:  # noqa: BLE001 - one bad event must not kill the pump
                    traceback.print_exc()
        except queue.Empty:
            pass
        self._pump_job = self.root.after(POLL_MS, self._drain_events)

    def _handle_event(self, kind: str, payload) -> None:
        if kind == "status":
            self.status_label.config(text=str(payload))
        elif kind == "hint":
            self.hint_label.config(text=str(payload))
        elif kind == "progress":
            message, count = payload  # type: ignore[misc]
            self.status_label.config(text=f"{message}")
        elif kind == "error":
            self.status_label.config(text="出错了")
            messagebox.showerror("出错了", str(payload))
        elif kind == "index_loaded":
            self._on_index_loaded(payload)
        elif kind == "scan_started":
            self.scanning = True
            self.progress.pack(side="right", padx=(0, 8))
            self.progress.start(12)
        elif kind == "scan_done":
            self._on_scan_done(payload)
        elif kind == "scan_failed":
            self.scanning = False
            self.progress.stop()
            self.progress.pack_forget()
            messagebox.showerror("扫描失败", str(payload))
        elif kind == "search_done":
            self._on_search_done(payload)
        elif kind == "watch_changes":
            self._on_watch_changes(payload)
        elif kind == "watch_error":
            self.watcher_active = False
            self.watch_var.set(False)
            self._update_status()
            messagebox.showwarning("实时监控已停止", str(payload))
        elif kind == "hotkey":
            self._on_hotkey()
        elif kind == "tray_show":
            self.show_window()
        elif kind == "tray_menu":
            self._on_tray_menu(int(payload))

    # --------------------------------------------------------------- index ---
    def _load_index_async(self) -> None:
        path = self.index_path

        def worker() -> None:
            if not path.exists():
                self._post("index_loaded", None)
                return
            try:
                index = SearchIndex.load(path)
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                self._post("error", f"无法读取索引 {path}：{exc}")
                self._post("index_loaded", None)
                return
            self._post("index_loaded", index)

        threading.Thread(target=worker, name="pev-load", daemon=True).start()

    def _on_index_loaded(self, index: SearchIndex | None) -> None:
        if index is None:
            self.status_label.config(
                text="尚未建立索引 · 按 F5 扫描全部磁盘，或用「索引 → 扫描指定文件夹」"
            )
            self.hint_label.config(text="提示：以管理员身份运行可用 MFT 直读，速度快 10 倍以上")
            return
        with self.index_lock:
            self.index = index
        self._update_status()
        self._schedule_search(immediate=True)
        if util.is_admin():
            self.watch_var.set(True)
            self.toggle_watch()

    def _update_status(self) -> None:
        parts: list[str] = []
        with self.index_lock:
            index = self.index
        if index is not None:
            meta = index.meta
            parts.append(f"索引 {util.format_count(index.count)} 个对象")
            source = meta.get("source")
            if source:
                label = {"mft": "MFT 直读", "walk": "目录遍历", "mixed": "混合"}.get(source, source)
                parts.append(f"来源 {label}")
            if meta.get("has_stat") is False:
                parts.append("无大小/时间")
        else:
            parts.append("无索引")
        parts.append("监控 开" if self.watcher_active else "监控 关")
        if not util.is_admin():
            parts.append("非管理员")
        self.status_label.config(text="  |  ".join(parts))

    # ---------------------------------------------------------------- scan ---
    def scan_all(self) -> None:
        if self.scanning:
            return
        self.start_scan(None, None)

    def scan_folder(self) -> None:
        if self.scanning:
            return
        folder = filedialog.askdirectory(title="选择要索引的文件夹")
        if folder:
            self.start_scan(None, [folder])

    def start_scan(self, volumes: list[str] | None, directories: list[str] | None) -> None:
        self._post("scan_started")
        method = "auto"

        def progress(message: str, count: int) -> None:
            self._post("progress", (message, count))

        def worker() -> None:
            try:
                result = scanner.scan(volumes=volumes, directories=directories, method=method, progress=progress)
                if not result.paths:
                    self._post("scan_failed", "没有扫描到任何文件")
                    return
                self._post("progress", (f"正在整理索引：{util.format_count(len(result))} 个对象 ...", len(result)))
                table = result.to_table()
                table.save(self.index_path)
                self._post("scan_done", (result, table, SearchIndex(table)))
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                traceback.print_exc()
                self._post("scan_failed", str(exc))

        self.scan_thread = threading.Thread(target=worker, name="pev-scan", daemon=True)
        self.scan_thread.start()

    def _on_scan_done(self, payload) -> None:
        result, table, index = payload
        self.scanning = False
        self.progress.stop()
        self.progress.pack_forget()
        with self.index_lock:
            self.index = index
        self._update_status()
        self.status_label.config(
            text=f"扫描完成：{util.format_count(table.count)} 个对象 · "
            f"{util.format_duration(result.seconds)} · {result.source} · 索引 {util.format_size(self.index_path.stat().st_size)}"
        )
        self._schedule_search(immediate=True)
        if util.is_admin() and not self.watcher_active:
            self.watch_var.set(True)
            self.toggle_watch()

    def restart_elevated(self) -> None:
        argv = ["gui", "-o", str(self.index_path)]
        if util.is_admin():
            messagebox.showinfo("已经是管理员", "当前进程已经是管理员权限。")
            return
        if util.relaunch_as_admin(argv):
            self.root.destroy()
        else:
            messagebox.showwarning("提权失败", "无法以管理员身份启动，请手动以管理员身份运行。")

    # -------------------------------------------------------------- search ---
    def _on_search_key(self, event: tk.Event) -> None:
        if event.keysym in ("Escape", "Return", "Control_R", "Control_L", "Shift_L", "Shift_R"):
            return
        self._schedule_search()

    def _schedule_search(self, immediate: bool = False) -> None:
        if self.search_job is not None:
            try:
                self.root.after_cancel(self.search_job)
            except Exception:  # pragma: no cover - defensive
                pass
            self.search_job = None
        delay = 0 if immediate else SEARCH_DEBOUNCE_MS
        self.search_job = self.root.after(delay, self._run_search_now)

    def _run_search_now(self) -> None:
        if self.search_job is not None:
            # Called directly (Enter key, tests) while the debounce timer is
            # still pending: drop it, otherwise it would fire after shutdown.
            try:
                self.root.after_cancel(self.search_job)
            except Exception:  # pragma: no cover - already fired
                pass
            self.search_job = None
        text = self.search_var.get()
        with self.index_lock:
            index = self.index
        if index is None:
            self.rows = []
            self._render([])
            self.count_label.config(text="")
            return
        self.search_token += 1
        token = self.search_token

        def worker() -> None:
            started = time.perf_counter()
            try:
                parsed = query.parse_query(text)
                rows = index.search(parsed, limit=MAX_ROWS)
                error = None
                warnings = parsed.warnings
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                rows, error, warnings = [], str(exc), []
            elapsed = time.perf_counter() - started
            self._post("search_done", (token, rows, elapsed, error, warnings))

        threading.Thread(target=worker, name="pev-search", daemon=True).start()

    def _on_search_done(self, payload) -> None:
        token, rows, elapsed, error, warnings = payload
        if token != self.search_token:
            return
        self.last_search_seconds = elapsed
        self.rows = rows
        if error:
            self.count_label.config(text="搜索出错")
            self.status_label.config(text=error)
            return
        self._render(rows)
        suffix = ""
        if len(rows) >= MAX_ROWS:
            suffix = f"（仅显示前 {MAX_ROWS:,} 条）"
        self.count_label.config(
            text=f"{len(rows):,} 个对象{suffix} · {util.format_duration(elapsed)}"
        )
        if warnings:
            self.hint_label.config(text=" / ".join(warnings))

    def _render(self, rows: list[FileRow]) -> None:
        self.tree.delete(*self.tree.get_children())
        self.row_keys = []
        for position, row in enumerate(rows):
            tags: list[str] = []
            if position % 2:
                tags.append("odd")
            if row.is_dir:
                tags.append("dir")
            size_text = "<目录>" if row.is_dir else util.format_size(row.size)
            self.row_keys.append(row.path)
            self.tree.insert(
                "",
                "end",
                values=(row.name, row.path, size_text, util.format_time(row.mtime)),
                tags=tuple(tags),
            )
        self.hint_label.config(text=f"共 {len(rows):,} 条")

    def sort_by(self, column: str) -> None:
        if not self.rows:
            return
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        key = {
            "name": lambda row: row.name.lower(),
            "path": lambda row: row.path.lower(),
            "size": lambda row: row.size,
            "mtime": lambda row: row.mtime,
        }[column]
        self.rows.sort(key=key, reverse=self.sort_reverse)
        self._render(self.rows)

    def clear_search(self) -> None:
        self.search_var.set("")
        self.search_entry.focus_set()

    # ------------------------------------------------------------ keyboard ---
    # Tk ignores modifiers that a pattern does not mention, so `<Return>` also
    # fires for Ctrl+Enter / Alt+Enter. Every combination binding must therefore
    # return "break", and the plain one must refuse to run with a modifier down;
    # otherwise Ctrl+Enter opened the file before jumping to its folder.
    def _on_open_key(self, event: tk.Event) -> str:
        if event.state & (_CTRL_MASK | _ALT_MASK):
            return "break"
        self.open_selected()
        return "break"

    def _on_reveal_key(self, event: tk.Event) -> str:
        self.reveal_selected()
        return "break"

    def _on_properties_key(self, event: tk.Event) -> str:
        self.show_properties()
        return "break"

    # ----------------------------------------------------------- selection ---
    def _selected_row(self) -> FileRow | None:
        selection = self.tree.selection()
        if not selection:
            return None
        index = self.tree.index(selection[0])
        if 0 <= index < len(self.rows):
            return self.rows[index]
        return None

    def open_selected(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        try:
            util.open_path(row.path)
        except OSError as exc:
            messagebox.showwarning("无法打开", f"{row.path}\n{exc}")

    def reveal_selected(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        util.reveal_path(row.path)

    def show_properties(self) -> None:
        """Open the Windows properties sheet, exactly like Explorer does."""
        row = self._selected_row()
        if row is None:
            return
        if not util.show_properties(row.path):
            messagebox.showwarning("属性", f"无法显示属性：\n{row.path}")

    def copy_path(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        paths = [self.rows[self.tree.index(item)].path for item in selection if self.tree.index(item) < len(self.rows)]
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(paths))
        self.hint_label.config(text=f"已复制 {len(paths)} 条路径")

    def copy_name(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(row.name)
        self.hint_label.config(text="已复制文件名")

    def _show_menu(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.menu.tk_popup(event.x_root, event.y_root)

    def export_csv(self) -> None:
        if not self.rows:
            messagebox.showinfo("导出", "当前没有结果可导出。")
            return
        target = filedialog.asksaveasfilename(
            title="导出结果", defaultextension=".csv", filetypes=[("CSV", "*.csv")]
        )
        if not target:
            return
        with open(target, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.writer(handle)
            writer.writerow(["名称", "完整路径", "大小", "修改时间", "是否目录"])
            for row in self.rows:
                writer.writerow([row.name, row.path, row.size, util.format_time(row.mtime), row.is_dir])
        self.hint_label.config(text=f"已导出 {len(self.rows):,} 条到 {target}")

    # --------------------------------------------------------------- watch ---
    def toggle_watch(self) -> None:
        if self.watch_var.get():
            self._start_watch()
        else:
            self._stop_watch()

    def _start_watch(self) -> None:
        if self.watcher_active:
            return
        if not util.is_admin():
            self.watch_var.set(False)
            messagebox.showinfo(
                "需要管理员权限",
                "实时监控需要读取 NTFS 变更日志，请使用「索引 → 以管理员身份重启」。",
            )
            return
        self.watcher_active = True
        self.watcher_stop.clear()
        self._update_status()
        self.status_label.config(text="正在建立实时监控（读取目录表与变更日志）...")

        def worker() -> None:
            from .watcher import JournalReset, JournalWatcher

            watcher = JournalWatcher()
            try:
                watcher.start(progress=lambda message, count: self._post("progress", (message, count)))
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                self._post("watch_error", str(exc))
                return
            if not watcher.active:
                self._post("watch_error", "无法监控任何卷：" + "; ".join(watcher.warnings))
                return
            self._post("status", f"实时监控中：{', '.join(item.letter for item in watcher.trackers)}")
            try:
                while not self.watcher_stop.is_set():
                    changes = watcher.poll(timeout_ms=500)
                    if changes:
                        self._post("watch_changes", changes)
            except JournalReset as exc:
                self._post("watch_error", str(exc) + "\n请按 F5 重新扫描。")
            except Exception as exc:  # noqa: BLE001 - surfaced to the user
                self._post("watch_error", str(exc))
            finally:
                watcher.close()

        self.watcher_thread = threading.Thread(target=worker, name="pev-watch", daemon=True)
        self.watcher_thread.start()

    def _stop_watch(self) -> None:
        self.watcher_active = False
        self.watcher_stop.set()
        self._update_status()

    def _on_watch_changes(self, changes) -> None:
        with self.index_lock:
            index = self.index
            if index is None:
                return
            changed = index.apply(changes.added, changes.removed)
        self._update_status()
        self.hint_label.config(text=f"实时更新：+{len(changes.added)} / -{len(changes.removed)}")
        if changed and self.search_var.get().strip():
            self._schedule_search()

    # ---------------------------------------------------------------- misc ---
    def show_index_info(self) -> None:
        with self.index_lock:
            index = self.index
        if index is None:
            messagebox.showinfo("索引信息", "尚未建立索引。")
            return
        meta = index.meta
        lines = [
            f"索引文件：{self.index_path}",
            f"文件大小：{util.format_size(self.index_path.stat().st_size) if self.index_path.exists() else '-'}",
            f"对象数量：{util.format_count(index.count)}",
            f"数据来源：{meta.get('source', '?')}",
            f"包含卷：{', '.join(meta.get('volumes') or []) or '-'}",
            f"含大小/时间：{'是' if meta.get('has_stat') else '否'}",
        ]
        created = meta.get("created")
        if created:
            lines.append(f"建立时间：{util.format_time(int(created), '%Y-%m-%d %H:%M:%S')}")
        messagebox.showinfo("索引信息", "\n".join(lines))

    # ------------------------------------------------------------ settings ---
    def open_settings(self) -> None:
        if self._settings_window is not None:
            self._settings_window.lift()
            return
        window = tk.Toplevel(self.root)
        self._settings_window = window
        window.title("设置")
        window.transient(self.root)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", lambda: self._close_settings(window))

        frame = ttk.Frame(window, padding=14)
        frame.pack(fill="both", expand=True)

        hotkey_var = tk.StringVar(value=self.settings.hotkey)
        enabled_var = tk.BooleanVar(value=self.settings.hotkey_enabled)
        tray_var = tk.BooleanVar(value=self.settings.tray_icon)
        close_var = tk.StringVar(value=self.settings.close_action)

        ttk.Label(frame, text="全局热键（显示 / 隐藏窗口）", font=("Microsoft YaHei UI", 9, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        entry = ttk.Entry(frame, textvariable=hotkey_var, width=24, font=("Consolas", 10))
        entry.grid(row=1, column=0, sticky="w", pady=(6, 2))
        ttk.Checkbutton(frame, text="启用", variable=enabled_var).grid(row=1, column=1, sticky="w", padx=(8, 0))
        ttk.Label(
            frame,
            text="例如 Ctrl+Alt+E、Win+Shift+F、Ctrl+Shift+Space；留空表示不启用",
            foreground="#666666",
        ).grid(row=2, column=0, columnspan=2, sticky="w")

        ttk.Separator(frame, orient="horizontal").grid(row=3, column=0, columnspan=2, sticky="ew", pady=12)

        ttk.Label(frame, text="关闭窗口时", font=("Microsoft YaHei UI", 9, "bold")).grid(
            row=4, column=0, columnspan=2, sticky="w"
        )
        for offset, (value, label) in enumerate(CLOSE_ACTIONS.items()):
            ttk.Radiobutton(frame, text=label, value=value, variable=close_var).grid(
                row=5 + offset, column=0, columnspan=2, sticky="w", pady=(4, 0)
            )
        ttk.Checkbutton(frame, text="在通知区域显示图标（托盘）", variable=tray_var).grid(
            row=8, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=9, column=0, columnspan=2, sticky="e", pady=(16, 0))

        def save() -> None:
            spec = hotkey_var.get().strip()
            if enabled_var.get() and spec:
                try:
                    parse_hotkey(spec)
                except ValueError as exc:
                    messagebox.showwarning("热键无效", str(exc), parent=window)
                    return
            self.settings.hotkey = spec
            self.settings.hotkey_enabled = bool(enabled_var.get()) and bool(spec)
            self.settings.tray_icon = bool(tray_var.get())
            self.settings.close_action = close_var.get() or "tray"
            try:
                self.settings.save()
            except OSError as exc:
                messagebox.showwarning("设置未保存", f"{exc}", parent=window)
            self.apply_shell_settings()
            self._close_settings(window)

        ttk.Button(buttons, text="保存", command=save, width=10).pack(side="right")
        ttk.Button(buttons, text="取消", command=lambda: self._close_settings(window), width=10).pack(
            side="right", padx=(0, 8)
        )
        ttk.Button(
            buttons,
            text="恢复默认",
            command=lambda: hotkey_var.set(DEFAULT_HOTKEY) or enabled_var.set(True),
            width=10,
        ).pack(side="left")

        window.grab_set()
        entry.focus_set()
        window.bind("<Escape>", lambda _event: self._close_settings(window))
        window.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - window.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - window.winfo_height()) // 3
        window.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _close_settings(self, window: tk.Toplevel) -> None:
        try:
            window.grab_release()
        except Exception:  # pragma: no cover - defensive
            pass
        window.destroy()
        self._settings_window = None


def launch(index_path: Path | None = None, initial_query: str = "") -> int:
    """Open the window; returns a process exit code."""
    guard = SingleInstance()
    if guard.already_running:
        # A resident copy is already running: bring it to the front and leave.
        broadcast_show()
        return 0
    root = tk.Tk()
    try:
        app = PeverythingApp(root, index_path or util.default_index_path(), initial_query)
        app.single_instance = guard  # keep the mutex handle alive
        if app.settings.start_hidden:
            root.after(300, app.hide_window)
    except Exception:  # pragma: no cover - startup failure
        traceback.print_exc()
        messagebox.showerror("启动失败", traceback.format_exc())
        return 1
    root.mainloop()
    return 0
