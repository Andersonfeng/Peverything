"""Build standalone Windows executables with PyInstaller.

    python tools/build_exe.py              # one-file pev.exe + Peverything.exe
    python tools/build_exe.py --onedir     # folder build, starts faster
    python tools/build_exe.py --console-only

Output goes to ``dist/``; PyInstaller work files go to ``build/pyinstaller/``
(and are intentionally outside the source tree layout).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from peverything import APP_NAME, __version__  # noqa: E402

DIST = ROOT / "dist"
WORK = ROOT / "build" / "pyinstaller"
ICON = ROOT / "peverything" / "assets" / "peverything.ico"

VERSION_TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={quad},
    prodvers={quad},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          '080404b0',
          [
            StringStruct('CompanyName', '{app}'),
            StringStruct('FileDescription', '{app} 文件检索'),
            StringStruct('FileVersion', '{version}'),
            StringStruct('InternalName', '{name}'),
            StringStruct('LegalCopyright', 'MIT License'),
            StringStruct('OriginalFilename', '{name}.exe'),
            StringStruct('ProductName', '{app}'),
            StringStruct('ProductVersion', '{version}')
          ]
        )
      ]
    ),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def version_tuple() -> tuple[int, int, int, int]:
    parts = [int(item) for item in __version__.split(".")[:3]]
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2], 0)


def write_version_file(name: str) -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    target = WORK / f"{name}_version.txt"
    quad = version_tuple()
    numeric = f"{quad[0]}.{quad[1]}.{quad[2]}.{quad[3]}"
    target.write_text(
        VERSION_TEMPLATE.format(quad=quad, app=APP_NAME, name=name, version=numeric),
        encoding="utf-8",
    )
    return target


def build(name: str, entry: Path, windowed: bool, onedir: bool) -> Path:
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name",
        name,
        "--paths",
        str(ROOT),
        "--distpath",
        str(DIST),
        "--workpath",
        str(WORK),
        "--specpath",
        str(WORK),
        "--icon",
        str(ICON),
        "--add-data",
        f"{ICON.parent}{';' if sys.platform == 'win32' else ':'}assets",
        "--version-file",
        str(write_version_file(name)),
        "--hidden-import",
        "peverything.gui",
        "--onedir" if onedir else "--onefile",
        "--windowed" if windowed else "--console",
        str(entry),
    ]
    print("+ " + " ".join(args), flush=True)
    try:
        subprocess.check_call(args)
    except subprocess.CalledProcessError:
        print(
            f"\n构建 {name} 失败。如果 PyInstaller 报的是 PermissionError / Access is denied，"
            f"通常是 dist\\{name}.exe 还在运行：先关掉它再重试。",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return DIST / (f"{name}.exe" if not onedir else name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--onedir", action="store_true", help="build a folder instead of one file")
    parser.add_argument("--console-only", action="store_true", help="skip the GUI executable")
    parser.add_argument("--gui-only", action="store_true", help="skip the CLI executable")
    args = parser.parse_args()

    ICON.exists() or subprocess.check_call([sys.executable, str(ROOT / "tools" / "make_icon.py")])

    targets: list[tuple[str, Path, bool]] = []
    if not args.gui_only:
        targets.append(("pev", ROOT / "tools" / "entry_pev.py", False))
    if not args.console_only:
        targets.append(("Peverything", ROOT / "tools" / "entry_gui.py", True))

    built: list[Path] = []
    for name, entry, windowed in targets:
        built.append(build(name, entry, windowed, args.onedir))

    print()
    print(f"{APP_NAME} {__version__} 构建完成：")
    for path in built:
        if path.is_dir():
            total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
            print(f"  {path}  ({total / 1024 / 1024:.1f} MB, 目录)")
        elif path.exists():
            print(f"  {path}  ({path.stat().st_size / 1024 / 1024:.1f} MB)")
        else:
            print(f"  {path}  (缺失！)", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
