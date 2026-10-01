"""post_tool_use / post_tool_use_failure command hook: mask personal data in tool output.

Replaces e-mails, Russian-style phone numbers (``+7``/``8``, then 3-3-2-2 digits) and
Luhn-valid card numbers in the tool output (``content``, or ``error`` on failure) with
``[redacted <kind>]`` and prints ``{"updated_output": ...}``. Values that occur verbatim in
the task are kept, so the agent can still check what it was asked to type. Masking is
deterministic, so snapshot fingerprints stay stable; element refs (``e123``) never match.

Options: ``--kinds email,phone,card`` (default: all three).

    - id: pii
      event: post_tool_use
      type: command
      command: python scripts/hooks/pii_redaction.py
      match: {tool: browser_.*}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable

KINDS = ("email", "phone", "card")

_SEP = r"[ \u00a0\u202f\u2009-]"
_CARD = re.compile(
    r"(?<![\w-])(?:\d{13,19}"
    rf"|\d{{4}}({_SEP})\d{{4}}\1\d{{4}}\1\d{{1,7}}"
    rf"|\d{{4}}({_SEP})\d{{6}}\2\d{{5}})(?![\w-])"
)
_EMAIL = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])"
)
_PHONE = re.compile(
    rf"(?<![\w+])(?:\+\d{{1,3}}|8){_SEP}?\(?\d{{3}}\)?{_SEP}?\d{{3}}"
    rf"{_SEP}?\d{{2}}{_SEP}?\d{{2}}(?!\w)"
)


def _is_card(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char)
        if index % 2:
            number = number * 2 - 9 if number > 4 else number * 2
        total += number
    return total % 10 == 0


RULES: dict[str, tuple[re.Pattern[str], Callable[[str], bool] | None]] = {
    "email": (_EMAIL, None),
    "phone": (_PHONE, None),
    "card": (_CARD, _is_card),
}


def _kinds(value: str) -> list[str]:
    kinds = [kind.strip() for kind in value.split(",") if kind.strip()]
    unknown = sorted(set(kinds) - set(KINDS))
    if unknown or not kinds:
        raise argparse.ArgumentTypeError(f"use a comma-separated subset of {','.join(KINDS)}")
    return kinds


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kinds", type=_kinds, default=list(KINDS))
    try:
        options = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits 2, which would read as "block"
        return 0 if exc.code == 0 else 1
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    event = json.load(sys.stdin)
    field = "content" if event.get("name") == "post_tool_use" else "error"
    text = str((event.get("result") or {}).get(field) or "")
    task = str(event.get("task") or "")
    masked = 0

    def mask(kind: str, check: Callable[[str], bool] | None) -> Callable[[re.Match[str]], str]:
        def replace(match: re.Match[str]) -> str:
            nonlocal masked
            value = match.group()
            if value in task or (check is not None and not check(value)):
                return value
            masked += 1
            return f"[redacted {kind}]"

        return replace

    redacted = text
    for kind in options.kinds:
        pattern, check = RULES[kind]
        redacted = pattern.sub(mask(kind, check), redacted)
    if masked:
        tool = event.get("tool") or "tool"
        print(
            json.dumps(
                {
                    "updated_output": redacted,
                    "user_message": f"pii_redaction masked {masked} value(s) in {tool} output.",
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
