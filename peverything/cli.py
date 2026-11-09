"""Command line interface: ``python -m peverything <command>``."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from . import APP_NAME, __version__, query, scanner, util
from .index import PathTable, SearchIndex
from .usn import UsnError

SORT_KEYS = {
    "name": lambda row: row.name.lower(),
    "path": lambda row: row.path.lower(),
    "size": lambda row: row.size,
    "time": lambda row: row.mtime,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pev",
        description=f"{APP_NAME} - 极速 Windows 文件检索（Everything 的 Python 实现）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  pev scan                     建立全部本地磁盘的索引\n"
            "  pev scan -v C,D --method mft 用 MFT 直读（需要管理员）\n"
            "  pev search 'report ext:pdf'  搜索 pdf 报告\n"
            "  pev search 'size:>1gb' -n 50 找大于 1GB 的文件\n"
            "  pev gui                      打开图形界面\n"
            "  pev watch --apply            用 USN 日志实时更新索引\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # ---------------------------------------------------------------- scan ---
    p_scan = sub.add_parser("scan", help="扫描磁盘并建立索引")
    p_scan.add_argument("targets", nargs="*", help="只扫描这些目录（默认扫描全部本地磁盘）")
    p_scan.add_argument("-v", "--volumes", default="", help="要扫描的盘符，例如 C,D")
    p_scan.add_argument("-o", "--index", default=None, help="索引文件路径")
    p_scan.add_argument(
        "--method",
        choices=("auto", "mft", "walk"),
        default="auto",
        help="auto=管理员时用 MFT 直读并自动降级，mft=强制直读，walk=目录遍历（默认 auto）",
    )
    p_scan.add_argument("--exclude", action="append", default=[], help="排除目录（通配符，可多次指定）")
    p_scan.add_argument("--quiet", "-q", action="store_true", help="不显示进度")
    p_scan.add_argument("--elevate", action="store_true", help="以管理员身份重新启动以使用 MFT 直读")
    p_scan.add_argument("--workers", type=int, default=8, help="目录遍历线程数（默认 8）")

    # -------------------------------------------------------------- search ---
    p_search = sub.add_parser("search", help="搜索索引")
    p_search.add_argument("terms", nargs="*", help="搜索词（支持 Everything 语法）")
    p_search.add_argument("-o", "--index", default=None, help="索引文件路径")
    p_search.add_argument("-n", "--limit", type=int, default=200, help="最多显示多少条（默认 200，0 表示不限）")
    p_search.add_argument("--sort", choices=tuple(SORT_KEYS), default=None, help="排序字段")
    p_search.add_argument("--reverse", action="store_true", help="反向排序")
    p_search.add_argument("--json", action="store_true", help="输出 JSON")
    p_search.add_argument("--long", action="store_true", help="显示大小与修改时间")
    p_search.add_argument("--files-only", action="store_true", help="只显示文件")
    p_search.add_argument("--dirs-only", action="store_true", help="只显示文件夹")
    p_search.add_argument("--count-only", action="store_true", help="只输出匹配数量")
    p_search.add_argument("--reveal", action="store_true", help="在资源管理器中定位第一条结果")
    p_search.add_argument("--open", action="store_true", help="用默认程序打开第一条结果")

    # ----------------------------------------------------------------- gui ---
    p_gui = sub.add_parser("gui", help="打开图形界面")
    p_gui.add_argument("-o", "--index", default=None, help="索引文件路径")
    p_gui.add_argument("--query", default="", help="启动时预填的搜索词")

    # --------------------------------------------------------------- watch ---
    p_watch = sub.add_parser("watch", help="用 USN 变更日志实时跟踪文件变化")
    p_watch.add_argument("-o", "--index", default=None, help="索引文件路径")
    p_watch.add_argument("-v", "--volumes", default="", help="要监控的盘符，默认全部本地磁盘")
    p_watch.add_argument("--apply", action="store_true", help="把变化写回索引文件（否则只打印）")
    p_watch.add_argument("--interval", type=float, default=1.0, help="轮询间隔秒数（默认 1.0）")

    # ---------------------------------------------------------------- info ---
    p_info = sub.add_parser("info", help="显示索引信息")
    p_info.add_argument("-o", "--index", default=None, help="索引文件路径")
    p_info.add_argument("--json", action="store_true", help="输出 JSON")

    return parser


def index_path(args) -> Path:
    return Path(args.index).expanduser() if args.index else util.default_index_path()


# ------------------------------------------------------------------ output ---
class _Progress:
    def __init__(self, quiet: bool = False) -> None:
        self.quiet = quiet
        self._last = 0.0
        self._width = shutil.get_terminal_size((100, 20)).columns
        self._dirty = False

    def __call__(self, message: str, count: int | None = None) -> None:
        if self.quiet:
            return
        now = time.perf_counter()
        if now - self._last < 0.08:
            return
        self._last = now
        line = util.truncate(message, max(20, self._width - 2), ellipsis="...")
        sys.stdout.write("\r" + util.pad(line, self._width - 1))
        sys.stdout.flush()
        self._dirty = True

    def clear(self) -> None:
        if self.quiet or not self._dirty:
            return
        sys.stdout.write("\r" + " " * (self._width - 1) + "\r")
        sys.stdout.flush()
        self._dirty = False


def _fail(message: str, code: int = 1) -> int:
    print(f"错误: {message}", file=sys.stderr)
    return code


def _load_index(path: Path) -> SearchIndex | None:
    if not path.exists():
        _fail(f"找不到索引 {path}\n请先运行: pev scan")
        return None
    try:
        return SearchIndex.load(path)
    except (OSError, ValueError) as exc:
        _fail(f"无法读取索引 {path}: {exc}")
        return None


# -------------------------------------------------------------------- scan ---
def cmd_scan(args) -> int:
    target = index_path(args)
    if args.elevate and not util.is_admin():
        argv = ["scan", *args.targets]
        if args.volumes:
            argv += ["-v", args.volumes]
        argv += ["-o", str(target), "--method", "mft"]
        if args.exclude:
            for pattern in args.exclude:
                argv += ["--exclude", pattern]
        if args.quiet:
            argv.append("--quiet")
        if util.relaunch_as_admin(argv):
            print("已在管理员窗口中重新启动扫描。")
            return 0
        return _fail("无法提权，请手动以管理员身份运行")

    volumes = [item for item in args.volumes.replace(" ", ",").split(",") if item]
    progress = _Progress(args.quiet)
    method = args.method
    if method == "mft" and not util.is_admin():
        progress.clear()
        return _fail("MFT 直读需要管理员权限：请用管理员终端运行，或加 --elevate，或改用 --method walk")

    started = time.perf_counter()
    try:
        result = scanner.scan(
            volumes=volumes or None,
            directories=args.targets or None,
            method=method,
            exclude=args.exclude,
            progress=progress,
            workers=max(1, args.workers),
        )
    except UsnError as exc:
        progress.clear()
        return _fail(str(exc))
    except KeyboardInterrupt:
        progress.clear()
        print("\n已取消。")
        return 130
    progress.clear()

    if not result.paths:
        return _fail("没有扫描到任何文件，请检查路径或权限")

    print(
        f"扫描完成：{util.format_count(len(result))} 个对象，"
        f"耗时 {util.format_duration(result.seconds)}，"
        f"速度 {util.human_rate(len(result), result.seconds)}，方式 {result.source}"
    )
    progress("正在建立索引 ...")
    table = result.to_table()
    progress("正在写入磁盘 ...")
    table.save(target)
    progress.clear()
    size = target.stat().st_size
    print(f"索引已保存：{target}（{util.format_size(size)}）")
    if result.errors:
        print(f"跳过 {len(result.errors)} 个无法访问的位置，例如：", file=sys.stderr)
        for message in result.errors[:5]:
            print(f"  {message}", file=sys.stderr)
    if result.source == "mft":
        print("提示：MFT 索引不含文件大小/修改时间，size: 与 dm: 过滤器需要 --method walk 建立的索引。")
    return 0


# ------------------------------------------------------------------ search ---
def cmd_search(args) -> int:
    path = index_path(args)
    index = _load_index(path)
    if index is None:
        return 1

    text = " ".join(args.terms)
    try:
        parsed = query.parse_query(text)
    except query.QueryError as exc:
        return _fail(str(exc))

    if args.files_only:
        parsed.groups[0].append(query.Term("type", "file"))
    if args.dirs_only:
        parsed.groups[0].append(query.Term("type", "folder"))

    for warning in parsed.warnings:
        print(f"提示: {warning}", file=sys.stderr)
    if parsed.requires_stat and not index.meta.get("has_stat", True):
        print(
            "提示: 当前索引由 MFT 直读建立，不含文件大小/修改时间，size:/dm: 条件无法匹配任何文件。",
            file=sys.stderr,
        )

    limit = None if not args.limit else args.limit
    started = time.perf_counter()
    if args.sort:
        rows = index.search(parsed)
        rows.sort(key=SORT_KEYS[args.sort], reverse=args.reverse)
        if limit:
            rows = rows[:limit]
    else:
        rows = index.search(parsed, limit=limit)
        if args.reverse:
            rows.reverse()
    elapsed = time.perf_counter() - started

    if args.count_only:
        print(len(rows) if limit is None or len(rows) < limit else f">={len(rows)}")
        return 0 if rows else 1

    if args.json:
        payload = [
            {
                "path": row.path,
                "size": row.size,
                "mtime": row.mtime,
                "is_dir": row.is_dir,
            }
            for row in rows
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_rows(rows, long_format=args.long, elapsed=elapsed, total=index.count)

    if args.reveal and rows:
        util.reveal_path(rows[0].path)
    if args.open and rows:
        try:
            util.open_path(rows[0].path)
        except OSError as exc:
            return _fail(f"无法打开 {rows[0].path}: {exc}")
    return 0 if rows else 2


def _print_rows(rows, long_format: bool, elapsed: float, total: int) -> None:
    if not rows:
        print("未找到匹配项。", file=sys.stderr)
        return
    width = shutil.get_terminal_size((120, 20)).columns
    if long_format:
        size_width = 12
        time_width = 16
        path_width = max(20, width - size_width - time_width - 4)
        print(f"{util.pad('大小', size_width, 'right')}  {util.pad('修改时间', time_width)}  {'路径'}")
        print("-" * min(width, size_width + time_width + path_width + 4))
        for row in rows:
            size_text = util.format_size(row.size) or ("<目录>" if row.is_dir else "")
            print(
                f"{util.pad(size_text, size_width, 'right')}  "
                f"{util.pad(util.format_time(row.mtime), time_width)}  "
                f"{util.truncate(row.path, path_width, ellipsis='...')}"
            )
    else:
        for row in rows:
            print(row.path)
    print(
        f"\n共 {len(rows)} 条结果，用时 {util.format_duration(elapsed)}"
        f"（索引 {util.format_count(total)} 个对象）"
    )


# -------------------------------------------------------------------- info ---
def cmd_info(args) -> int:
    path = index_path(args)
    if not path.exists():
        return _fail(f"找不到索引 {path}\n请先运行: pev scan")
    table = PathTable.load(path)
    meta = table.meta
    payload = {
        "index": str(path),
        "size_bytes": path.stat().st_size,
        "count": table.count,
        "created": meta.get("created"),
        "source": meta.get("source"),
        "volumes": meta.get("volumes"),
        "has_stat": meta.get("has_stat"),
        "scan_seconds": meta.get("scan_seconds"),
        "app_version": meta.get("app_version"),
        "memory_bytes": len(table.data) + len(table.low) + table.offsets.buffer_info()[1] * 4
        + table.attrs.buffer_info()[1] * 4
        + table.sizes.buffer_info()[1] * 8
        + table.mtimes.buffer_info()[1] * 8,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"索引文件 : {payload['index']}")
    print(f"磁盘占用 : {util.format_size(payload['size_bytes'])}")
    print(f"内存占用 : {util.format_size(payload['memory_bytes'])}")
    print(f"对象数量 : {util.format_count(table.count)}")
    created = meta.get("created")
    if created:
        print(f"建立时间 : {util.format_time(int(created), '%Y-%m-%d %H:%M:%S')}")
    print(f"数据来源 : {meta.get('source', '?')}")
    print(f"包含卷   : {', '.join(meta.get('volumes') or []) or '-'}")
    print(f"含大小/时间: {'是' if meta.get('has_stat') else '否'}")
    if meta.get("scan_seconds"):
        print(f"扫描耗时 : {util.format_duration(float(meta['scan_seconds']))}")
    return 0


# ------------------------------------------------------------------- watch ---
def cmd_watch(args) -> int:
    from .watcher import JournalReset, JournalWatcher

    path = index_path(args)
    index = _load_index(path) if path.exists() else None
    if path.exists() and index is None:
        return 1
    if not util.is_admin():
        return _fail("实时监控需要管理员权限（读取 USN 变更日志）")

    letters = [item for item in args.volumes.replace(" ", ",").split(",") if item]
    print("正在建立监控（读取目录表与变更日志）...")
    watcher = JournalWatcher(letters or None)
    try:
        watcher.start(progress=_Progress())
    except Exception as exc:  # pragma: no cover - environment dependent
        return _fail(str(exc))
    if not watcher.active:
        return _fail("无法监控任何卷：" + "; ".join(watcher.warnings))
    print(f"正在监控 {', '.join(tracker.letter for tracker in watcher.trackers)}，按 Ctrl+C 结束。")
    for warning in watcher.warnings:
        print(f"提示: {warning}", file=sys.stderr)

    stop = threading.Event()
    try:
        while not stop.is_set():
            try:
                changes = watcher.poll(timeout_ms=max(50, int(args.interval * 1000)))
            except JournalReset as exc:
                print(f"\n{exc}，需要重新扫描。", file=sys.stderr)
                return 3
            if not changes:
                continue
            print(f"\n[{util.now_iso()}] {len(changes)} 项变更")
            print(changes.describe())
            if args.apply and index is not None:
                index.apply(changes.added, changes.removed)
    except KeyboardInterrupt:
        print("\n停止监控。")
    finally:
        watcher.close()
        if args.apply and index is not None:
            index.save(path)
            print(f"索引已更新并保存：{path}")
    return 0


# --------------------------------------------------------------------- gui ---
def cmd_gui(args) -> int:
    try:
        from .gui import launch
    except ImportError as exc:
        return _fail(f"无法加载图形界面：{exc}")
    return launch(index_path(args), getattr(args, "query", "") or "")


# -------------------------------------------------------------------- main ---
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # A legacy console code page (e.g. cp936) cannot represent every file name;
    # never let that crash a search listing.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:  # pragma: no cover - non-standard stream
            pass
    util.enable_debug_privilege()
    handlers = {
        "scan": cmd_scan,
        "search": cmd_search,
        "info": cmd_info,
        "watch": cmd_watch,
        "gui": cmd_gui,
    }
    handler = handlers[args.command]
    try:
        return handler(args)
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except BrokenPipeError:  # pragma: no cover - piping to head
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
