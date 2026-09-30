"""Importable hook handlers for tests (referenced as ``tests.hook_fixtures:<name>``).

``CALLS`` records ``(label, event)`` for every run of a :func:`scripted` handler so tests
can assert execution order; reset it per test.
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.contracts import HookEvent, HookResult

CALLS: list[tuple[str, HookEvent]] = []


def scripted(
    label: str = "",
    decision: str | None = None,
    reason: str = "",
    context: str = "",
    updated_input: dict[str, Any] | None = None,
    updated_output: str | None = None,
    sleep: float = 0.0,
    error: str = "",
) -> Any:
    """Factory: a handler that records its call and returns a fixed :class:`HookResult`."""

    async def handler(event: HookEvent) -> HookResult | None:
        CALLS.append((label, event))
        if sleep:
            await asyncio.sleep(sleep)
        if error:
            raise RuntimeError(error)
        if decision is None and not (reason or context or updated_input or updated_output):
            return None
        return HookResult(
            decision=decision,  # type: ignore[arg-type]
            reason=reason,
            updated_input=updated_input,
            updated_output=updated_output,
            additional_context=context,
        )

    return handler


async def deny_all(event: HookEvent) -> HookResult:
    CALLS.append(("deny_all", event))
    return HookResult(decision="deny", reason="denied by deny_all")


async def no_opinion(event: HookEvent) -> None:
    CALLS.append(("no_opinion", event))


def sync_handler(event: HookEvent) -> HookResult:
    return HookResult(decision="allow")


class AsyncCallable:
    """Handler object with an async ``__call__`` (built through ``options``)."""

    def __init__(self, reason: str = "") -> None:
        self.reason = reason

    async def __call__(self, event: HookEvent) -> HookResult:
        CALLS.append(("callable", event))
        return HookResult(decision="allow", reason=self.reason)


NOT_CALLABLE = 42
