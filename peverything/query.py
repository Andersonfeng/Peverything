"""Everything-compatible-ish query language.

Supported syntax (a practical subset of Everything's search syntax):

===================  =========================================================
``foo bar``          both terms must match (space = AND)
``foo|bar``          either term matches (``|`` = OR, splits the whole query)
``!foo``             does not match
``"foo bar"``        quoted literal
``*`` ``?``          wildcards, on the whole path
``ext:py;js``        extension list
``path:temp``        the *directory* part contains ``temp``
``nopath:readme``    only the file name is searched
``wfn:*.txt``        whole-file-name wildcard match
``regex:^C:\\Temp``  regular expression, matched per path
``size:>10mb``       ``size:<1kb``, ``size:1kb..10mb``, ``size:1024``
``dm:today``         date modified: ``today``/``yesterday``/``thisweek``/
                     ``thismonth``/``thisyear``/``2024``/``2024-05``/
                     ``2024-05-01``/``2024-05-01..2024-06-01``
``dc:`` ``da:``      accepted as aliases of ``dm:`` (we only store mtime)
``file:`` ``folder:`` filter by type
``parent:C:\\Windows`` the containing directory is exactly this
``case:``            turn on case sensitivity for the rest of the query
===================  =========================================================
"""

from __future__ import annotations

import heapq
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterator

from .index import PathTable, glob_to_regex
from .usn import FILE_ATTRIBUTE_DIRECTORY

MODIFIERS = {
    "ext",
    "size",
    "dm",
    "dc",
    "da",
    "date",
    "path",
    "nopath",
    "case",
    "regex",
    "wfn",
    "file",
    "folder",
    "type",
    "parent",
    "attrib",
}

_SIZE_UNITS = {
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "p": 1024**5,
    "pb": 1024**5,
}


class QueryError(ValueError):
    """The query cannot be parsed."""


class Term:
    __slots__ = ("kind", "value", "negate", "text", "pattern", "needle", "needles")

    def __init__(self, kind: str, value: object, negate: bool = False, text: str = "") -> None:
        self.kind = kind
        self.value = value
        self.negate = negate
        self.text = text
        self.pattern: re.Pattern[bytes] | None = None
        self.needle: bytes = b""
        self.needles: list[bytes] = []

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Term({self.kind!r}, {self.value!r}, negate={self.negate})"


@dataclass
class ParsedQuery:
    groups: list[list[Term]] = field(default_factory=list)
    case: bool = False
    warnings: list[str] = field(default_factory=list)
    raw: str = ""

    @property
    def is_empty(self) -> bool:
        return not any(self.groups)

    @property
    def requires_stat(self) -> bool:
        return any(t.kind in ("size", "date") and not t.negate for group in self.groups for t in group)


# ------------------------------------------------------------------ tokens ---
def _tokenize(text: str) -> list[tuple[str, bool]]:
    tokens: list[tuple[str, bool]] = []
    current: list[str] = []
    quoted = False
    index = 0
    length = len(text)

    def flush() -> None:
        nonlocal current, quoted
        if current:
            tokens.append(("".join(current), quoted))
            current = []
        quoted = False

    while index < length:
        char = text[index]
        if char == '"':
            index += 1
            quoted = True
            start = index
            while index < length and text[index] != '"':
                index += 1
            current.extend(text[start:index])
            if index < length:
                index += 1
            flush()
            continue
        if char == "|":
            flush()
            tokens.append(("|", False))
            index += 1
            continue
        if char.isspace():
            flush()
            index += 1
            continue
        current.append(char)
        index += 1
    flush()
    return tokens


# ------------------------------------------------------------------ parsing ---
def parse_query(text: str) -> ParsedQuery:
    parsed = ParsedQuery(raw=text)
    tokens = _tokenize(text)
    if not tokens:
        parsed.groups = [[]]
        return parsed

    groups: list[list[Term]] = [[]]
    for token, quoted in tokens:
        if token == "|" and not quoted:
            groups.append([])
            continue
        term = _parse_term(token, quoted, parsed)
        if term is not None:
            groups[-1].append(term)
    parsed.groups = [group for group in groups] or [[]]
    _finalize(parsed)
    return parsed


def _encode(text: str, case: bool) -> bytes:
    return (text if case else text.lower()).encode("utf-8", "surrogatepass")


def _finalize(parsed: ParsedQuery) -> None:
    """Resolve text terms to bytes once the global ``case:`` flag is known."""
    flags = re.MULTILINE | (0 if parsed.case else re.IGNORECASE)
    for group in parsed.groups:
        for term in group:
            if term.kind in ("sub", "dirpath", "name", "parent"):
                term.needle = _encode(str(term.value), parsed.case)
            elif term.kind == "ext":
                term.value = tuple(item.encode("utf-8") for item in term.value)  # type: ignore[arg-type]
                term.needles = [b"." + item for item in term.value]  # type: ignore[union-attr]
            elif term.kind == "glob":
                term.pattern = glob_to_regex(str(term.value), case_sensitive=parsed.case)
                term.needle = _glob_hint(str(term.value))
            elif term.kind == "nameglob":
                term.pattern = glob_to_regex(str(term.value), case_sensitive=parsed.case)
                term.needle = _glob_hint(str(term.value))
            elif term.kind == "regex":
                term.pattern = re.compile(str(term.value).encode("utf-8"), flags)
                term.needle = _regex_hint(str(term.value))


WILDCARD_BYTES = b"*?"


def _glob_hint(pattern: str) -> bytes:
    """Longest wildcard-free run of a glob, used as a cheap prefilter."""
    best = b""
    current = bytearray()
    for char in pattern.encode("utf-8"):
        if char in WILDCARD_BYTES:
            if len(current) > len(best):
                best = bytes(current)
            current.clear()
        else:
            current.append(char)
    if len(current) > len(best):
        best = bytes(current)
    return best if len(best) >= 2 else b""


_REGEX_SPECIAL = set("^$*+?{}[]()|")


def _regex_hint(pattern: str) -> bytes:
    """Longest run of characters that every match of *pattern* must contain.

    Used to turn ``regex:report_\\d+`` into a ``bytes.find`` prefilter, so a plain
    regex scan never has to try a match at every byte of the index.  The
    function is deliberately conservative: alternation, groups and character
    classes make a literal run non-necessary, so those patterns simply get no
    hint (and a slower, always correct, full scan).
    """
    if "(?" in pattern[:8] or "|" in pattern.replace("\\|", ""):
        return b""
    best = b""
    current = bytearray()
    index = 0
    length = len(pattern)

    def flush() -> None:
        nonlocal best
        if len(current) > len(best):
            best = bytes(current)
        current.clear()

    while index < length:
        char = pattern[index]
        if char == "\\":
            if index + 1 >= length:
                break
            escaped = pattern[index + 1]
            # \. \\ \- are literal; \d \w \b \n etc. are classes/anchors.
            if escaped in ".^$*+?{}[]()|\\/-_":
                current.append(ord(escaped))
                index += 2
                continue
            flush()
            index += 2
            continue
        if char in "()":
            # A group may be optional or repeated as a whole.
            return b""
        if char == "[":
            # A character class matches one of several characters.
            closing = pattern.find("]", index + 1)
            if closing < 0:
                return b""
            flush()
            index = closing + 1
            continue
        if char == "{":
            closing = pattern.find("}", index + 1)
            if closing < 0:
                return b""
            minimum = pattern[index + 1 : closing].split(",")[0].strip()
            if minimum in ("", "0") and current:
                current.pop()  # `x{0,3}` makes the last literal optional
            flush()
            index = closing + 1
            continue
        if char in "?*":
            if current:
                current.pop()  # `x?` / `x*` makes the last literal optional
            flush()
            index += 1
            continue
        if char in _REGEX_SPECIAL:
            flush()
            index += 1
            continue
        current.extend(char.encode("utf-8"))
        index += 1
    flush()
    return best if len(best) >= 2 else b""


def _parse_term(token: str, quoted: bool, parsed: ParsedQuery) -> Term | None:
    negate = False
    if not quoted and token.startswith("!"):
        negate = True
        token = token[1:]
    if not token:
        return None

    modifier = ""
    value = token
    if not quoted:
        match = re.match(r"^([A-Za-z]+):(.*)$", token, re.DOTALL)
        if match and match.group(1).lower() in MODIFIERS:
            modifier = match.group(1).lower()
            value = match.group(2)

    if not modifier:
        if "*" in token or "?" in token:
            term = Term("glob", token, negate, token)
            term.pattern = glob_to_regex(token)
            return term
        return Term("sub", token, negate, token)

    if modifier == "case":
        # `case:` is a switch; `case:foo` also switches and keeps `foo` as a term.
        parsed.case = True
        return _parse_term(value, quoted, parsed) if value else None

    if modifier == "ext":
        exts = [item.strip().lstrip(".").lower() for item in re.split(r"[;,]", value) if item.strip()]
        if not exts:
            raise QueryError("ext: 后面需要一个扩展名，例如 ext:py;js")
        return Term("ext", tuple(exts), negate, token)

    if modifier == "size":
        low, high = _parse_size_expression(value)
        return Term("size", (low, high), negate, token)

    if modifier in ("dm", "dc", "da", "date"):
        if modifier in ("dc", "da"):
            parsed.warnings.append("dc:/da: 暂不支持，已按修改时间 dm: 处理")
        low, high = _parse_date_expression(value)
        return Term("date", (low, high), negate, token)

    if modifier == "path":
        if not value:
            raise QueryError("path: 后面需要一个目录片段")
        return Term("dirpath", value, negate, token)

    if modifier == "nopath":
        if not value:
            raise QueryError("nopath: 后面需要一个文件名片段")
        return Term("name", value, negate, token)

    if modifier == "wfn":
        term = Term("nameglob", value, negate, token)
        term.pattern = glob_to_regex(value)
        return term

    if modifier == "regex":
        if not value:
            raise QueryError("regex: 后面需要一个正则表达式")
        term = Term("regex", value, negate, token)
        try:
            term.pattern = re.compile(value.encode("utf-8"), re.MULTILINE)
        except re.error as exc:
            raise QueryError(f"正则表达式无效：{exc}") from exc
        return term

    if modifier in ("file", "folder", "type"):
        lowered = (value or modifier).strip().lower()
        if modifier == "folder" or lowered in ("folder", "dir", "directory"):
            return Term("type", "folder", negate, token)
        if modifier == "file" or lowered in ("file", "files"):
            return Term("type", "file", negate, token)
        raise QueryError(f"无法识别的类型：{value!r}，请使用 type:file 或 type:folder")

    if modifier == "parent":
        if not value:
            raise QueryError("parent: 后面需要一个目录路径")
        return Term("parent", value.rstrip("\\"), negate, token)

    if modifier == "attrib":
        parsed.warnings.append("attrib: 暂不支持，已忽略该条件")
        return None

    return Term("sub", token, negate, token)


def _parse_size_expression(value: str) -> tuple[int, int]:
    expression = value.strip().lower().replace(" ", "")
    if not expression:
        raise QueryError("size: 需要一个大小，例如 size:>10mb")

    def parse_one(text: str) -> int:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)([a-z]*)", text)
        if not match:
            raise QueryError(f"无法解析大小：{text!r}")
        number = float(match.group(1))
        unit = match.group(2) or "b"
        if unit not in _SIZE_UNITS:
            raise QueryError(f"未知的大小单位：{unit!r}")
        return int(number * _SIZE_UNITS[unit])

    if ".." in expression:
        left, _, right = expression.partition("..")
        return parse_one(left) if left else 0, parse_one(right) if right else (1 << 62)
    for prefix in (">=", "<=", ">", "<", "="):
        if expression.startswith(prefix):
            bound = parse_one(expression[len(prefix) :])
            if prefix in (">", ">="):
                return bound, 1 << 62
            if prefix in ("<", "<="):
                return 0, bound
            return bound, bound
    exact = parse_one(expression)
    return exact, exact


def _start_of_day(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _parse_date_expression(value: str) -> tuple[int, int]:
    expression = value.strip().lower()
    if not expression:
        raise QueryError("dm: 需要一个日期，例如 dm:today")
    now = datetime.now()

    keyword = {
        "now": (now - timedelta(minutes=1), now + timedelta(minutes=1)),
        "today": (_start_of_day(now), _start_of_day(now) + timedelta(days=1)),
        "yesterday": (_start_of_day(now) - timedelta(days=1), _start_of_day(now)),
    }
    if expression in keyword:
        low, high = keyword[expression]
        return int(low.timestamp()), int(high.timestamp())
    if expression == "thisweek":
        start = _start_of_day(now) - timedelta(days=now.weekday())
        return int(start.timestamp()), int((start + timedelta(days=7)).timestamp())
    if expression == "thismonth":
        start = _start_of_day(now.replace(day=1))
        next_month = (start + timedelta(days=32)).replace(day=1)
        return int(start.timestamp()), int(next_month.timestamp())
    if expression == "thisyear":
        start = _start_of_day(now.replace(month=1, day=1))
        return int(start.timestamp()), int(start.replace(year=start.year + 1).timestamp())

    def parse_one(text: str) -> tuple[int, int]:
        text = text.strip()
        for fmt, step in (
            ("%Y-%m-%d %H:%M", "minute"),
            ("%Y-%m-%d", "day"),
            ("%Y-%m", "month"),
            ("%Y", "year"),
        ):
            try:
                moment = datetime.strptime(text, fmt)
            except ValueError:
                continue
            start = _start_of_day(moment)
            if step == "minute":
                return int(moment.timestamp()), int((moment + timedelta(minutes=1)).timestamp())
            if step == "day":
                return int(start.timestamp()), int((start + timedelta(days=1)).timestamp())
            if step == "month":
                nxt = (start + timedelta(days=32)).replace(day=1)
                return int(start.timestamp()), int(nxt.timestamp())
            return int(start.timestamp()), int(start.replace(year=start.year + 1).timestamp())
        raise QueryError(f"无法解析日期：{text!r}")

    if ".." in expression:
        left, _, right = expression.partition("..")
        low = parse_one(left)[0] if left else 0
        high = parse_one(right)[1] if right else (1 << 62)
        return low, high
    return parse_one(expression)


# ------------------------------------------------------------------ search ---
def _dir_of(path: bytes) -> bytes:
    cut = path.rfind(b"\\")
    return path[:cut] if cut >= 0 else b""


def _name_of(path: bytes) -> bytes:
    cut = path.rfind(b"\\")
    return path[cut + 1 :] if cut >= 0 else path


def _sub_term(needle: bytes) -> Term:
    term = Term("sub", needle)
    term.needle = needle
    return term


def _driver(parsed: ParsedQuery, group: list[Term]) -> tuple[str, Term, bool] | None:
    """Pick the cheapest positive term to enumerate, or None for a full scan.

    The third element says whether the enumeration is *exact* (every candidate
    really matches the term) or only a prefilter that must be verified.
    """
    for term in group:
        if term.negate:
            continue
        if term.kind == "sub":
            return ("sub", term, True)
        if term.kind == "nameglob":
            # Matched against the file name only, but the full path is a safe
            # superset, so it can still drive the enumeration as a prefilter.
            return ("hint", term, False) if term.needle else ("scan", term, False)
        if term.kind in ("glob", "regex"):
            if term.needle:
                return ("hint", term, False)
            return ("scan", term, True)
    for term in group:
        if term.negate:
            continue
        if term.kind == "ext":
            if not term.needles:
                continue
            if len(term.needles) == 1:
                return ("sub", _sub_term(term.needles[0]), False)
            return ("subs", term, False)
        if term.kind in ("dirpath", "name"):
            return ("sub", _sub_term(term.needle), False)
    return None


def _iter_candidates(
    table: PathTable, parsed: ParsedQuery, driver: tuple[str, Term, bool] | None
) -> Iterator[int]:
    if driver is None:
        yield from range(len(table))
        return
    kind, term, _exact = driver
    if kind == "sub":
        yield from table.find_substr(term.needle, case=parsed.case)
        return
    if kind == "hint":
        yield from table.find_substr(term.needle, case=parsed.case)
        return
    if kind == "subs":
        previous = -1
        for index in heapq.merge(*(table.find_substr(needle, case=parsed.case) for needle in term.needles)):
            if index != previous:
                previous = index
                yield index
        return
    if kind == "scan":
        yield from table.find_regex(term.pattern, case=parsed.case)  # type: ignore[arg-type]


def _match_term(
    table: PathTable,
    index: int,
    raw: bytes,
    low: bytes,
    case: bool,
    term: Term,
) -> bool:
    kind = term.kind
    text = raw if case else low

    if kind == "sub":
        return term.needle in text
    if kind == "glob" or kind == "regex":
        assert term.pattern is not None
        return term.pattern.search(text) is not None
    if kind == "ext":
        dot = low.rfind(b".")
        if dot < 0:
            return False
        return low[dot + 1 :] in term.value  # type: ignore[operator]
    if kind == "dirpath":
        return term.needle in _dir_of(text)
    if kind == "name":
        return term.needle in _name_of(text)
    if kind == "nameglob":
        assert term.pattern is not None
        return term.pattern.search(_name_of(text)) is not None
    if kind == "parent":
        return _dir_of(text) == term.needle
    if kind == "type":
        is_dir = bool(table.attrs[index] & FILE_ATTRIBUTE_DIRECTORY)
        return is_dir if term.value == "folder" else not is_dir
    if kind == "size":
        size = table.sizes[index]
        if size < 0:
            return False
        low_bound, high_bound = term.value  # type: ignore[misc]
        return low_bound <= size <= high_bound
    if kind == "date":
        mtime = table.mtimes[index]
        if mtime < 0:
            return False
        low_bound, high_bound = term.value  # type: ignore[misc]
        return low_bound <= mtime < high_bound
    return False


def _iter_group(table: PathTable, parsed: ParsedQuery, group: list[Term]) -> Iterator[int]:
    driver = _driver(parsed, group)
    exact = bool(driver and driver[2])
    driver_term = driver[1] if driver is not None else None
    checks = [term for term in group if not (exact and term is driver_term)]
    if not checks:
        yield from _iter_candidates(table, parsed, driver)
        return

    needs_bytes = any(
        term.kind in ("sub", "glob", "regex", "ext", "dirpath", "name", "nameglob", "parent")
        for term in checks
    )
    for index in _iter_candidates(table, parsed, driver):
        if needs_bytes:
            raw, low = table.row_bytes(index)
        else:
            raw = low = b""
        matched = True
        for term in checks:
            positive = _match_term(table, index, raw, low, parsed.case, term)
            if positive == term.negate:
                matched = False
                break
        if matched:
            yield index


def iter_query(table: PathTable, parsed: ParsedQuery) -> Iterator[int]:
    """Yield matching row indices in ascending path order."""
    streams = [_iter_group(table, parsed, group) for group in parsed.groups]
    if not streams:
        return
    if len(streams) == 1:
        yield from streams[0]
        return
    previous = -1
    for index in heapq.merge(*streams):
        if index != previous:
            previous = index
            yield index


def run_query(table: PathTable, parsed: ParsedQuery, limit: int | None = None) -> list[int]:
    result: list[int] = []
    for index in iter_query(table, parsed):
        result.append(index)
        if limit is not None and len(result) >= limit:
            break
    return result
