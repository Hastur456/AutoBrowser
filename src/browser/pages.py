"""Where the browser currently is: page URL and host from Playwright MCP output.

Shared by the permission resolver (:mod:`src.browser.permissions`) and the memory scope
(:mod:`src.browser.memory`). Playwright MCP prints a ``- Page URL:`` line in every tool
response and snapshot; nothing else is parsed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from src.config import normalize_domain

_PAGE_URL = re.compile(r"^\s*-\s*Page URL:\s*(\S+)", re.MULTILINE)
_SCHEMELESS = re.compile(r"^[\w.-]+\.[a-z]{2,}([:/?#]|$)", re.IGNORECASE)


def url_domain(url: Any) -> str:
    """Normalized host of ``url``; ``""`` when it has none (``data:``, ``about:``, garbage)."""

    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text and _SCHEMELESS.match(text):
        text = f"http://{text}"  # "ozon.ru/search" as typed by a model
    try:
        host = urlsplit(text).hostname or ""
        return normalize_domain(host)
    except ValueError:
        return ""


def page_url(text: Any) -> str:
    """The ``- Page URL:`` of a Playwright MCP response or snapshot, else ``""``."""

    match = _PAGE_URL.search(str(text or ""))
    return match.group(1) if match else ""


def current_page_url(state: Mapping[str, Any]) -> str:
    """The page the browser is on: the latest tool output's ``Page URL``, then the snapshot's."""

    result = state.get("tool_result") or {}
    for text in (
        result.get("content") if isinstance(result, Mapping) else "",
        state.get("snapshot"),
    ):
        url = page_url(text)
        if url:
            return url
    return ""


__all__ = ["current_page_url", "page_url", "url_domain"]
