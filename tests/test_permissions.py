"""PermissionEngine: precedence, modes, grants, fail-closed and rule validation.

Engines are built from explicit ``PermissionsSettings(...)`` / rules, never from
``get_settings()`` (a personal ``config.yaml`` would leak in).
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import ValidationError

from src.config import PermissionRule, PermissionsSettings, normalize_domain
from src.contracts import PermissionCheck
from src.harness.permissions import BUILTIN_RULES, PermissionEngine

MODES = ("default", "read_only", "dont_ask", "bypass")


def rule(id: str, decision: str, **fields: Any) -> PermissionRule:
    return PermissionRule(id=id, decision=decision, **fields)


def engine(*rules: PermissionRule, mode: str = "default", **kwargs: Any) -> PermissionEngine:
    return PermissionEngine.from_settings(
        PermissionsSettings(mode=mode, rules=list(rules)), **kwargs
    )


def check(tool: str = "browser_click", **fields: Any) -> PermissionCheck:
    return PermissionCheck(tool=tool, server=fields.pop("server", "playwright"), **fields)


class Resolver:
    def __init__(self, **resources: str) -> None:
        self._resources = resources

    def resources(self, check: PermissionCheck, state: Mapping[str, Any]) -> Mapping[str, str]:
        return self._resources


class Exploding:
    def resources(self, check: PermissionCheck, state: Mapping[str, Any]) -> Mapping[str, str]:
        raise RuntimeError("secret-token-in-url")


# --------------------------------------------------------------------------- precedence


def test_no_rule_allows_in_default_mode() -> None:
    verdict = engine().evaluate(check())
    assert (verdict.decision, verdict.source, verdict.rule_id) == ("allow", "mode", "")


def test_a_read_only_tool_is_allowed_by_its_annotation() -> None:
    verdict = engine().evaluate(check("browser_snapshot", read_only=True))
    assert (verdict.decision, verdict.source) == ("allow", "annotation")


@pytest.mark.parametrize(
    "order",
    list(itertools.permutations([("a", "allow"), ("k", "ask"), ("d", "deny")])),
)
def test_deny_beats_ask_beats_allow_in_any_order(order: tuple[tuple[str, str], ...]) -> None:
    rules = [rule(id, decision) for id, decision in order]
    verdict = engine(*rules).evaluate(check())
    assert (verdict.decision, verdict.rule_id, verdict.source) == ("deny", "d", "rule")

    without_deny = [item for item in rules if item.decision != "deny"]
    verdict = engine(*without_deny).evaluate(check())
    assert (verdict.decision, verdict.rule_id) == ("ask", "k")


def test_allow_rule_reports_its_id() -> None:
    verdict = engine(rule("ok", "allow", tool="browser_click")).evaluate(check())
    assert (verdict.decision, verdict.source, verdict.rule_id) == ("allow", "rule", "ok")


def test_a_deny_rule_beats_every_mode_grant_and_hook() -> None:
    for mode in MODES:
        perms = engine(rule("no", "deny"), rule("yes", "allow"), mode=mode)
        perms.grant(("playwright", "browser_click", ""))
        verdict = perms.evaluate(check(read_only=True, hook_ask_reason="hook says ask"))
        assert (verdict.decision, verdict.rule_id) == ("deny", "no"), mode


def test_generated_and_custom_reasons() -> None:
    assert engine(rule("r1", "deny")).evaluate(check()).reason == "Denied by rule r1."
    assert engine(rule("r2", "ask")).evaluate(check()).reason == "Approval required by rule r2."
    custom = rule("r3", "deny", reason="No {tool} on this site.")
    assert engine(custom).evaluate(check()).reason == "No browser_click on this site."


def test_reasons_never_quote_the_arguments() -> None:
    secret = "hunter2-secret"
    perms = engine(rule("t", "deny", tool="browser_type", args={"text": "hunter"}))
    verdict = perms.evaluate(check("browser_type", args={"text": secret}))
    assert verdict.decision == "deny" and secret not in verdict.reason


# --------------------------------------------------------------------------- matching


def test_tool_is_a_fullmatch_and_server_is_exact() -> None:
    perms = engine(rule("nav", "deny", tool="browser_nav", server="playwright"))
    assert perms.evaluate(check("browser_navigate")).decision == "allow"
    perms = engine(rule("nav", "deny", tool="browser_navigate|browser_tabs", server="other"))
    assert perms.evaluate(check("browser_navigate")).decision == "allow"
    assert perms.evaluate(check("browser_navigate", server="other")).decision == "deny"


def test_args_are_searched_and_a_missing_argument_does_not_match() -> None:
    perms = engine(rule("cards", "deny", tool="browser_type", args={"text": r"\d{16}"}))
    assert perms.evaluate(check("browser_type", args={"text": "x 4111111111111111"})).decision == "deny"
    assert perms.evaluate(check("browser_type", args={"text": "hello"})).decision == "allow"
    assert perms.evaluate(check("browser_type", args={})).decision == "allow"
    numeric = engine(rule("n", "deny", args={"index": "^3$"}))
    assert numeric.evaluate(check(args={"index": 3})).decision == "deny"


def test_domains_are_suffix_matched() -> None:
    perms = engine(rule("shop", "deny", tool="browser_navigate", not_domains=["ozon.ru"]))
    navigate = check("browser_navigate")
    for domain, decision in [
        ("ozon.ru", "allow"),
        ("www.ozon.ru", "allow"),
        ("seller.ozon.ru", "allow"),
        ("notozon.ru", "deny"),
        ("evil.com", "deny"),
    ]:
        perms._resolver = Resolver(domain=domain)  # noqa: SLF001 - swap the resource only
        assert perms.evaluate(navigate).decision == decision, domain


def test_an_unknown_domain_fails_closed() -> None:
    """Restricting rules apply, permitting rules do not."""

    no_domain = Resolver()
    deny = engine(rule("d", "deny", domains=["evil.com"]), resolver=no_domain)
    assert deny.evaluate(check()).decision == "deny"
    ask = engine(rule("a", "ask", not_domains=["ozon.ru"]), resolver=no_domain)
    assert ask.evaluate(check()).decision == "ask"
    allow = engine(rule("ok", "allow", domains=["ozon.ru"]), mode="read_only", resolver=no_domain)
    assert allow.evaluate(check()).decision == "deny"  # the allow did not match
    # Without any resolver the domain is unknown as well.
    assert engine(rule("d", "deny", domains=["evil.com"])).evaluate(check()).decision == "deny"


def test_target_matches_the_resolved_target_and_fails_closed() -> None:
    buy = rule("buy", "ask", tool="browser_click", target="(?i)купить|buy")
    assert engine(buy, resolver=Resolver(target="button «Купить»")).evaluate(check()).decision == "ask"
    assert engine(buy, resolver=Resolver(target="link Catalog")).evaluate(check()).decision == "allow"
    assert engine(buy, resolver=Resolver()).evaluate(check()).decision == "ask"
    allow = rule("ok", "allow", target="Catalog")
    assert engine(allow, mode="read_only", resolver=Resolver()).evaluate(check()).decision == "deny"


# --------------------------------------------------------------------------- modes


def test_read_only_mode() -> None:
    perms = engine(mode="read_only")
    assert perms.evaluate(check("browser_snapshot", read_only=True)).decision == "allow"
    denied = perms.evaluate(check("browser_click"))
    assert (denied.decision, denied.source) == ("deny", "mode")
    assert "read_only" in denied.reason
    allowed = engine(rule("clicks", "allow", tool="browser_click"), mode="read_only")
    assert allowed.evaluate(check("browser_click")).decision == "allow"


def test_ask_per_mode() -> None:
    ask = rule("k", "ask", reason="Needs a human.")
    expected = {
        "default": ("ask", "rule"),
        "read_only": ("ask", "rule"),
        "dont_ask": ("deny", "mode"),
        "bypass": ("allow", "mode"),
    }
    for mode, (decision, source) in expected.items():
        verdict = engine(ask, mode=mode).evaluate(check(read_only=True))
        assert (verdict.decision, verdict.source, verdict.rule_id) == (decision, source, "k"), mode
    denied = engine(ask, mode="dont_ask").evaluate(check())
    assert denied.reason.startswith("Not executed: Needs a human. ")
    assert "dont_ask" in denied.reason


def test_always_ask_and_hook_ask_are_not_bypassed() -> None:
    always = rule("js", "ask", tool="browser_evaluate", always_ask=True)
    verdict = engine(always, mode="bypass").evaluate(check("browser_evaluate"))
    assert (verdict.decision, verdict.always_ask, verdict.grant_key) == ("ask", True, None)
    hooked = engine(mode="bypass").evaluate(check(hook_ask_reason="hook wants a human"))
    assert (hooked.decision, hooked.source, hooked.reason) == ("ask", "hook", "hook wants a human")
    assert hooked.always_ask
    assert engine(always, mode="dont_ask").evaluate(check("browser_evaluate")).decision == "deny"
    assert engine(mode="dont_ask").evaluate(check(hook_ask_reason="x")).decision == "deny"


def test_the_mode_can_be_overridden() -> None:
    perms = PermissionEngine.from_settings(PermissionsSettings(mode="bypass"), mode="dont_ask")
    assert perms.mode == "dont_ask"


# --------------------------------------------------------------------------- grants


def test_a_session_grant_covers_exactly_server_tool_and_domain() -> None:
    perms = engine(rule("k", "ask"), resolver=Resolver(domain="ozon.ru"))
    verdict = perms.evaluate(check())
    assert verdict.decision == "ask"
    assert verdict.grant_key == ("playwright", "browser_click", "ozon.ru")

    perms.grant(verdict.grant_key)
    granted = perms.evaluate(check())
    assert (granted.decision, granted.source, granted.rule_id) == ("allow", "grant", "k")

    assert perms.evaluate(check("browser_type")).decision == "ask"
    assert perms.evaluate(check(server="other")).decision == "ask"
    perms._resolver = Resolver(domain="wildberries.ru")  # noqa: SLF001
    assert perms.evaluate(check()).decision == "ask"


def test_a_grant_never_covers_always_ask_or_a_hook_ask() -> None:
    perms = engine(rule("js", "ask", always_ask=True))
    perms.grant(("playwright", "browser_click", ""))
    assert perms.evaluate(check()).decision == "ask"
    assert engine().evaluate(check(hook_ask_reason="h")).decision == "ask"
    hooked = engine()
    hooked.grant(("playwright", "browser_click", ""))
    assert hooked.evaluate(check(hook_ask_reason="h")).decision == "ask"


def test_a_grant_does_not_turn_read_only_mode_off() -> None:
    perms = engine(rule("k", "ask"), mode="read_only")
    perms.grant(("playwright", "browser_click", ""))
    assert perms.evaluate(check()).decision == "deny"


# --------------------------------------------------------------------------- fail closed


def test_a_resolver_failure_denies_without_leaking_the_error() -> None:
    verdict = engine(rule("ok", "allow"), resolver=Exploding()).evaluate(check())
    assert (verdict.decision, verdict.source) == ("deny", "error")
    assert "secret-token-in-url" not in verdict.reason


def test_a_matcher_failure_denies() -> None:
    class Weird:
        def __str__(self) -> str:
            raise ValueError("boom")

    perms = engine(rule("t", "allow", args={"text": "x"}))
    verdict = perms.evaluate(check(args={"text": Weird()}))
    assert (verdict.decision, verdict.source) == ("deny", "error")


# --------------------------------------------------------------------------- builtin rules


def test_the_builtin_marker_rule_asks_for_sensitive_tool_names() -> None:
    perms = engine()
    for name in ("purchase_item", "make_PAYMENT", "delete_account", "get_credentials"):
        verdict = perms.evaluate(check(name, server="shop"))
        assert (verdict.decision, verdict.source) == ("ask", "builtin"), name
        assert verdict.reason == f"Tool requires human approval before use: {name}"
        assert verdict.rule_id == "sensitive-tool-name"
    assert perms.evaluate(check("browser_click")).decision == "allow"


def test_config_rules_cannot_remove_builtin_rules() -> None:
    perms = engine(rule("buy-ok", "allow", tool="purchase_item"))
    assert perms.evaluate(check("purchase_item")).decision == "ask"


def test_extra_builtin_rules_and_duplicate_ids() -> None:
    extra = rule("js", "ask", tool="browser_evaluate", always_ask=True)
    perms = engine(extra_builtin=[extra])
    assert perms.evaluate(check("browser_evaluate")).source == "builtin"
    with pytest.raises(ValueError, match="duplicate permission rule id"):
        engine(rule("sensitive-tool-name", "deny"))
    assert [item.id for item in BUILTIN_RULES] == ["sensitive-tool-name"]


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "fields",
    [
        {"id": "x", "decision": "deny", "tool": "("},
        {"id": "x", "decision": "deny", "target": "["},
        {"id": "x", "decision": "deny", "args": {"url": "("}},
        {"id": "x", "decision": "deny", "always_ask": True},
        {"id": "x", "decision": "allow", "always_ask": True},
        {"id": "x", "decision": "ask", "domains": ["a.com"], "not_domains": ["b.com"]},
        {"id": "x", "decision": "deny", "domains": ["https://evil.com/path"]},
        {"id": "x", "decision": "deny", "domains": [""]},
        {"id": "", "decision": "deny"},
        {"id": "x", "decision": "maybe"},
        {"id": "x", "decision": "deny", "unknown": 1},
    ],
)
def test_invalid_rules_are_rejected(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PermissionRule(**fields)


def test_duplicate_rule_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate permission rule id"):
        PermissionsSettings(rules=[{"id": "a", "decision": "deny"}, {"id": "a", "decision": "ask"}])


def test_rule_domains_are_normalized() -> None:
    parsed = rule("d", "deny", domains=["WWW.Ozon.ru", "*.example.com", "пример.рф"])
    assert parsed.domains == ["ozon.ru", "example.com", "xn--e1afmkfd.xn--p1ai"]
    assert normalize_domain("") == ""
