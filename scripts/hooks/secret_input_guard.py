"""pre_tool_use command hook: never type a payment card number or another secret.

Scans every string argument except ``element`` and ``ref`` -- ``text`` of
``browser_type``, the field values of ``browser_fill_form`` -- for card numbers (13-19
digits, contiguous or in 4-digit groups, passing the Luhn check) and for the extra
``--pattern`` regular expressions (case-insensitive).

On a match it exits 2 with the reason on stderr (``--decision deny``, default: only this
call is blocked) or prints ``{"decision": "ask"}`` (``--decision ask``: routed to approval,
which ends the task ``blocked`` when nothing approves it). The reason never quotes the
typed text: ``hook.decided`` events are persisted.

Options: ``--decision deny|ask``, ``--pattern REGEX`` (repeatable).

    - id: secret-input
      event: pre_tool_use
      type: command
      command: python scripts/hooks/secret_input_guard.py
      match: {tool: browser_type|browser_fill_form}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Iterator
from typing import Any

_SEP = r"[ \u00a0\u202f\u2009-]"
_CARD = re.compile(
    r"(?<![\w-])(?:\d{13,19}"
    rf"|\d{{4}}({_SEP})\d{{4}}\1\d{{4}}\1\d{{1,7}}"
    rf"|\d{{4}}({_SEP})\d{{6}}\2\d{{5}})(?![\w-])"
)
_SKIP_KEYS = frozenset({"element", "ref"})


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _has_card(text: str) -> bool:
    for match in _CARD.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return True
    return False


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in _SKIP_KEYS:
                yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--decision", choices=("deny", "ask"), default="deny")
    parser.add_argument("--pattern", action="append", dest="patterns", default=[])
    try:
        options = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits 2, which would read as "block"
        return 0 if exc.code == 0 else 1
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    extra = [re.compile(pattern, re.IGNORECASE) for pattern in options.patterns]

    event = json.load(sys.stdin)
    for value in _strings(event.get("args") or {}):
        if _has_card(value) or any(pattern.search(value) for pattern in extra):
            reason = (
                "Secret input guard: the text to enter looks like a payment card number or "
                "another secret. It is not typed without approval."
            )
            if options.decision == "deny":
                print(reason, file=sys.stderr)
                return 2
            print(json.dumps({"decision": "ask", "reason": reason}))
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
