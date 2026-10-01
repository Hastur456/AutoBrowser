"""Browser side of the PermissionEngine: resources (domain, click target) and builtin rules.

The engine (:mod:`src.harness.permissions`) is server-neutral; everything that knows about
URLs, Playwright MCP arguments and snapshot text lives here. Both resources are best-effort
guardrails, not a security boundary — the real browser boundaries are profile isolation and
Playwright's ``--blocked-origins``.

* ``domain`` — the host a call acts on: ``args.url`` for ``browser_navigate`` (and
  ``browser_tabs`` ``new``), otherwise the ``- Page URL:`` line of the latest tool output, then
  of the current snapshot (Playwright MCP prints it in both). Lowercase, no ``www.``/port,
  IDN as punycode (:func:`src.config.normalize_domain`). ``data:``/``about:`` pages and an
  unparsable URL give no domain, which permission rules treat fail-closed.
* ``target`` — what an element action acts on, for rules such as "a click on «Купить» needs
  approval": the model's ``element`` description plus the snapshot line of the referenced
  element (role and accessible name from the page), joined by ``\\n`` so a rule matches
  either source.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from src.browser.names import TABS_TOOL
from src.config import PermissionRule, normalize_domain
from src.contracts import PermissionCheck

NAVIGATE_TOOL = "browser_navigate"

_PAGE_URL = re.compile(r"^\s*-\s*Page URL:\s*(\S+)", re.MULTILINE)
_SCHEMELESS = re.compile(r"^[\w.-]+\.[a-z]{2,}([:/?#]|$)", re.IGNORECASE)
_ATTRIBUTE = re.compile(r"\s*\[[^\]]*\]")

#: Browser rules no configuration can remove (``extra_builtin`` of the session engine).
BROWSER_BUILTIN_RULES: tuple[PermissionRule, ...] = (
    PermissionRule(
        id="browser-evaluate",
        decision="ask",
        always_ask=True,
        tool=r"browser_evaluate|browser_run_code(_unsafe)?",
        reason="Running JavaScript in the page needs approval ({tool}).",
    ),
    PermissionRule(
        id="browser-file-upload",
        decision="ask",
        tool=r"browser_file_upload|browser_drop",
        reason="Handing local files to the page needs approval ({tool}).",
    ),
)


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


def snapshot_element(snapshot: Any, ref: Any) -> str:
    """Role and accessible name of ``ref`` in the snapshot: ``button "Купить"``; else ``""``."""

    ref = str(ref or "").strip()
    if not ref:
        return ""
    marker = f"[ref={ref}]"
    for line in str(snapshot or "").splitlines():
        if marker in line:
            text = _ATTRIBUTE.sub("", line).strip().removeprefix("-").strip()
            return text.removesuffix(":").strip()
    return ""


class BrowserResourceResolver:
    """:class:`~src.contracts.PermissionResourceResolver` for Playwright-MCP-style tools."""

    def resources(self, check: PermissionCheck, state: Mapping[str, Any]) -> Mapping[str, str]:
        resources: dict[str, str] = {}
        domain = self._domain(check, state)
        if domain:
            resources["domain"] = domain
        target = self._target(check, state)
        if target:
            resources["target"] = target
        return resources

    @staticmethod
    def _domain(check: PermissionCheck, state: Mapping[str, Any]) -> str:
        if check.tool in {NAVIGATE_TOOL, TABS_TOOL} and check.args.get("url"):
            # The destination, even when it has no host: never fall back to the current page.
            return url_domain(check.args["url"])
        result = state.get("tool_result") or {}
        for text in (
            result.get("content") if isinstance(result, Mapping) else "",
            state.get("snapshot"),
        ):
            url = page_url(text)
            if url:
                return url_domain(url)
        return ""

    @staticmethod
    def _target(check: PermissionCheck, state: Mapping[str, Any]) -> str:
        snapshot = state.get("snapshot")
        parts = [str(check.args.get("element", "") or "").strip()]
        parts.append(snapshot_element(snapshot, check.args.get("target") or check.args.get("ref")))
        for field in check.args.get("fields") or []:
            if isinstance(field, Mapping):
                parts.append(str(field.get("name", "") or "").strip())
                parts.append(snapshot_element(snapshot, field.get("target") or field.get("ref")))
        return "\n".join(part for part in parts if part)


__all__ = [
    "BROWSER_BUILTIN_RULES",
    "BrowserResourceResolver",
    "page_url",
    "snapshot_element",
    "url_domain",
]
