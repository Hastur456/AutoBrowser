"""post_tool_use command hook: tell the model a captcha or an access block is in the way.

Searches the tool output for bot checks and access blocks (captcha, "не робот",
"Access denied", "403 Forbidden", Cloudflare "Just a moment...", "Доступ ограничен").
On a match it exits 2: on ``post_tool_use`` the stderr text reaches the model as a separate
``[harness]`` note, while the snapshot, its refs and the progress fingerprints stay
untouched.

Options: ``--pattern REGEX`` (repeatable, case-insensitive; extends the defaults).

    - id: obstacles
      event: post_tool_use
      type: command
      command: python scripts/hooks/page_obstacle_detector.py
      match: {tool: browser_snapshot|browser_navigate}
"""

from __future__ import annotations

import argparse
import json
import re
import sys

DEFAULT_PATTERNS = (
    r"\b(?:re|h)?captcha\b",
    r"\bкапч\w*",
    r"\bне\s+робот\w*",
    r"\bare\s+you\s+a\s+robot\b",
    r"\bverify\s+(?:that\s+)?you\s+are\s+(?:a\s+)?human\b",
    r"\bjust\s+a\s+moment\.\.\.",
    r"\baccess\s+denied\b",
    r"\b403\s+forbidden\b",
    r"\bдоступ\s+(?:запрещ|ограничен)\w*",
    r"\bподозрительн\w+\s+активност\w*",
)

MESSAGE = (
    "The page looks like a bot check or an access block (captcha, access denied). "
    "Repeating the same action will not get past it. Do not keep clicking through it: try "
    "another route to the information, or report that the site blocks access."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pattern", action="append", dest="patterns", default=[])
    try:
        options = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits 2, which would read as "block"
        return 0 if exc.code == 0 else 1
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    patterns = [
        re.compile(pattern, re.IGNORECASE) for pattern in (*DEFAULT_PATTERNS, *options.patterns)
    ]

    event = json.load(sys.stdin)
    content = str((event.get("result") or {}).get("content") or "")
    if content and any(pattern.search(content) for pattern in patterns):
        print(MESSAGE, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
