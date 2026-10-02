"""Browser side of persistent memory: the scope (current host) and the write policy.

The harness (:mod:`src.harness.memory_store`) is server-neutral; it receives these through
the :class:`~src.contracts.MemoryScopeResolver` and :class:`~src.contracts.MemoryContentPolicy`
protocols, the same way the permission engine receives :class:`BrowserResourceResolver`.

* :class:`BrowserMemoryScope` — the normalized host of the page the browser is on (the
  ``- Page URL:`` of the latest tool output, then of the snapshot), so ``sites/ozon.ru.md``
  is shown on ``www.ozon.ru`` and ``seller.ozon.ru``.
* :class:`BrowserMemoryPolicy` — what must never be persisted: element refs (valid for one
  snapshot only), CSS/XPath selectors (the agent is snapshot-driven, not selector-driven),
  prompt-injection phrases (memory is re-read by later sessions) and secret-like
  ``key: value`` pairs. Reasons never quote the refused text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from src.browser.hooks import DEFAULT_INJECTION_PATTERNS
from src.browser.pages import current_page_url, url_domain

_REF = re.compile(r"\bref\s*=?\s*e\d+\b|\[ref=", re.IGNORECASE)
_SELECTORS = (
    re.compile(r"^\s*[#.][\w-]+\s*[>{]", re.MULTILINE),
    re.compile(r"//\w+\["),
    re.compile(r"\bxpath\b", re.IGNORECASE),
    re.compile(r"querySelector", re.IGNORECASE),
)
_SECRET = re.compile(
    r"(?i)\b(password|пароль|token|api[_-]?key|secret)\s*[:=]\s*\S",
)


class BrowserMemoryScope:
    """:class:`~src.contracts.MemoryScopeResolver`: the host of the current page."""

    def scope(self, state: Mapping[str, Any]) -> str:
        return url_domain(current_page_url(state))


class BrowserMemoryPolicy:
    """:class:`~src.contracts.MemoryContentPolicy` for snapshot-driven browser memory."""

    def __init__(self, injection_patterns: Sequence[str] = DEFAULT_INJECTION_PATTERNS) -> None:
        self._injection = [re.compile(pattern, re.IGNORECASE) for pattern in injection_patterns]

    def violation(self, text: str) -> str | None:
        text = str(text or "")
        if _REF.search(text):
            return (
                "Memory must not contain element refs (ref=e…): a ref is valid for one "
                "snapshot only. Describe the control by its role and visible name instead."
            )
        if any(pattern.search(text) for pattern in _SELECTORS):
            return (
                "Memory must not contain CSS/XPath selectors: the agent works from "
                "snapshots. Describe the control by its role and visible name instead."
            )
        if any(pattern.search(text) for pattern in self._injection):
            return "Memory must not contain instructions aimed at the agent or its prompt."
        if _SECRET.search(text):
            return "Memory must not contain passwords, tokens, keys or other secrets."
        return None


__all__ = ["BrowserMemoryPolicy", "BrowserMemoryScope"]
