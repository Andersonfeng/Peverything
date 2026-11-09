"""Tests for the path table and the incremental delta layer."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from peverything.index import IndexFormatError, PathTable, SearchIndex, glob_to_regex
from peverything.query import parse_query
from peverything.usn import FILE_ATTRIBUTE_DIRECTORY

PATHS = [
    "C:\\Windows\\System32\\drivers\\etc\\hosts",
    "C:\\Windows\\notepad.exe",
    "C:\\Users\\alice\\Documents\\thesis.docx",
    "C:\\Users\\alice\\Downloads\\thesis-final.docx",
    "D:\\Media\\Movies\\movie.mkv",
    "D:\\Media\\Music\\song.flac",
]


class PathTableTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.table = PathTable.build(PATHS, meta={"source": "test"})

    def test_sorted_and_counted(self) -> None:
        self.assertEqual(len(self.table), len(PATHS))
        paths = [self.table.path_at(index) for index in range(len(self.table))]
        self.assertEqual(paths, sorted(PATHS))

    def test_find_substr_is_ascending(self) -> None:
        hits = list(self.table.find_substr(b"thesis"))
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits, sorted(hits))

    def test_find_substr_is_case_insensitive_by_default(self) -> None:
        self.assertEqual(len(list(self.table.find_substr(b"THESIS"))), 2)
        self.assertEqual(len(list(self.table.find_substr(b"THESIS", case=True))), 0)

    def test_find_glob(self) -> None:
        self.assertEqual(len(list(self.table.find_glob(b"*.docx"))), 2)

    def test_glob_to_regex_does_not_cross_paths(self) -> None:
        pattern = glob_to_regex("a*b")
        self.assertIsNotNone(pattern.search(b"aXXb"))
        self.assertIsNone(pattern.search(b"a\nb"))

    def test_save_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "index.pev"
            self.table.save(target)
            loaded = PathTable.load(target)
            self.assertEqual(loaded.count, self.table.count)
            self.assertEqual(loaded.meta["source"], "test")
            for index in range(loaded.count):
                self.assertEqual(loaded.path_at(index), self.table.path_at(index))

    def test_reject_foreign_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "bogus.pev"
            target.write_bytes(b"NOPE" + b"\x00" * 32)
            with self.assertRaises(IndexFormatError):
                PathTable.load(target)

    def test_directory_attribute(self) -> None:
        table = PathTable.build(["C:\\folder", "C:\\file.txt"], [FILE_ATTRIBUTE_DIRECTORY, 0])
        rows = {row.name: row for row in table.iter_rows()}
        self.assertTrue(rows["folder"].is_dir)
        self.assertFalse(rows["file.txt"].is_dir)
        self.assertEqual(rows["file.txt"].ext, "txt")


class SearchIndexTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.base = PathTable.build(PATHS)
        self.index = SearchIndex(self.base)

    def search(self, text: str, limit: int | None = None) -> set[str]:
        return {row.path for row in self.index.search(parse_query(text), limit)}

    def test_delta_add(self) -> None:
        self.index.apply([("C:\\Users\\alice\\new-file.txt", 0, 10, 123)])
        found = self.search("new-file")
        self.assertEqual(found, {"C:\\Users\\alice\\new-file.txt"})
        self.assertEqual(self.index.count, len(PATHS) + 1)

    def test_delta_remove(self) -> None:
        self.index.apply(removes=["C:\\Windows\\notepad.exe"])
        self.assertEqual(self.search("notepad"), set())
        self.assertEqual(len(self.search("thesis")), 2)

    def test_add_then_remove(self) -> None:
        self.index.apply([("C:\\temp\\x.tmp", 0, 1, 1)])
        self.index.apply(removes=["C:\\temp\\x.tmp"])
        self.assertEqual(self.search("x.tmp"), set())

    def test_compaction_keeps_state(self) -> None:
        self.index.apply([("C:\\Users\\alice\\added.log", 0, 5, 5)], ["C:\\Windows\\notepad.exe"])
        self.index.compact()
        self.assertEqual(self.index.pending_count, 0)
        self.assertEqual(self.index.removed_count, 0)
        self.assertIn("C:\\Users\\alice\\added.log", self.search("added"))
        self.assertNotIn("C:\\Windows\\notepad.exe", self.search("notepad"))

    def test_limit_is_respected(self) -> None:
        rows = self.index.search(parse_query(""), limit=3)
        self.assertEqual(len(rows), 3)

    def test_remove_unmatched_is_harmless(self) -> None:
        self.index.apply(removes=["C:\\does\\not\\exist"])
        self.assertEqual(len(self.search("")), len(PATHS))

    def test_save_compacts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "index.pev"
            self.index.apply([("D:\\Media\\Movies\\extra.mkv", 0, 1, 1)])
            self.index.save(target)
            reloaded = SearchIndex.load(target)
            self.assertEqual(reloaded.count, len(PATHS) + 1)
            self.assertEqual(
                {row.path for row in reloaded.search(parse_query("mkv"))},
                {"D:\\Media\\Movies\\movie.mkv", "D:\\Media\\Movies\\extra.mkv"},
            )


if __name__ == "__main__":
    unittest.main()
