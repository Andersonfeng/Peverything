"""Tests for the MFT tree reconstruction and the journal path resolution."""

from __future__ import annotations

import unittest

from peverything.scanner import build_paths
from peverything.usn import FILE_ATTRIBUTE_DIRECTORY, ROOT_FILE_REFERENCE, JournalInfo, Volume
from peverything.watcher import ChangeSet, VolumeWatch

DIR = FILE_ATTRIBUTE_DIRECTORY


class BuildPathsTestCase(unittest.TestCase):
    def test_nested_tree(self) -> None:
        names = ["Users", "me", "a.txt", "b.txt"]
        refs = [10, 11, 12, 13]
        parents = [ROOT_FILE_REFERENCE, 10, 11, ROOT_FILE_REFERENCE]
        attrs = [DIR, DIR, 0, 0]
        paths, out_attrs, dir_map = build_paths(names, refs, parents, attrs, "C:\\")
        self.assertEqual(
            sorted(paths),
            sorted(["C:\\Users", "C:\\Users\\me", "C:\\Users\\me\\a.txt", "C:\\b.txt"]),
        )
        self.assertEqual(len(out_attrs), 4)
        self.assertEqual(dir_map[10], "C:\\Users\\")
        self.assertEqual(dir_map[11], "C:\\Users\\me\\")
        self.assertEqual(dir_map[ROOT_FILE_REFERENCE], "C:\\")

    def test_out_of_order_records(self) -> None:
        """A child may be enumerated before its parent; resolution must walk up."""
        names = ["a.txt", "me", "Users"]
        refs = [12, 11, 10]
        parents = [11, 10, ROOT_FILE_REFERENCE]
        attrs = [0, DIR, DIR]
        paths, _attrs, dir_map = build_paths(names, refs, parents, attrs, "C:\\")
        self.assertIn("C:\\Users\\me\\a.txt", paths)
        self.assertEqual(dir_map[12] if 12 in dir_map else None, None)

    def test_orphans_are_dropped(self) -> None:
        names = ["ghost.txt", "real.txt"]
        refs = [20, 21]
        parents = [999, ROOT_FILE_REFERENCE]
        attrs = [0, 0]
        paths, _attrs, _dir_map = build_paths(names, refs, parents, attrs, "D:\\")
        self.assertEqual(paths, ["D:\\real.txt"])

    def test_parent_must_be_a_directory(self) -> None:
        names = ["file", "child.txt"]
        refs = [30, 31]
        parents = [ROOT_FILE_REFERENCE, 30]
        attrs = [0, 0]
        paths, _attrs, _dir_map = build_paths(names, refs, parents, attrs, "C:\\")
        self.assertNotIn("C:\\file\\child.txt", paths)
        self.assertEqual(paths, ["C:\\file"])

    def test_dirs_only(self) -> None:
        names = ["Users", "me", "a.txt"]
        refs = [10, 11, 12]
        parents = [ROOT_FILE_REFERENCE, 10, 11]
        attrs = [DIR, DIR, 0]
        paths, out_attrs, dir_map = build_paths(names, refs, parents, attrs, "C:\\", dirs_only=True)
        self.assertEqual(sorted(paths), ["C:\\Users", "C:\\Users\\me"])
        self.assertEqual(len(out_attrs), 2)
        self.assertIn(11, dir_map)

    def test_cycle_guard(self) -> None:
        """Corrupt records forming a loop must not hang the scan."""
        names = ["loop"]
        refs = [40]
        parents = [40]
        attrs = [DIR]
        paths, _attrs, _dir_map = build_paths(names, refs, parents, attrs, "C:\\")
        self.assertEqual(paths, [])


class WatcherPathTestCase(unittest.TestCase):
    def setUp(self) -> None:
        journal = JournalInfo(1, 0, 0, 0, 0, 0, 0)
        self.watch = VolumeWatch(Volume("C"), {10: "C:\\Users\\", 11: "C:\\Users\\me\\"}, journal)

    def test_root_child(self) -> None:
        self.assertEqual(self.watch._path_of(ROOT_FILE_REFERENCE, "pagefile.sys"), "C:\\pagefile.sys")

    def test_nested_child(self) -> None:
        self.assertEqual(self.watch._path_of(11, "note.txt"), "C:\\Users\\me\\note.txt")

    def test_unknown_parent_is_skipped(self) -> None:
        self.assertIsNone(self.watch._path_of(12345, "note.txt"))

    def test_ntfs_metadata_is_hidden(self) -> None:
        self.assertIsNone(self.watch._path_of(ROOT_FILE_REFERENCE, "$MFT"))

    def test_changeset_helpers(self) -> None:
        changes = ChangeSet(added=[("C:\\a", 0, -1, -1)], removed=["C:\\b"])
        self.assertTrue(changes)
        self.assertEqual(len(changes), 2)
        self.assertIn("+ C:\\a", changes.describe())
        self.assertIn("- C:\\b", changes.describe())
        self.assertFalse(ChangeSet())


if __name__ == "__main__":
    unittest.main()
