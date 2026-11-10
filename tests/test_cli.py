"""End-to-end tests through the command line interface."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from peverything.cli import main


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        (self.root / "alpha.txt").write_text("alpha", encoding="utf-8")
        (self.root / "beta.log").write_text("x" * 2048, encoding="utf-8")
        sub = self.root / "nested"
        sub.mkdir()
        (sub / "gamma.txt").write_text("gamma", encoding="utf-8")
        self.index = self.root / "test.pev"
        self._old_env = os.environ.get("PEVERYTHING_INDEX")

    def tearDown(self) -> None:
        self.folder.cleanup()
        if self._old_env is None:
            os.environ.pop("PEVERYTHING_INDEX", None)
        else:
            os.environ["PEVERYTHING_INDEX"] = self._old_env

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_scan_then_search(self) -> None:
        code, out, _err = self.run_cli("scan", str(self.root), "-o", str(self.index), "--quiet")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.index.exists())

        code, out, _err = self.run_cli("search", "alpha", "-o", str(self.index), "--json")
        self.assertEqual(code, 0, out)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["path"].endswith("alpha.txt"))
        self.assertEqual(rows[0]["size"], 5)

    def test_search_without_index_fails(self) -> None:
        code, _out, err = self.run_cli("search", "x", "-o", str(self.index))
        self.assertEqual(code, 1)
        self.assertIn("找不到索引", err)

    def test_search_no_results(self) -> None:
        self.run_cli("scan", str(self.root), "-o", str(self.index), "--quiet")
        code, _out, _err = self.run_cli("search", "no-such-file", "-o", str(self.index))
        self.assertEqual(code, 2)

    def test_filters_end_to_end(self) -> None:
        self.run_cli("scan", str(self.root), "-o", str(self.index), "--quiet")
        code, out, _err = self.run_cli("search", "ext:log size:>1kb", "-o", str(self.index), "--json")
        self.assertEqual(code, 0, out)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["path"].endswith("beta.log"))

    def test_dirs_only(self) -> None:
        self.run_cli("scan", str(self.root), "-o", str(self.index), "--quiet")
        code, out, _err = self.run_cli("search", "nested", "-o", str(self.index), "--json", "--dirs-only")
        self.assertEqual(code, 0, out)
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["is_dir"])

    def test_info(self) -> None:
        self.run_cli("scan", str(self.root), "-o", str(self.index), "--quiet")
        code, out, _err = self.run_cli("info", "-o", str(self.index))
        self.assertEqual(code, 0, out)
        self.assertIn("对象数量", out)

    def test_env_index_path(self) -> None:
        os.environ["PEVERYTHING_INDEX"] = str(self.index)
        code, _out, _err = self.run_cli("scan", str(self.root), "--quiet")
        self.assertEqual(code, 0)
        self.assertTrue(self.index.exists())

    def test_bad_query(self) -> None:
        self.run_cli("scan", str(self.root), "-o", str(self.index), "--quiet")
        code, _out, err = self.run_cli("search", "size:abc", "-o", str(self.index))
        self.assertEqual(code, 1)
        self.assertIn("大小", err)


class GuiEntryTestCase(unittest.TestCase):
    """The windowed exe is double-clicked with no arguments."""

    def setUp(self) -> None:
        from peverything import gui_entry

        self.entry = gui_entry
        self.calls: list[list[str]] = []
        self.original = gui_entry.main
        gui_entry.main = lambda argv: self.calls.append(list(argv)) or 0  # type: ignore[assignment]

    def tearDown(self) -> None:
        self.entry.main = self.original  # type: ignore[assignment]

    def test_no_arguments_means_gui(self) -> None:
        self.entry.main_gui([])
        self.assertEqual(self.calls[-1], ["gui"])

    def test_flags_are_forwarded_to_gui(self) -> None:
        self.entry.main_gui(["--query=ext:pdf"])
        self.assertEqual(self.calls[-1], ["gui", "--query=ext:pdf"])

    def test_existing_subcommand_is_kept(self) -> None:
        self.entry.main_gui(["gui", "-o", "index.pev"])
        self.assertEqual(self.calls[-1], ["gui", "-o", "index.pev"])

    def test_version_is_not_rewritten(self) -> None:
        self.entry.main_gui(["--version"])
        self.assertEqual(self.calls[-1], ["--version"])


if __name__ == "__main__":
    unittest.main()
