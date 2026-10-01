"""Deterministic tool authorization: ``PermissionCheck -> allow | ask | deny``.

The :class:`PermissionEngine` is session-scoped like the
:class:`~src.harness.hooks.HookEngine`: :class:`~src.harness.session.SessionContext` builds it
from ``settings.permissions`` before any browser/MCP resource starts (a broken rule fails
startup), and the loop reaches it through ``EngineResources.permissions``. It holds the
session's approval grants.

Evaluation (order and specificity of rules do not matter):

1. any matching ``deny`` rule → deny (no mode, grant or hook lifts it);
2. ``read_only`` mode, a tool without ``readOnlyHint`` and no matching ``allow`` → deny;
3. matching ``ask`` rules or a hook ``ask`` → a session grant, ``bypass`` (unless
   ``always_ask``/hook), ``dont_ask`` (→ deny) or ``ask``;
4. any matching ``allow`` rule → allow;
5. otherwise allow (the mode default; ``destructiveHint`` is ignored — Playwright MCP sets it
   on every mutating tool, see ``docs/decisions/2026-10-01-permission-engine.md``).

Any exception fails closed (``deny``, ``source: error``). A rule that filters on a resource
(``domains``, ``not_domains``, ``target``) the resolver could not provide matches for
``deny``/``ask`` and not for ``allow``.

The engine knows no tool names and ships no rules: what is risky is only the configured
``permissions.rules`` (:class:`~src.config.PermissionsSettings`), plus the MCP
``readOnlyHint`` annotation for ``read_only`` mode.

Server-neutral: resources (domain, click target) come from an injected
:class:`~src.contracts.PermissionResourceResolver`; the browser one lives in
:mod:`src.browser.permissions`. Reasons never quote the call arguments.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from src.config import PermissionRule, PermissionsSettings
from src.contracts import (
    PermissionCheck,
    PermissionMode,
    PermissionResourceResolver,
    PermissionSource,
    PermissionVerdict,
)

logger = logging.getLogger(__name__)

GrantKey = tuple[str, str, str]


@dataclass(frozen=True)
class _CompiledRule:
    rule: PermissionRule
    source: PermissionSource
    tool: re.Pattern[str]
    args: tuple[tuple[str, re.Pattern[str]], ...]
    target: re.Pattern[str] | None

    @classmethod
    def build(cls, rule: PermissionRule, source: PermissionSource) -> _CompiledRule:
        return cls(
            rule=rule,
            source=source,
            tool=re.compile(rule.tool),
            args=tuple((key, re.compile(pattern)) for key, pattern in rule.args.items()),
            target=re.compile(rule.target) if rule.target else None,
        )

    def matches(self, check: PermissionCheck, resources: Mapping[str, str]) -> bool:
        rule = self.rule
        if rule.server and rule.server != check.server:
            return False
        if not self.tool.fullmatch(check.tool):
            return False
        for key, pattern in self.args:
            if key not in check.args or not pattern.search(str(check.args[key])):
                return False
        # Fail closed on an unresolved resource: it may be anything, so a restricting
        # rule applies and a permitting one does not.
        unresolved = rule.decision != "allow"
        if rule.domains or rule.not_domains:
            domain = str(resources.get("domain", "") or "")
            if not domain:
                if not unresolved:
                    return False
            elif rule.domains and not _in_domains(domain, rule.domains):
                return False
            elif rule.not_domains and _in_domains(domain, rule.not_domains):
                return False
        if self.target is not None:
            target = str(resources.get("target", "") or "")
            if not target:
                if not unresolved:
                    return False
            elif not self.target.search(target):
                return False
        return True

    def reason(self, check: PermissionCheck) -> str:
        if self.rule.reason:
            return self.rule.reason.replace("{tool}", check.tool)
        if self.rule.decision == "deny":
            return f"Denied by rule {self.rule.id}."
        if self.rule.decision == "ask":
            return f"Approval required by rule {self.rule.id}."
        return f"Allowed by rule {self.rule.id}."


def _in_domains(domain: str, domains: Iterable[str]) -> bool:
    return any(domain == item or domain.endswith(f".{item}") for item in domains)


class PermissionEngine:
    """Session-scoped authorization over rules, a mode and session grants."""

    def __init__(
        self,
        *,
        mode: PermissionMode = "default",
        rules: Iterable[PermissionRule] = (),
        resolver: PermissionResourceResolver | None = None,
    ) -> None:
        compiled = [_CompiledRule.build(rule, "rule") for rule in rules]
        seen: set[str] = set()
        for item in compiled:
            if item.rule.id in seen:
                raise ValueError(f"duplicate permission rule id {item.rule.id!r}")
            seen.add(item.rule.id)
        self._rules = tuple(compiled)
        self._mode: PermissionMode = mode
        self._resolver = resolver
        self._grants: set[GrantKey] = set()

    @classmethod
    def from_settings(
        cls,
        settings: PermissionsSettings | None = None,
        *,
        resolver: PermissionResourceResolver | None = None,
        mode: PermissionMode | None = None,
    ) -> PermissionEngine:
        """Build from ``settings.permissions`` (the code defaults when ``None``, never the
        personal config); ``mode`` overrides the configured one."""

        settings = settings if settings is not None else PermissionsSettings()
        return cls(
            mode=mode or settings.mode,
            rules=settings.rules,
            resolver=resolver,
        )

    @property
    def mode(self) -> PermissionMode:
        return self._mode

    @property
    def grants(self) -> frozenset[GrantKey]:
        return frozenset(self._grants)

    def grant(self, key: GrantKey) -> None:
        """Remember a "for this session" approval of ``(server, tool, domain)``."""

        self._grants.add(key)

    def evaluate(
        self,
        check: PermissionCheck,
        state: Mapping[str, Any] | None = None,
    ) -> PermissionVerdict:
        """Decide ``check``; never raises — a failure is a ``deny``."""

        try:
            return self._evaluate(check, state or {})
        except Exception as exc:  # noqa: BLE001 - authorization must fail closed
            # The exception text may quote arguments; keep it out of the verdict.
            logger.warning("permission check for %s failed: %s", check.tool, type(exc).__name__)
            return PermissionVerdict(
                decision="deny",
                reason="Not executed: the permission check failed, so the call was denied.",
                source="error",
            )

    def _evaluate(self, check: PermissionCheck, state: Mapping[str, Any]) -> PermissionVerdict:
        resources: Mapping[str, str] = (
            dict(self._resolver.resources(check, state)) if self._resolver is not None else {}
        )
        matched = [item for item in self._rules if item.matches(check, resources)]
        by_decision = {
            decision: [item for item in matched if item.rule.decision == decision]
            for decision in ("deny", "ask", "allow")
        }

        if by_decision["deny"]:
            first = by_decision["deny"][0]
            return PermissionVerdict(
                decision="deny",
                reason=first.reason(check),
                source=first.source,
                rule_id=first.rule.id,
            )

        if self._mode == "read_only" and not check.read_only and not by_decision["allow"]:
            return PermissionVerdict(
                decision="deny",
                reason=(
                    f"Not executed: permission mode read_only allows only read-only tools, "
                    f"and {check.tool} can change state."
                ),
                source="mode",
            )

        if by_decision["ask"] or check.hook_ask_reason:
            return self._resolve_ask(check, by_decision["ask"], resources)

        if by_decision["allow"]:
            first = by_decision["allow"][0]
            return PermissionVerdict(
                decision="allow",
                reason=first.reason(check),
                source=first.source,
                rule_id=first.rule.id,
            )

        return PermissionVerdict(
            decision="allow",
            reason=f"Tool allowed: {check.tool}",
            source="annotation" if check.read_only else "mode",
        )

    def _resolve_ask(
        self,
        check: PermissionCheck,
        asks: list[_CompiledRule],
        resources: Mapping[str, str],
    ) -> PermissionVerdict:
        always = bool(check.hook_ask_reason) or any(item.rule.always_ask for item in asks)
        key: GrantKey = (check.server, check.tool, str(resources.get("domain", "") or ""))
        if asks:
            reason, source, rule_id = asks[0].reason(check), asks[0].source, asks[0].rule.id
        else:
            reason, source, rule_id = check.hook_ask_reason, "hook", ""

        if not always and key in self._grants:
            return PermissionVerdict(
                decision="allow",
                reason=f"Approved for this session: {check.tool}",
                source="grant",
                rule_id=rule_id,
            )
        if self._mode == "bypass" and not always:
            return PermissionVerdict(
                decision="allow",
                reason=f"Approval bypassed (permission mode bypass): {check.tool}",
                source="mode",
                rule_id=rule_id,
            )
        if self._mode == "dont_ask":
            return PermissionVerdict(
                decision="deny",
                reason=(
                    f"Not executed: {reason.rstrip('.')}. No approval is possible in this run "
                    "(permission mode dont_ask)."
                ),
                source="mode",
                rule_id=rule_id,
            )
        return PermissionVerdict(
            decision="ask",
            reason=reason,
            source=source,
            rule_id=rule_id,
            always_ask=always,
            grant_key=None if always else key,
        )


__all__ = ["GrantKey", "PermissionEngine"]
