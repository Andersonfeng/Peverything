"""Tests for the raw USN structures.

The parsing runs against synthetic buffers, so these pass without an NTFS
volume and without administrator rights.
"""

from __future__ import annotations

import struct
import unittest

from peverything.usn import (
    FILE_ATTRIBUTE_DIRECTORY,
    JOURNAL_DATA,
    MFT_ENUM_DATA,
    READ_USN_JOURNAL_DATA,
    USN_RECORD,
    USN_RECORD_SIZE,
    parse_usn_records,
)


def make_record(
    name: str,
    frn: int = 42,
    parent: int = 5,
    attributes: int = 0,
    reason: int = 0x100,
    usn: int = 100,
    major: int = 2,
) -> bytes:
    encoded = name.encode("utf-16-le")
    header = USN_RECORD.pack(
        USN_RECORD_SIZE + len(encoded),
        major,
        0,
        frn,
        parent,
        usn,
        132_000_000_000_000_000,
        reason,
        0,
        0,
        attributes,
        len(encoded),
        USN_RECORD_SIZE,
    )
    return header + encoded


class StructLayoutTestCase(unittest.TestCase):
    def test_sizes_match_the_windows_sdk(self) -> None:
        self.assertEqual(USN_RECORD_SIZE, 60)
        self.assertEqual(JOURNAL_DATA.size, 56)
        self.assertEqual(MFT_ENUM_DATA.size, 24)
        self.assertEqual(READ_USN_JOURNAL_DATA.size, 40)


class ParseRecordsTestCase(unittest.TestCase):
    def test_single_record(self) -> None:
        blob = make_record("hello.txt", frn=99, parent=7, attributes=0, usn=555)
        records = list(parse_usn_records(blob))
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.name, "hello.txt")
        self.assertEqual(record.frn, 99)
        self.assertEqual(record.parent_frn, 7)
        self.assertEqual(record.usn, 555)
        self.assertFalse(record.is_dir)
        self.assertTrue(record.is_created)

    def test_directory_attribute(self) -> None:
        blob = make_record("folder", attributes=FILE_ATTRIBUTE_DIRECTORY)
        record = next(parse_usn_records(blob))
        self.assertTrue(record.is_dir)

    def test_multiple_records_with_offset(self) -> None:
        first = make_record("a.txt", frn=1)
        second = make_record("b.txt", frn=2)
        blob = b"\x00" * 8 + first + second  # 8 byte next-USN header
        records = list(parse_usn_records(blob, 8, len(blob)))
        self.assertEqual([record.name for record in records], ["a.txt", "b.txt"])

    def test_unicode_name(self) -> None:
        blob = make_record("报表 2024.xlsx")
        record = next(parse_usn_records(blob))
        self.assertEqual(record.name, "报表 2024.xlsx")

    def test_v3_records_are_skipped(self) -> None:
        blob = make_record("x.txt", major=3) + make_record("y.txt", frn=9)
        records = list(parse_usn_records(blob))
        self.assertEqual([record.name for record in records], ["y.txt"])

    def test_truncated_buffer_is_ignored(self) -> None:
        blob = make_record("complete.txt") + b"\x01\x02\x03"
        records = list(parse_usn_records(blob))
        self.assertEqual(len(records), 1)

    def test_zero_length_record_stops_parsing(self) -> None:
        blob = struct.pack("<IHHQQqqIIIIHH", 0, 2, 0, 1, 5, 1, 0, 0, 0, 0, 0, 0, 60)
        self.assertEqual(list(parse_usn_records(blob)), [])

    def test_reason_flags(self) -> None:
        deleted = next(parse_usn_records(make_record("gone.txt", reason=0x200)))
        self.assertTrue(deleted.is_deleted)
        renamed = next(parse_usn_records(make_record("old.txt", reason=0x1000)))
        self.assertTrue(renamed.is_rename_old)
        self.assertFalse(renamed.is_created)


class VolumeTestCase(unittest.TestCase):
    def test_drive_letter_normalisation(self) -> None:
        from peverything.usn import Volume

        self.assertEqual(Volume("c:").root, "C:\\")
        self.assertEqual(Volume("c:\\").drive, "C")
        with self.assertRaises(ValueError):
            Volume("")

    def test_open_without_admin_is_reported(self) -> None:
        """On this CI box we are not elevated; the error must be actionable."""
        import ctypes

        from peverything.usn import UsnError, Volume

        if ctypes.windll.shell32.IsUserAnAdmin():
            self.skipTest("running elevated")
        with self.assertRaises(UsnError) as caught:
            Volume("C").open()
        self.assertIn("管理员", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
