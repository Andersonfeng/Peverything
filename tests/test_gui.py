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

        self.root = tk.Tk()
        self.app = PeverythingApp(self.root, self.index_path)
        self.assertTrue(pump(self.root, lambda: self.app.index is not None, timeout=20))

    def tearDown(self) -> None:
        try:
            self.app.close()
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


if __name__ == "__main__":
    unittest.main()
