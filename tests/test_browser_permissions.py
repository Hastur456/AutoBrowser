"""Browser permission resources (domain, target) and browser rules from configuration.

The snapshot and response samples are verbatim Playwright MCP output (1.64).
"""

from __future__ import annotations

from typing import Any

import pytest

from src.browser.permissions import (
    BrowserResourceResolver,
    page_url,
    snapshot_element,
    url_domain,
)
from src.config import PermissionRule, PermissionsSettings
from src.contracts import PermissionCheck
from src.harness.permissions import PermissionEngine

SNAPSHOT = """### Page
- Page URL: https://www.ozon.ru/product/123/?utm=1
- Page Title: Shop
### Snapshot
```yaml
- generic [active] [ref=e1]:
  - button "Купить" [ref=e2]
  - link "Catalog" [ref=e3] [cursor=pointer]:
    - /url: /x
  - textbox "Search" [ref=e4]
```"""

NAVIGATE_RESPONSE = """### Ran Playwright code
```js
await page.goto('https://evil.example.com/login');
```
### Page
- Page URL: https://evil.example.com/login
- Page Title: Login
### Snapshot
- [Snapshot](.playwright-mcp\\page-2026-10-01T17-37-30-455Z.yml)"""


def resolve(tool: str, args: dict[str, Any], **state: Any) -> dict[str, str]:
    check = PermissionCheck(tool=tool, server="playwright", args=args)
    return dict(BrowserResourceResolver().resources(check, state))


def engine(*rules: dict[str, Any], mode: str = "default") -> PermissionEngine:
    return PermissionEngine.from_settings(
        PermissionsSettings(mode=mode, rules=[PermissionRule(**rule) for rule in rules]),
        resolver=BrowserResourceResolver(),
    )


def decide(perms: PermissionEngine, tool: str, args: dict[str, Any], **state: Any) -> str:
    check = PermissionCheck(tool=tool, server="playwright", args=args)
    return perms.evaluate(check, state).decision


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("url", "domain"),
    [
        ("https://www.ozon.ru/search/?text=x", "ozon.ru"),
        ("HTTPS://Seller.Ozon.RU:8443/a", "seller.ozon.ru"),
        ("ozon.ru/search?text=x", "ozon.ru"),
        ("https://пример.рф/", "xn--e1afmkfd.xn--p1ai"),
        ("http://127.0.0.1:8000/", "127.0.0.1"),
        ("data:text/html,<b>x</b>", ""),
        ("about:blank", ""),
        ("", ""),
        ("http://[::1]:80/", ""),
    ],
)
def test_url_domain(url: str, domain: str) -> None:
    assert url_domain(url) == domain


def test_page_url_and_snapshot_element_read_real_playwright_output() -> None:
    assert page_url(SNAPSHOT) == "https://www.ozon.ru/product/123/?utm=1"
    assert page_url(NAVIGATE_RESPONSE) == "https://evil.example.com/login"
    assert page_url("- Page Title: none") == ""
    assert snapshot_element(SNAPSHOT, "e2") == 'button "Купить"'
    assert snapshot_element(SNAPSHOT, "e3") == 'link "Catalog"'
    assert snapshot_element(SNAPSHOT, "e1") == "generic"
    assert snapshot_element(SNAPSHOT, "e99") == ""
    assert snapshot_element(SNAPSHOT, "e") == ""
    assert snapshot_element(SNAPSHOT, "") == ""


# --------------------------------------------------------------------------- resolver


def test_navigate_uses_its_destination_not_the_current_page() -> None:
    assert resolve("browser_navigate", {"url": "https://evil.com/x"}, snapshot=SNAPSHOT) == {
        "domain": "evil.com"
    }
    # A destination without a host never falls back to the current page.
    assert resolve("browser_navigate", {"url": "data:text/html,x"}, snapshot=SNAPSHOT) == {}
    assert resolve("browser_tabs", {"action": "new", "url": "https://a.com"}) == {"domain": "a.com"}


def test_any_tool_with_a_url_argument_acts_on_that_url() -> None:
    # No tool names in the resolver: the url argument is the destination, whoever takes it.
    assert resolve("other__open", {"url": "https://a.com/x"}, snapshot=SNAPSHOT) == {
        "domain": "a.com"
    }


def test_element_actions_use_the_page_url() -> None:
    resources = resolve("browser_click", {"element": "Buy button", "target": "e2"}, snapshot=SNAPSHOT)
    assert resources == {"domain": "ozon.ru", "target": 'Buy button\nbutton "Купить"'}


def test_the_latest_tool_output_wins_over_an_older_snapshot() -> None:
    result = {"name": "browser_navigate", "status": "success", "content": NAVIGATE_RESPONSE}
    resources = resolve("browser_type", {"text": "x"}, snapshot=SNAPSHOT, tool_result=result)
    assert resources["domain"] == "evil.example.com"
    # An error without a Page URL falls back to the snapshot.
    failed = {"name": "browser_click", "status": "error", "error": "boom", "content": ""}
    assert resolve("browser_type", {}, snapshot=SNAPSHOT, tool_result=failed)["domain"] == "ozon.ru"


def test_without_any_page_url_there_is_no_domain() -> None:
    assert resolve("browser_click", {"target": "e2"}, snapshot="- button [ref=e2]") == {
        "target": "button"
    }
    assert resolve("browser_click", {}) == {}


def test_fill_form_targets_every_field() -> None:
    args = {"fields": [{"name": "Card number", "target": "e4", "value": "4111"}, "junk"]}
    assert resolve("browser_fill_form", args, snapshot=SNAPSHOT)["target"] == (
        'Card number\ntextbox "Search"'
    )


def test_the_legacy_ref_argument_is_understood() -> None:
    assert resolve("browser_click", {"ref": "e2"}, snapshot=SNAPSHOT)["target"] == 'button "Купить"'


# --------------------------------------------------------------------------- with the engine


def test_navigation_outside_the_allowed_shops_is_denied() -> None:
    perms = engine(
        {"id": "shop-only", "decision": "deny", "tool": "browser_navigate", "not_domains": ["ozon.ru"]}
    )
    assert decide(perms, "browser_navigate", {"url": "https://www.ozon.ru/"}) == "allow"
    assert decide(perms, "browser_navigate", {"url": "https://evil.com/"}) == "deny"
    assert decide(perms, "browser_navigate", {"url": "data:text/html,x"}) == "deny"


def test_a_click_on_a_foreign_domain_is_denied() -> None:
    perms = engine({"id": "stay", "decision": "deny", "tool": "browser_click", "not_domains": ["ozon.ru"]})
    foreign = SNAPSHOT.replace("www.ozon.ru", "evil.com")
    assert decide(perms, "browser_click", {"target": "e2"}, snapshot=SNAPSHOT) == "allow"
    assert decide(perms, "browser_click", {"target": "e2"}, snapshot=foreign) == "deny"


def test_a_snapshot_without_url_never_satisfies_a_domain_allow() -> None:
    perms = engine(
        {"id": "ozon-clicks", "decision": "allow", "tool": "browser_click", "domains": ["ozon.ru"]},
        mode="read_only",
    )
    assert decide(perms, "browser_click", {"target": "e2"}, snapshot=SNAPSHOT) == "allow"
    assert decide(perms, "browser_click", {"target": "e2"}, snapshot="- button [ref=e2]") == "deny"


def test_a_buy_click_asks_through_its_target() -> None:
    perms = engine(
        {
            "id": "purchases",
            "decision": "ask",
            "tool": "browser_click",
            "target": "(?i)купить|оформить|buy",
        }
    )
    assert decide(perms, "browser_click", {"target": "e2"}, snapshot=SNAPSHOT) == "ask"
    assert decide(perms, "browser_click", {"target": "e3"}, snapshot=SNAPSHOT) == "allow"
    # The model's own description counts too.
    assert decide(perms, "browser_click", {"element": "Buy now", "target": "e3"}, snapshot=SNAPSHOT) == "ask"


def test_a_session_grant_is_per_domain() -> None:
    perms = engine({"id": "clicks", "decision": "ask", "tool": "browser_click"})
    check = PermissionCheck(tool="browser_click", server="playwright", args={"target": "e2"})
    verdict = perms.evaluate(check, {"snapshot": SNAPSHOT})
    assert verdict.grant_key == ("playwright", "browser_click", "ozon.ru")
    perms.grant(verdict.grant_key)
    assert perms.evaluate(check, {"snapshot": SNAPSHOT}).decision == "allow"
    foreign = SNAPSHOT.replace("www.ozon.ru", "wildberries.ru")
    assert perms.evaluate(check, {"snapshot": foreign}).decision == "ask"


# --------------------------------------------------------------------------- configured rules

#: The commented examples of ``config.example.yaml``: no rule ships with the code.
PAGE_JS = {
    "id": "page-js",
    "decision": "ask",
    "always_ask": True,
    "tool": "browser_evaluate|browser_run_code(_unsafe)?",
    "reason": "Running JavaScript in the page needs approval ({tool}).",
}
FILE_HANDOFF = {
    "id": "file-handoff",
    "decision": "ask",
    "tool": "browser_file_upload|browser_drop",
    "reason": "Handing local files to the page needs approval ({tool}).",
}


@pytest.mark.parametrize(
    "tool",
    ["browser_navigate", "browser_click", "browser_type", "browser_snapshot", "browser_tabs",
     "browser_evaluate", "browser_run_code_unsafe", "browser_file_upload", "browser_drop"],
)
def test_nothing_asks_without_configured_rules(tool: str) -> None:
    for mode in ("default", "dont_ask", "bypass"):
        assert decide(engine(mode=mode), tool, {"url": "https://ozon.ru"}, snapshot=SNAPSHOT) == "allow"


@pytest.mark.parametrize("tool", ["browser_evaluate", "browser_run_code", "browser_run_code_unsafe"])
def test_a_page_javascript_rule_always_asks(tool: str) -> None:
    for mode, expected in [("default", "ask"), ("bypass", "ask"), ("dont_ask", "deny")]:
        assert decide(engine(PAGE_JS, mode=mode), tool, {"function": "() => 1"}) == expected, mode
    perms = engine(PAGE_JS)
    perms.grant(("playwright", tool, ""))
    verdict = perms.evaluate(PermissionCheck(tool=tool, server="playwright"))
    assert (verdict.decision, verdict.rule_id, verdict.always_ask) == ("ask", "page-js", True)


@pytest.mark.parametrize("tool", ["browser_file_upload", "browser_drop"])
def test_a_file_handoff_rule_asks_but_can_be_granted(tool: str) -> None:
    perms = engine(FILE_HANDOFF)
    check = PermissionCheck(tool=tool, server="playwright", args={"paths": ["C:/secret.txt"]})
    verdict = perms.evaluate(check, {"snapshot": SNAPSHOT})
    assert (verdict.decision, verdict.rule_id) == ("ask", "file-handoff")
    assert "secret" not in verdict.reason
    perms.grant(verdict.grant_key)
    assert perms.evaluate(check, {"snapshot": SNAPSHOT}).decision == "allow"
    assert decide(engine(FILE_HANDOFF, mode="bypass"), tool, {}) == "allow"
