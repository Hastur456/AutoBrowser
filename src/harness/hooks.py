"""Deterministic lifecycle hooks around the engine-native agent loop.

A hook is an ``async`` Python callable registered in ``settings.hooks.registry`` for one
:data:`~src.contracts.HookEventName`. At fixed lifecycle points the loop builds a
:class:`~src.contracts.HookEvent` and calls :meth:`HookEngine.run`; the engine runs the
matching handlers **sequentially in registry order** and folds their
:class:`~src.contracts.HookResult`\\ s into one :class:`HookOutcome`:

* decisions aggregate as ``deny > ask > allow > None``; the first ``deny`` short-circuits the
  remaining handlers;
* ``updated_input`` (``pre_tool_use``) and ``updated_output`` (``post_tool_use*``) chain —
  the next handler sees the already rewritten ``args``/``result``;
* ``additional_context`` of all handlers is concatenated in execution order.

A handler that times out or raises gets the per-event failure decision
(:data:`FAIL_CLOSED_EVENTS` deny, every other event has no decision) unless its spec sets
``fail_closed`` explicitly. Every handler run produces a :class:`HookDecisionRecord` that is
handed to ``on_record``; the engine owns no ``EventEmitter`` — the loop emits the records as
``hook.decided`` through the same emitter the ``GoalRunner`` watchdog polls.

The engine never sees ``LoopState``: the loop translates a :class:`HookOutcome` into state
updates itself. :class:`NullHookEngine` (``has()`` is always ``False``) is what the loop gets
when hooks are disabled, so no :class:`~src.contracts.HookEvent` is even built then.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from src.config import TOOL_HOOK_EVENTS, HookSpec, HooksSettings
from src.contracts import (
    HookDecision,
    HookEvent,
    HookEventName,
    HookHandler,
    HookResult,
)

#: Events whose handler failure (timeout/exception) denies by default.
FAIL_CLOSED_EVENTS: frozenset[str] = frozenset({"goal_start", "pre_tool_use"})

#: Events whose ``updated_output`` rewrites the tool result.
POST_TOOL_EVENTS: frozenset[str] = frozenset({"post_tool_use", "post_tool_use_failure"})

_DECISION_RANK: dict[str | None, int] = {None: 0, "allow": 1, "ask": 2, "deny": 3}


class HookConfigError(ValueError):
    """The hook registry cannot be loaded; raised at session start, never mid-task."""


@dataclass(frozen=True)
class HookDecisionRecord:
    """What one handler run decided — the payload of one ``hook.decided`` event."""

    hook_id: str
    event: HookEventName
    decision: HookDecision | None
    reason: str
    modified: bool
    duration_ms: int
    #: ``"timeout"`` or ``"<ExcType>: message"`` when the handler failed.
    error: str = ""
    #: Why the handler was not run (e.g. ``"stop_budget_exhausted"``).
    skipped: str = ""


@dataclass(frozen=True)
class HookOutcome:
    """Aggregated result of all handlers that ran for one event."""

    decision: HookDecision | None = None
    reason: str = ""
    updated_input: dict[str, Any] | None = None
    updated_output: str | None = None
    additional_context: str = ""
    records: tuple[HookDecisionRecord, ...] = ()


@dataclass(frozen=True)
class RegisteredHook:
    """One loaded hook: a resolved async handler plus its match filter and limits."""

    id: str
    event: HookEventName
    handler: HookHandler
    server: str = ""
    tool: re.Pattern[str] | None = None
    timeout_seconds: float = 10.0
    fail_closed: bool | None = None

    def matches(self, event: HookEvent) -> bool:
        """Empty filter matches all; ``server`` is exact; ``tool`` is ``re.fullmatch``."""

        if event.name not in TOOL_HOOK_EVENTS:
            return True
        if self.server and event.server != self.server:
            return False
        return self.tool is None or self.tool.fullmatch(event.tool) is not None

    def failure_denies(self) -> bool:
        if self.fail_closed is not None:
            return self.fail_closed
        return self.event in FAIL_CLOSED_EVENTS


OnRecord = Callable[[HookDecisionRecord], None]


class NullHookEngine:
    """Hook engine with no hooks: the loop's fast path when hooks are disabled."""

    def has(self, name: HookEventName) -> bool:
        return False

    async def run(self, event: HookEvent, *, on_record: OnRecord | None = None) -> HookOutcome:
        return HookOutcome()

    def skip(self, name: HookEventName, reason: str) -> tuple[HookDecisionRecord, ...]:
        return ()


class HookEngine:
    """Run registered lifecycle hooks for one session (see the module docstring)."""

    def __init__(
        self,
        hooks: Sequence[RegisteredHook],
        *,
        progress_timeout_seconds: float | None = None,
    ) -> None:
        seen: set[str] = set()
        by_event: dict[str, list[RegisteredHook]] = {}
        for hook in hooks:
            if hook.id in seen:
                raise HookConfigError(f"Duplicate hook id: {hook.id!r}.")
            seen.add(hook.id)
            if not _is_async_callable(hook.handler):
                raise HookConfigError(
                    f"Hook {hook.id!r}: handler must be an async callable (a sync handler "
                    "cannot be interrupted on timeout)."
                )
            if progress_timeout_seconds is not None and (
                hook.timeout_seconds >= progress_timeout_seconds
            ):
                raise HookConfigError(
                    f"Hook {hook.id!r}: timeout {hook.timeout_seconds}s must be below "
                    f"loop.progress_timeout_seconds ({progress_timeout_seconds}s)."
                )
            by_event.setdefault(hook.event, []).append(hook)
        self._hooks: dict[str, tuple[RegisteredHook, ...]] = {
            event: tuple(items) for event, items in by_event.items()
        }

    @classmethod
    def from_settings(
        cls,
        hooks: HooksSettings,
        *,
        progress_timeout_seconds: float,
    ) -> HookEngine | NullHookEngine:
        """Load ``hooks.registry``; disabled settings give a :class:`NullHookEngine`.

        Handlers are imported here, so an import error, a duplicate id, a sync handler or a
        timeout not below ``progress_timeout_seconds`` fails session start instead of being
        skipped silently.
        """

        if not hooks.enabled:
            return NullHookEngine()
        if hooks.default_timeout_seconds >= progress_timeout_seconds:
            raise HookConfigError(
                f"hooks.default_timeout_seconds ({hooks.default_timeout_seconds}s) must be "
                f"below loop.progress_timeout_seconds ({progress_timeout_seconds}s)."
            )
        loaded = [
            _load_hook(spec, default_timeout=hooks.default_timeout_seconds)
            for spec in hooks.registry
        ]
        return cls(loaded, progress_timeout_seconds=progress_timeout_seconds)

    def has(self, name: HookEventName) -> bool:
        """Whether any hook is registered for ``name`` (lets the loop skip building events)."""

        return bool(self._hooks.get(name))

    def skip(self, name: HookEventName, reason: str) -> tuple[HookDecisionRecord, ...]:
        """Records for the hooks of ``name`` that the loop decided not to run."""

        return tuple(
            HookDecisionRecord(
                hook_id=hook.id,
                event=name,
                decision=None,
                reason="",
                modified=False,
                duration_ms=0,
                skipped=reason,
            )
            for hook in self._hooks.get(name, ())
        )

    async def run(self, event: HookEvent, *, on_record: OnRecord | None = None) -> HookOutcome:
        """Run the matching handlers for ``event`` in order and aggregate their results."""

        current = event
        decision: HookDecision | None = None
        reason = ""
        updated_input: dict[str, Any] | None = None
        updated_output: str | None = None
        contexts: list[str] = []
        records: list[HookDecisionRecord] = []

        for hook in self._hooks.get(event.name, ()):
            if not hook.matches(current):
                continue

            started = time.perf_counter()
            result, error = await _call(hook, current)
            duration_ms = int((time.perf_counter() - started) * 1000)
            if error and hook.failure_denies():
                result = HookResult(decision="deny", reason=f"Hook {hook.id} failed: {error}")

            modified = False
            if result is not None:
                if result.updated_input is not None and event.name == "pre_tool_use":
                    updated_input = dict(result.updated_input)
                    current = replace(current, args=dict(updated_input))
                    modified = True
                if result.updated_output is not None and event.name in POST_TOOL_EVENTS:
                    updated_output = str(result.updated_output)
                    current = replace(
                        current,
                        result=_with_output(current.result, updated_output),
                    )
                    modified = True
                if result.additional_context:
                    contexts.append(result.additional_context)
                if _DECISION_RANK[result.decision] > _DECISION_RANK[decision]:
                    decision = result.decision
                    reason = result.reason

            record = HookDecisionRecord(
                hook_id=hook.id,
                event=event.name,
                decision=None if result is None else result.decision,
                reason="" if result is None else result.reason,
                modified=modified,
                duration_ms=duration_ms,
                error=error,
            )
            records.append(record)
            if on_record is not None:
                on_record(record)
            if decision == "deny":
                break

        return HookOutcome(
            decision=decision,
            reason=reason,
            updated_input=updated_input,
            updated_output=updated_output,
            additional_context="\n\n".join(contexts),
            records=tuple(records),
        )


def registry_digest(hooks: HooksSettings) -> str:
    """SHA-256 of the canonical JSON of ``hooks.registry`` (recorded in ``session.json``)."""

    payload = [spec.model_dump(mode="json") for spec in hooks.registry]
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _call(hook: RegisteredHook, event: HookEvent) -> tuple[HookResult | None, str]:
    """Run one handler under its timeout; failures become an error string, never raise."""

    try:
        result = await asyncio.wait_for(hook.handler(event), timeout=hook.timeout_seconds)
    except TimeoutError:
        return None, "timeout"
    except Exception as exc:  # noqa: BLE001 - a broken hook must not crash the task
        return None, f"{type(exc).__name__}: {exc}"
    if result is not None and not isinstance(result, HookResult):
        return None, f"TypeError: handler returned {type(result).__name__}, not HookResult"
    if result is not None and result.decision not in _DECISION_RANK:
        return None, f"ValueError: unknown decision {result.decision!r}"
    return result, ""


def _with_output(result: dict[str, Any], output: str) -> dict[str, Any]:
    """Return ``result`` with ``output`` as its content (or its error, for a failure)."""

    rewritten = dict(result)
    if rewritten.get("status") == "success":
        rewritten["content"] = output
    else:
        rewritten["error"] = output
    return rewritten


def _is_async_callable(value: Any) -> bool:
    if inspect.iscoroutinefunction(value):
        return True
    call = getattr(value, "__call__", None)  # noqa: B004 - objects with async __call__
    return inspect.iscoroutinefunction(call)


def _import_target(path: str, hook_id: str) -> Any:
    module_name, _, attr_path = path.partition(":")
    try:
        target: Any = importlib.import_module(module_name)
    except ImportError as exc:
        raise HookConfigError(f"Hook {hook_id!r}: cannot import {module_name!r}: {exc}") from exc
    for attr in attr_path.split("."):
        try:
            target = getattr(target, attr)
        except AttributeError as exc:
            raise HookConfigError(
                f"Hook {hook_id!r}: {module_name!r} has no attribute {attr_path!r}."
            ) from exc
    return target


def _load_hook(spec: HookSpec, *, default_timeout: float) -> RegisteredHook:
    target = _import_target(spec.handler, spec.id)
    if spec.options:
        try:
            handler = target(**spec.options)
        except Exception as exc:
            raise HookConfigError(
                f"Hook {spec.id!r}: factory {spec.handler!r} rejected its options: {exc}"
            ) from exc
    else:
        handler = target
    if not _is_async_callable(handler):
        hint = "" if spec.options else " (a factory needs `options` to be called)"
        raise HookConfigError(
            f"Hook {spec.id!r}: {spec.handler!r} is not an async handler{hint}."
        )
    return RegisteredHook(
        id=spec.id,
        event=spec.event,
        handler=handler,
        server=spec.match.server,
        tool=re.compile(spec.match.tool) if spec.match.tool else None,
        timeout_seconds=(
            spec.timeout_seconds if spec.timeout_seconds is not None else default_timeout
        ),
        fail_closed=spec.fail_closed,
    )


__all__ = [
    "FAIL_CLOSED_EVENTS",
    "POST_TOOL_EVENTS",
    "HookConfigError",
    "HookDecisionRecord",
    "HookEngine",
    "HookOutcome",
    "NullHookEngine",
    "RegisteredHook",
    "registry_digest",
]
