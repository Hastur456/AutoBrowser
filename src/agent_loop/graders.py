"""Generic graders of a final answer against a Golden Set ``expected`` mapping.

Only the text of the answer is graded, with no knowledge of sites or pages: the same checks
work for any task. Each key of ``expected`` is one check (§4.3.4 of
docs/development/2026-10-19-autonomous-test-pipeline-design.md):

* ``contains: str | list[str]`` — every substring is present (case-insensitive);
* ``not_contains: str | list[str]`` — none is present (case-insensitive);
* ``regex: str | list[str]`` — every pattern matches (``re.search``; use ``(?i)`` for case);
* ``items`` / ``min_items`` / ``max_items: int`` — the number of items. A numbered answer
  (a run of at least two) counts its longest numbering run 1, 2, 3, … (``1.``, ``1)``,
  ``**1.**``, ``### 1.``), so bullets under an item, text between items and a notes list do
  not change it. Otherwise the largest bullet list counts its top-level items (bullets
  indented under a single numbered item are its details). The body rows of the largest
  markdown table count too (the larger figure wins). Code blocks and horizontal rules are
  ignored. Limits: unnumbered attribute bullets of one item look like items, so pair an item
  count with a per-item check (a ``regex`` repeated N times, say); bullet groups split by
  bold names or other unindented lines count as separate lists, so they undercount;
* ``numbers_at_most`` / ``numbers_at_least: number`` — every number in the answer is within
  the bound, and there is at least one. ``120 000``, ``1,299.99`` and ``1 299,99`` are single
  numbers, ``-15`` is negative, list ordinals and URLs are not numbers. Product codes, old
  prices and other digits in the text still count, and ``3 100 000`` is one number.

A key without a grader (``fields``, say) is ``ungraded``: reported, never a failure. An empty
``expected`` grades as ``ungraded``, and an empty string or list as an expectation is invalid.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

CheckStatus = Literal["passed", "failed", "ungraded"]

_BULLET = re.compile(r"^(?P<indent>[ \t]*)[-*+]\s+\S")
#: A numbered item: ``1.``, ``1)``, bold ``**1.**``/``**1. Name**``, or a ``### 1.`` heading.
_NUMBERED = re.compile(
    r"^(?P<indent>[ \t]*)(?:#{1,6}[ \t]+)?(?:\*\*|__)?(?P<number>\d+)[.)](?:\*\*|__)?(?:\s|$)"
)
_FENCE = re.compile(r"^[ \t]*(?:```|~~~)")
_RULE = re.compile(r"^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
#: A cell boundary: a pipe not escaped as ``\|``.
_CELL_PIPE = re.compile(r"(?<!\\)\|")
#: A URL with a scheme, ``www.``, a bare domain followed by a path (``ozon.ru/product/…``),
#: or a site-relative path (``/product/item-123/``).
_URL = re.compile(
    r"(?:https?://|www\.)\S+"
    r"|\b[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}/\S*"
    r"|(?<![\w/])/[\w.-]+/\S*",
    re.IGNORECASE,
)
_ORDINAL_MARKER = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]+)?(?:\*\*|__)?\d+[.)](?:\*\*|__)?[ \t]+", re.MULTILINE
)
_THOUSANDS_SPACE = re.compile(r"[ \u00a0\u202f]")
#: One number: English thousands (``1,299.99``), space-separated thousands (``120 000``,
#: ``1 299,99``; plain, no-break and narrow no-break spaces; never glued to a word, so
#: ``A15 115 990`` is 15 and 115990, nor to a Latin suffix, so ``7 155H`` is 7 and 155), or
#: plain digits with an optional decimal part.
_NUMBER = re.compile(
    r"(?:(?<![\d.,])\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|(?<![\w.,])\d{1,3}(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?(?![A-Za-z])"
    r"|(?<![\d.,])\d+(?:[.,]\d+)?)"
    r"(?!\d)"
)
#: Hyphen-minus and the Unicode minus sign.
_MINUS_SIGNS = "-−"


@dataclass(frozen=True)
class GradeCheck:
    """The outcome of one ``expected`` key."""

    key: str
    status: CheckStatus
    message: str = ""

    def to_json_dict(self) -> dict[str, str]:
        return {"key": self.key, "status": self.status, "message": self.message}


@dataclass(frozen=True)
class Grade:
    """All checks of one answer."""

    checks: tuple[GradeCheck, ...] = ()

    @property
    def passed(self) -> bool:
        """No check failed (``ungraded`` keys do not fail a grade)."""

        return all(check.status != "failed" for check in self.checks)

    @property
    def status(self) -> CheckStatus:
        if not self.passed:
            return "failed"
        if all(check.status == "ungraded" for check in self.checks):
            return "ungraded"  # no checks at all, or none with a grader
        return "passed"

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": [check.to_json_dict() for check in self.checks],
        }


def grade_answer(answer: str, expected: Mapping[str, Any]) -> Grade:
    """Grade ``answer`` against every key of ``expected``, in key order."""

    checks = []
    for key, value in expected.items():
        grader = _GRADERS.get(key)
        if grader is None:
            checks.append(GradeCheck(key, "ungraded", "no deterministic grader for this key"))
            continue
        try:
            problem = grader(answer, value)
        except (TypeError, ValueError, re.error) as exc:
            problem = f"invalid expectation {value!r}: {exc}"
        checks.append(GradeCheck(key, "failed", problem) if problem else GradeCheck(key, "passed"))
    return Grade(tuple(checks))


def count_items(answer: str) -> int:
    """The number of items in ``answer`` (see the module docstring)."""

    lines = _prose_lines(answer)
    run, numbered_indent = _numbered_run(lines)
    if run >= 2:
        listed = run
    else:
        # A lone numbered line ("1. Opened the site.") does not hide a bullet list, but
        # bullets indented under it are its details.
        bullets = _bullet_sizes(lines, max_indent=numbered_indent if run else None)
        listed = max([run, *bullets])
    return max([listed, *_table_sizes(lines)])


def answer_numbers(answer: str) -> list[float]:
    """Every number in ``answer``, thousands separators removed, with its sign."""

    text = _ORDINAL_MARKER.sub("", _URL.sub(" ", answer))
    numbers = []
    for match in _NUMBER.finditer(text):
        digits = _THOUSANDS_SPACE.sub("", match.group())
        if "," in digits and "." in digits:
            digits = digits.replace(",", "")  # 1,299.99
        elif re.fullmatch(r"\d{1,3}(?:,\d{3})+", digits):
            digits = digits.replace(",", "")  # 1,299
        else:
            digits = digits.replace(",", ".")  # 1299,99
        value = float(digits)
        start = match.start()
        # A minus right before the number, not after a word or digit (a range like 10-20).
        if start and text[start - 1] in _MINUS_SIGNS and (start < 2 or not text[start - 2].isalnum()):
            value = -value
        numbers.append(value)
    return numbers


def _prose_lines(answer: str) -> list[str]:
    """The answer's lines; fenced code blocks and horizontal rules become blank lines."""

    lines: list[str] = []
    in_code = False
    for line in answer.splitlines():
        if _FENCE.match(line):
            in_code = not in_code
            lines.append("")
            continue
        lines.append("" if in_code or _RULE.match(line) else line)
    return lines


def _numbered_run(lines: list[str]) -> tuple[int, int]:
    """The longest run 1, 2, 3, … of numbered items at the outermost numbered indent, and
    that indent."""

    matches = [match for line in lines if (match := _NUMBERED.match(line))]
    if not matches:
        return 0, 0
    top = min(_indent(match) for match in matches)
    longest = run = 0
    for match in matches:
        if _indent(match) != top:
            continue
        number = int(match["number"])
        run = run + 1 if number == run + 1 else (1 if number == 1 else 0)
        longest = max(longest, run)
    return longest, top


def _bullet_sizes(lines: list[str], *, max_indent: int | None = None) -> list[int]:
    """Top-level item counts of each bullet list: blank and deeper-indented lines continue a
    list, any other line ends it. Bullets deeper than ``max_indent`` are ignored."""

    indents = [
        _indent(match)
        for line in lines
        if (match := _BULLET.match(line))
        and (max_indent is None or _indent(match) <= max_indent)
    ]
    if not indents:
        return []
    top = min(indents)
    sizes, size = [], 0
    for line in lines:
        match = _BULLET.match(line)
        if match and _indent(match) == top:
            size += 1
        elif not line.strip() or len(line.expandtabs()) - len(line.expandtabs().lstrip()) > top:
            continue
        elif size:
            sizes.append(size)
            size = 0
    return [*sizes, size] if size else sizes


def _table_sizes(lines: list[str]) -> list[int]:
    """Body rows of each table: the rows after its separator in its shape (cell count, outer
    pipes), or every row of a table with outer pipes and no separator."""

    sizes = []
    block: list[str] = []
    for line in [*lines, ""]:
        if line.strip() and "|" in line:
            block.append(line)
            continue
        if block:
            separators = [i for i, row in enumerate(block) if _TABLE_SEPARATOR.match(row)]
            if separators:
                separator = block[separators[0]]
                cells, piped = _cells(separator), separator.strip().startswith("|")
                body = 0
                for row in block[separators[0] + 1 :]:
                    if _cells(row) != cells or (piped and not row.strip().startswith("|")):
                        continue  # a wrapped cell, or a line after the table holding a "|"
                    body += 1
                sizes.append(body)
            elif all(_TABLE_ROW.match(row) for row in block):
                sizes.append(len(block))
            block = []
    return sizes


def _cells(row: str) -> int:
    return len(_CELL_PIPE.split(row.strip().strip("|")))


def _indent(match: re.Match[str]) -> int:
    return len(match["indent"].expandtabs())


def _strings(value: Any) -> list[str]:
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        raise TypeError("expected a string or a list of strings")
    if not items or not all(items):
        raise ValueError("an empty expectation checks nothing")
    return list(items)


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError("expected a non-negative integer")
    return value


def _bound(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("expected a number")
    return float(value)


def _contains(answer: str, value: Any) -> str:
    missing = [text for text in _strings(value) if text.casefold() not in answer.casefold()]
    return f"missing {missing!r}" if missing else ""


def _not_contains(answer: str, value: Any) -> str:
    present = [text for text in _strings(value) if text.casefold() in answer.casefold()]
    return f"contains {present!r}" if present else ""


def _regex(answer: str, value: Any) -> str:
    unmatched = [pattern for pattern in _strings(value) if not re.search(pattern, answer)]
    return f"no match for {unmatched!r}" if unmatched else ""


def _items(answer: str, value: Any) -> str:
    expected, actual = _count(value), count_items(answer)
    return "" if actual == expected else f"{actual} items, expected {expected}"


def _min_items(answer: str, value: Any) -> str:
    expected, actual = _count(value), count_items(answer)
    return "" if actual >= expected else f"{actual} items, expected at least {expected}"


def _max_items(answer: str, value: Any) -> str:
    expected, actual = _count(value), count_items(answer)
    return "" if actual <= expected else f"{actual} items, expected at most {expected}"


def _numbers_at_most(answer: str, value: Any) -> str:
    bound, numbers = _bound(value), answer_numbers(answer)
    if not numbers:
        return "the answer has no numbers"
    over = [number for number in numbers if number > bound]
    return f"numbers above {value}: {over!r}" if over else ""


def _numbers_at_least(answer: str, value: Any) -> str:
    bound, numbers = _bound(value), answer_numbers(answer)
    if not numbers:
        return "the answer has no numbers"
    under = [number for number in numbers if number < bound]
    return f"numbers below {value}: {under!r}" if under else ""


_GRADERS: dict[str, Callable[[str, Any], str]] = {
    "contains": _contains,
    "not_contains": _not_contains,
    "regex": _regex,
    "items": _items,
    "min_items": _min_items,
    "max_items": _max_items,
    "numbers_at_most": _numbers_at_most,
    "numbers_at_least": _numbers_at_least,
}


__all__ = [
    "CheckStatus",
    "Grade",
    "GradeCheck",
    "answer_numbers",
    "count_items",
    "grade_answer",
]
