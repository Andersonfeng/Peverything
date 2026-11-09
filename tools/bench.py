"""Micro benchmark for an existing index.

Usage::

    python tools/bench.py [index.pev] [--query "*.dll"] [-n 20]

Reports load time, memory footprint and per-query latency, which is what the
numbers in the README are measured with.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from peverything import util  # noqa: E402
from peverything.index import PathTable  # noqa: E402
from peverything.query import parse_query, run_query  # noqa: E402

DEFAULT_QUERIES = [
    "*.dll",
    "*.log",
    "ext:exe",
    "ext:py;js;ts",
    "!dll",
    "config",
    "regex:\\d{4}\\.log",
    "size:>100mb",
    "path:temp",
    "setup ext:log|ini",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("index", nargs="?", default=str(util.default_index_path()))
    parser.add_argument("--query", "-q", action="append", default=[], help="extra query to measure")
    parser.add_argument("--repeat", "-n", type=int, default=3)
    args = parser.parse_args()

    path = Path(args.index)
    if not path.exists():
        print(f"index not found: {path}", file=sys.stderr)
        return 1

    before = util.memory_usage()[0]
    started = time.perf_counter()
    table = PathTable.load(path)
    load_seconds = time.perf_counter() - started
    working, peak = util.memory_usage()

    print(f"索引文件 : {path}")
    print(f"磁盘大小 : {util.format_size(path.stat().st_size)}")
    print(f"对象数量 : {table.count:,}")
    print(f"加载耗时 : {util.format_duration(load_seconds)}")
    print(f"内存占用 : {util.format_size(working)}（峰值 {util.format_size(peak)}，加载前 {util.format_size(before)}）")
    print(f"来源     : {table.meta.get('source')}")
    print()
    print(f"{'查询':<28} {'命中':>10} {'首次':>10} {'平均':>10}")
    print("-" * 62)

    for text in DEFAULT_QUERIES + args.query:
        parsed = parse_query(text)
        first = 0.0
        total = 0.0
        hits = 0
        for attempt in range(max(1, args.repeat)):
            began = time.perf_counter()
            hits = len(run_query(table, parsed))
            elapsed = time.perf_counter() - began
            total += elapsed
            if attempt == 0:
                first = elapsed
        print(
            f"{text:<28} {hits:>10,} {first * 1000:>9.1f}ms "
            f"{total / max(1, args.repeat) * 1000:>9.1f}ms"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
