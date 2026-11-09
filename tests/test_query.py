"""Tests for query parsing and matching semantics."""

from __future__ import annotations

import time
import unittest
from datetime import datetime

from peverything.index import PathTable
from peverything.query import QueryError, parse_query, run_query, _parse_size_expression
from peverything.usn import FILE_ATTRIBUTE_DIRECTORY

DIR = FILE_ATTRIBUTE_DIRECTORY


def ts(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, 12, 0).timestamp())


RECORDS = [
    ("C:\\Windows\\System32\\kernel32.dll", 0, 900_000, ts(2020, 1, 1)),
    ("C:\\Windows\\explorer.exe", 0, 4_000_000, ts(2021, 5, 5)),
    ("C:\\Users\\me\\Desktop\\report.pdf", 0, 2_000, ts(2024, 3, 1)),
    ("C:\\Users\\me\\Desktop\\notes.txt", 0, 1_000, ts(2024, 3, 2)),
    ("C:\\Users\\me\\Desktop\\Projects", DIR, -1, ts(2024, 2, 1)),
    ("D:\\data\\backup\\report_2024.pdf", 0, 5_000, ts(2024, 6, 1)),
    ("D:\\data\\big.iso", 0, 3_000_000_000, ts(2019, 1, 1)),
    ("D:\\数据\\报表.xlsx", 0, 8_000, ts(2024, 7, 1)),
]


class QueryTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.paths = [item[0] for item in RECORDS]
        cls.table = PathTable.build(
            paths=[item[0] for item in RECORDS],
            attrs=[item[1] for item in RECORDS],
            sizes=[item[2] for item in RECORDS],
            mtimes=[item[3] for item in RECORDS],
            meta={"has_stat": True},
        )

    def search(self, text: str) -> set[str]:
        parsed = parse_query(text)
        self.assertFalse(parsed.warnings, f"unexpected warnings for {text!r}")
        indices = run_query(self.table, parsed)
        return {self.table.path_at(index) for index in indices}

    # ---------------------------------------------------------------- basic ---
    def test_empty_query_matches_everything(self) -> None:
        self.assertEqual(len(self.search("")), len(RECORDS))

    def test_substring_is_case_insensitive(self) -> None:
        self.assertEqual(self.search("REPORT"), self.search("report"))
        self.assertEqual(len(self.search("report")), 2)

    def test_and_semantics(self) -> None:
        found = self.search("report ext:pdf")
        self.assertEqual(len(found), 2)
        self.assertNotIn("D:\\data\\big.iso", found)

    def test_or_semantics(self) -> None:
        self.assertEqual(len(self.search("report|notes")), 3)

    def test_or_and_groups(self) -> None:
        found = self.search("notes|iso")
        self.assertEqual(len(found), 2)
        self.assertIn("D:\\data\\big.iso", found)
        self.assertIn("C:\\Users\\me\\Desktop\\notes.txt", found)

    def test_negation(self) -> None:
        found = self.search("report !2024")
        self.assertEqual(found, {"C:\\Users\\me\\Desktop\\report.pdf"})
        self.assertEqual(len(self.search("!report")), len(RECORDS) - 2)

    def test_quoted_phrase(self) -> None:
        self.assertEqual(self.search('"me\\Desktop"'), self.search("me\\Desktop"))
        self.assertEqual(len(self.search('"report.pdf"')), 1)

    def test_wildcards(self) -> None:
        found = self.search("*.pdf")
        self.assertEqual(len(found), 2)
        self.assertEqual(len(self.search("report_????.pdf")), 1)

    # ------------------------------------------------------------- modifiers ---
    def test_extension_list(self) -> None:
        self.assertEqual(len(self.search("ext:pdf;iso")), 3)
        self.assertEqual(len(self.search("ext:PDF")), 2)
        self.assertEqual(self.search("ext:xyz"), set())

    def test_path_and_nopath(self) -> None:
        found = self.search("path:desktop")
        self.assertEqual(len(found), 3)
        self.assertEqual(self.search("nopath:notes"), {"C:\\Users\\me\\Desktop\\notes.txt"})

    def test_whole_filename(self) -> None:
        self.assertEqual(len(self.search("wfn:*.pdf")), 2)
        self.assertEqual(len(self.search("wfn:report.pdf")), 1)

    def test_whole_filename_ignores_directory(self) -> None:
        table = PathTable.build(["C:\\pdf\\readme.txt", "C:\\docs\\a.pdf"])
        hits = {table.path_at(index) for index in run_query(table, parse_query("wfn:*.pdf"))}
        self.assertEqual(hits, {"C:\\docs\\a.pdf"})

    def test_regex(self) -> None:
        found = self.search(r"regex:report_\d+\.pdf")
        self.assertEqual(found, {"D:\\data\\backup\\report_2024.pdf"})

    def test_regex_anchor_is_per_path(self) -> None:
        self.assertEqual(len(self.search(r"regex:^D:")), 3)

    def test_bad_regex(self) -> None:
        with self.assertRaises(QueryError):
            parse_query("regex:[")

    def test_size(self) -> None:
        self.assertEqual(self.search("size:>1gb"), {"D:\\data\\big.iso"})
        self.assertEqual(len(self.search("size:1kb..10kb")), 3)
        self.assertEqual(len(self.search("size:<2kb")), 2)

    def test_size_expression_parser(self) -> None:
        self.assertEqual(_parse_size_expression(">10mb"), (10 * 1024**2, 1 << 62))
        self.assertEqual(_parse_size_expression("1kb..2kb"), (1024, 2048))
        self.assertEqual(_parse_size_expression("1024"), (1024, 1024))

    def test_date(self) -> None:
        self.assertEqual(len(self.search("dm:2024")), 5)
        self.assertEqual(len(self.search("dm:2024-03")), 2)
        self.assertEqual(len(self.search("dm:2024-03-01..2024-03-31")), 2)

    def test_type(self) -> None:
        self.assertEqual(self.search("folder:"), {"C:\\Users\\me\\Desktop\\Projects"})
        self.assertEqual(len(self.search("file:")), len(RECORDS) - 1)

    def test_parent(self) -> None:
        self.assertEqual(len(self.search("parent:C:\\Users\\me\\Desktop")), 3)

    def test_case_sensitivity(self) -> None:
        self.assertEqual(self.search("case:REPORT"), set())
        self.assertEqual(len(self.search("case:report")), 2)
        self.assertEqual(len(self.search("CASE:Windows")), 2)
        self.assertEqual(len(self.search("case:windows")), 0)

    def test_unicode(self) -> None:
        self.assertEqual(len(self.search("报表")), 1)
        self.assertEqual(len(self.search("数据")), 1)

    # --------------------------------------------------------------- parsing ---
    def test_unknown_modifier_is_literal(self) -> None:
        parsed = parse_query("http://example.com")
        self.assertEqual(len(parsed.groups[0]), 1)
        self.assertEqual(parsed.groups[0][0].kind, "sub")

    def test_quote_keeps_pipe_literal(self) -> None:
        parsed = parse_query('"a|b"')
        self.assertEqual(len(parsed.groups), 1)

    def test_dc_da_warning(self) -> None:
        parsed = parse_query("dc:2024")
        self.assertTrue(parsed.warnings)

    def test_attrib_warning(self) -> None:
        parsed = parse_query("attrib:H")
        self.assertTrue(parsed.warnings)
        self.assertEqual(len(parsed.groups[0]), 0)


class RegexHintTestCase(unittest.TestCase):
    """The literal prefilter must never skip a real match."""

    CASES = [
        (r"report_\d+\.pdf", "report_12.pdf"),
        (r"colou?r", "color"),
        (r"colou?r", "colour"),
        (r"a|b", "b"),
        (r"[abc]x", "bx"),
        (r"(?:foo)bar", "foobar"),
        (r"^D:\\data", "D:\\data\\x"),
        (r"log$", "setup.log"),
        (r"ab*c", "ac"),
        (r"a{0,3}b", "b"),
        (r"x{2,4}y", "xxy"),
        (r"setup.*log", "setup_install.log"),
    ]

    def test_hint_is_always_a_necessary_substring(self) -> None:
        from peverything.query import _regex_hint

        for pattern, sample in self.CASES:
            with self.subTest(pattern=pattern):
                hint = _regex_hint(pattern)
                if hint:
                    self.assertIn(hint.lower(), sample.lower().encode("utf-8"))

    def test_alternation_gets_no_hint(self) -> None:
        from peverything.query import _regex_hint

        self.assertEqual(_regex_hint("foo|bar"), b"")
        self.assertEqual(_regex_hint("[abc]xyz"), b"xyz")
        self.assertEqual(_regex_hint(r"\d{4}\.log"), b".log")


class NoStatTestCase(unittest.TestCase):
    """An MFT index has no size/mtime: those filters must simply not match."""

    def setUp(self) -> None:
        self.table = PathTable.build(["C:\\a.txt", "C:\\b.bin"], meta={"has_stat": False})
        self.parsed = parse_query("size:>1kb")
        self.assertTrue(self.parsed.requires_stat)

    def test_size_filter_matches_nothing_without_stat(self) -> None:
        self.assertEqual(run_query(self.table, self.parsed), [])
        self.assertFalse(self.table.meta["has_stat"])


if __name__ == "__main__":
    unittest.main()
