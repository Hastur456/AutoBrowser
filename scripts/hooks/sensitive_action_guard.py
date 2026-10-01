"""pre_tool_use command hook: stop actions on controls that look irreversible.

Reads the human-readable ``element`` argument of ref-based Playwright tools
(``browser_click``, ``browser_type``, ``browser_select_option``) and matches it against
keywords such as "Оплатить", "Оформить заказ", "Удалить", "Place order" -- whole words,
case- and whitespace-insensitive.

On a match:

* ``--decision ask`` (default) prints ``{"decision": "ask"}``: the call becomes
  ``needs_human``. The CLI wires no human callback, so unless a ``permission_request``
  hook approves the tool, the task ends ``blocked``.
* ``--decision deny`` exits 2 with the reason on stderr: only this call is blocked and the
  model may choose another action.

Options: ``--decision ask|deny``, ``--keyword TEXT`` (repeatable; replaces the defaults).

    - id: sensitive-actions
      event: pre_tool_use
      type: command
      command: python scripts/hooks/sensitive_action_guard.py
      match: {tool: browser_click|browser_type|browser_select_option}
"""

from __future__ import annotations

import argparse
import json
import re
import sys

DEFAULT_KEYWORDS = (
    "оплатить",
    "оплата",
    "оформить заказ",
    "подтвердить заказ",
    "подтвердить оплату",
    "купить сейчас",
    "купить в 1 клик",
    "удалить",
    "pay",
    "pay now",
    "place order",
    "buy now",
    "complete purchase",
    "delete",
)

_SPACES = re.compile(r"[\s\u00a0\u202f\u2009]+")


def _normalize(text: str) -> str:
    return _SPACES.sub(" ", text).strip().lower()


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    words = map(re.escape, _normalize(keyword).split())
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--decision", choices=("ask", "deny"), default="ask")
    parser.add_argument("--keyword", action="append", dest="keywords", default=None)
    try:
        options = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits 2, which would read as "block"
        return 0 if exc.code == 0 else 1
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    event = json.load(sys.stdin)
    element = (event.get("args") or {}).get("element")
    if not isinstance(element, str) or not element.strip():
        return 0
    text = _normalize(element)
    for keyword in options.keywords or DEFAULT_KEYWORDS:
        if _normalize(keyword) and _keyword_pattern(keyword).search(text):
            reason = (
                f"Sensitive action guard: the target control looks like an irreversible "
                f"action ('{_normalize(keyword)}') and needs explicit approval."
            )
            if options.decision == "deny":
                print(reason, file=sys.stderr)
                return 2
            print(json.dumps({"decision": "ask", "reason": reason}))
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
