"""GUI smoke test.

Tk needs a desktop session; on a headless machine the whole class is skipped.
The test drives the real application object: load index, search, render,
sort and copy, all without a human.
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from peverything import util

try:
    import tkinter as tk

    _TK_AVAILABLE = True
except Exception:  # pragma: no cover - no Tk / no display
    _TK_AVAILABLE = False


def pump(root: "tk.Tk", predicate, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if predicate():
            return True
        time.sleep(0.01)
    return False


@unittest.skipUnless(_TK_AVAILABLE, "Tk 不可用")
class GuiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.root_dir = Path(self.folder.name)
        (self.root_dir / "alpha.txt").write_text("alpha", encoding="utf-8")
        (self.root_dir / "beta.log").write_text("x" * 4096, encoding="utf-8")
        nested = self.root_dir / "nested"
        nested.mkdir()
        (nested / "gamma-alpha.txt").write_text("gamma", encoding="utf-8")
        self.index_path = self.root_dir / "gui.pev"

        from peverything.cli import main

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(["scan", str(self.root_dir), "-o", str(self.index_path), "--quiet"]), 0
            )

        from peverything.gui import PeverythingApp
        from peverything.settings import Settings

        self.root = tk.Tk()
        self.app = PeverythingApp(
            self.root,
            self.index_path,
            settings=Settings(close_action="exit", tray_icon=False, hotkey_enabled=False),
            enable_shell=False,
        )
        self.assertTrue(pump(self.root, lambda: self.app.index is not None, timeout=20))

    def tearDown(self) -> None:
        try:
            self.app.quit_app()
        finally:
            self.folder.cleanup()

    def search(self, text: str) -> None:
        self.app.count_label.config(text="")
        self.app.search_var.set(text)
        self.app._run_search_now()
        self.assertTrue(pump(self.root, lambda: bool(self.app.count_label.cget("text"))))

    def test_index_is_shown(self) -> None:
        self.assertIn("索引", self.app.status_label.cget("text"))

    def test_search_and_render(self) -> None:
        self.search("alpha")
        self.assertEqual(len(self.app.rows), 2)
        self.assertEqual(len(self.app.tree.get_children()), 2)
        self.assertIn("alpha", self.app.tree.item(self.app.tree.get_children()[0], "values")[0])

    def test_empty_result(self) -> None:
        self.search("no-such-name")
        self.assertEqual(self.app.rows, [])
        self.assertEqual(self.app.tree.get_children(), ())

    def test_sorting(self) -> None:
        self.search("alpha")
        self.app.sort_by("size")
        sizes = [row.size for row in self.app.rows]
        self.assertEqual(sizes, sorted(sizes))
        self.app.sort_by("size")
        self.assertEqual([row.size for row in self.app.rows], sorted(sizes, reverse=True))

    def test_copy_path(self) -> None:
        self.search("alpha")
        self.app.tree.selection_set(self.app.tree.get_children()[0])
        self.app.copy_path()
        self.assertTrue(self.root.clipboard_get().endswith(".txt"))

    def test_shortcut_bindings_exist(self) -> None:
        # Tk canonicalises "<Return>" to "<Key-Return>" in the bind table.
        tree_sequences = self.app.tree.bind()
        entry_sequences = self.app.search_entry.bind()
        for sequence in ("<Key-Return>", "<Control-Key-Return>", "<Alt-Key-Return>"):
            self.assertIn(sequence, tree_sequences, f"tree is missing {sequence}")
            self.assertIn(sequence, entry_sequences, f"search box is missing {sequence}")

    def test_enter_modifiers_do_not_open_the_file(self) -> None:
        """Ctrl+Enter used to open the file, because Tk also fires `<Return>`."""
        self.search("alpha")
        self.app.tree.selection_set(self.app.tree.get_children()[0])
        opened: list[str] = []
        revealed: list[str] = []
        properties: list[str] = []

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(util, "open_path", opened.append))
            stack.enter_context(mock.patch.object(util, "reveal_path", revealed.append))
            stack.enter_context(
                mock.patch.object(util, "show_properties", lambda path: properties.append(path) or True)
            )

            ctrl = SimpleNamespace(state=0x0004)
            alt = SimpleNamespace(state=0x0008)
            plain = SimpleNamespace(state=0)

            self.assertEqual(self.app._on_open_key(ctrl), "break")
            self.assertEqual(opened, [], "Ctrl+Enter must not open the file")

            self.assertEqual(self.app._on_reveal_key(ctrl), "break")
            self.assertEqual(len(revealed), 1)

            self.assertEqual(self.app._on_properties_key(alt), "break")
            self.assertEqual(len(properties), 1)

            self.app._on_open_key(plain)
            self.assertEqual(len(opened), 1)

    def test_hotkey_toggles_window(self) -> None:
        self.search("alpha")
        self.app._on_hotkey()
        self.root.update()
        self.assertEqual(self.root.state(), "withdrawn")
        self.app._on_hotkey()
        self.root.update()
        self.assertNotEqual(self.root.state(), "withdrawn")

    def test_close_action_exit(self) -> None:
        """With close_action=exit the X button really quits (no tray)."""
        self.app.close()
        with self.assertRaises(tk.TclError):
            self.root.winfo_exists()
        # quit_app is idempotent: tearDown runs again without raising.
        self.app.quit_app()


class AssetPathTestCase(unittest.TestCase):
    """The frozen exe must find the icon, or every icon falls back to Tk's."""

    def test_source_layout(self) -> None:
        from peverything.gui import asset_path

        self.assertTrue(asset_path("peverything.ico").exists())

    def test_bundle_layouts(self) -> None:
        import sys as _sys

        from peverything import gui

        for layout in ("assets", "peverything/assets"):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                target = root / layout / "peverything.ico"
                target.parent.mkdir(parents=True)
                target.write_bytes(b"ico")
                previous = getattr(_sys, "_MEIPASS", None)
                _sys._MEIPASS = str(root)  # type: ignore[attr-defined]
                try:
                    self.assertEqual(gui.asset_path("peverything.ico"), target)
                finally:
                    if previous is None:
                        del _sys._MEIPASS  # type: ignore[attr-defined]
                    else:
                        _sys._MEIPASS = previous  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
