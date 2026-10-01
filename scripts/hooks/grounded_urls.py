"""stop command hook: reject a final answer that cites links nobody observed.

Every ``http(s)://`` link in the final answer must occur in ``evidence`` (the latest
observation and snapshot) or in the task -- as the full URL, as its path (snapshots list
relative ``/url:`` values) or, for a link to a bare site, as its host. Otherwise it exits 2
and the reason goes back to the model, which keeps working. Shares the
``hooks.max_stop_blocks`` budget with the other stop hooks.

No options.

    - id: grounded-urls
      event: stop
      type: command
      command: python scripts/hooks/grounded_urls.py
"""

from __future__ import annotations

import json
import re
import sys
from urllib.parse import urlsplit

_URL = re.compile(r"https?://[^\s<>()\[\]{}\"'«»`]+", re.IGNORECASE)
_TRAILING = ".,;:!?…"


def _seen(url: str, evidence: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    path = parts.path.rstrip("/")
    candidates = [url.rstrip("/")]
    if path:
        candidates.append(f"{path}?{parts.query}" if parts.query else path)
        candidates.append(path)
    elif parts.hostname:
        candidates.append(parts.hostname)
    return any(candidate.lower() in evidence for candidate in candidates if candidate)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

    event = json.load(sys.stdin)
    answer = str(event.get("final_answer") or "")
    urls = dict.fromkeys(match.group().rstrip(_TRAILING) for match in _URL.finditer(answer))
    if not urls:
        return 0
    evidence = "\n".join([*map(str, event.get("evidence") or ()), str(event.get("task") or "")])
    unseen = [url for url in urls if not _seen(url, evidence.lower())]
    if not unseen:
        return 0
    print(
        "The final answer contains links that the latest page observation does not show: "
        f"{', '.join(unseen)}. Take links only from the page (a link's /url in the snapshot) "
        "or leave them out.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
