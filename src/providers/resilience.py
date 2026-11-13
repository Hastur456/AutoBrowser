"""One resilient model call over any adapter: retries, backoff and a call deadline.

:class:`ResilientChatModel` wraps a :class:`~src.llm.ChatModel` built by the registry and is
the only place a model call is repeated (``docs/decisions/2026-11-13-provider-call-
resilience.md``). The engine never retries and the adapters' clients never do either; the
decision rests on the classification the adapter put on the
:class:`~src.llm.ModelProviderError`, never on its text or on a vendor:

* ``safe`` failures (the model never started: connection, connect/write/pool timeout, 429,
  408/409/503/529) are retried up to ``llm.retry_max_attempts``;
* ``ambiguous`` ones (read timeout, a cut response, 500/502/504) only with
  ``llm.retry_ambiguous``, since the model may have generated and a repeat pays twice;
* ``never`` ones and anything that is not a provider error propagate at once, and a
  cancellation is never caught.

Pauses are full-jitter exponential (``retry_base_delay_seconds`` doubling up to
``retry_max_delay_seconds``), at least the provider's ``Retry-After``; a ``Retry-After``
above ``retry_after_max_seconds`` fails the call. Every attempt and pause fit in one call
deadline, a share of ``loop.progress_timeout_seconds`` below 1: the goal watchdog sees no
events during a call, so the provider error must come first.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from src.config import LLMSettings, LoopSettings
from src.contracts import ToolDef
from src.llm import ChatModel, ModelCallInfo, ModelProviderError, ModelResponse, ProviderAttempt
from src.messages import Message

_LOG = logging.getLogger(__name__)

#: Error kinds whose traceback is worth a debug log: transport failures (a status error's
#: text quotes the provider's body, which may echo a key).
_TRANSPORT_KINDS = frozenset({"connect", "timeout", "interrupted"})

_MS_PER_SECOND = 1000


@dataclass(frozen=True)
class ProviderCallPolicy:
    """When and how long to wait before repeating a failed model call."""

    max_attempts: int = 1
    retry_ambiguous: bool = False
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 8.0
    retry_after_max_seconds: float = 30.0
    #: Budget of the whole call, every attempt and pause included; ``None``: no deadline.
    deadline_seconds: float | None = None

    @classmethod
    def from_settings(cls, llm: LLMSettings, loop: LoopSettings) -> ProviderCallPolicy:
        return cls(
            max_attempts=llm.retry_max_attempts,
            retry_ambiguous=llm.retry_ambiguous,
            base_delay_seconds=llm.retry_base_delay_seconds,
            max_delay_seconds=llm.retry_max_delay_seconds,
            retry_after_max_seconds=llm.retry_after_max_seconds,
            deadline_seconds=llm.call_deadline_ratio * loop.progress_timeout_seconds,
        )

    def allows(self, error: ModelProviderError) -> bool:
        """May ``error`` be retried at all (whatever the attempt count)?"""

        safety = error.retry_safety
        return safety == "safe" or (safety == "ambiguous" and self.retry_ambiguous)

    def delay_for(self, attempt: int, retry_after: float | None, draw: float) -> float | None:
        """The pause after failed ``attempt`` (1-based); ``None`` when the provider asks for
        a longer wait than allowed. ``draw`` is a uniform number in ``[0, 1)``."""

        if retry_after is not None and retry_after > self.retry_after_max_seconds:
            return None
        ceiling = min(self.max_delay_seconds, self.base_delay_seconds * 2 ** max(0, attempt - 1))
        return max(draw * ceiling, retry_after or 0.0)


class ResilientChatModel(ChatModel):
    """A :class:`ChatModel` that repeats the failures its policy allows, within a deadline.

    A successful response carries :class:`~src.llm.ModelCallInfo` (attempts, wall time,
    the adapter's request id); a final error carries the same ``attempts`` and
    ``elapsed_ms`` and is chained to the last attempt's error.
    """

    def __init__(
        self,
        inner: ChatModel,
        policy: ProviderCallPolicy,
        *,
        provider: str = "",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        draw: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
        logger: logging.Logger | None = None,
    ) -> None:
        self.inner = inner
        self.policy = policy
        self._provider = provider
        self._sleep = sleep
        self._draw = draw
        self._clock = clock
        self._log = logger or _LOG

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[ToolDef] = (),
        **params: Any,
    ) -> ModelResponse:
        """Return one model response for ``messages``, optionally binding tools."""

        started = self._clock()
        deadline = (
            None if self.policy.deadline_seconds is None else started + self.policy.deadline_seconds
        )
        attempts: list[ProviderAttempt] = []
        delay = 0.0
        for number in range(1, max(self.policy.max_attempts, 1) + 1):
            attempt_started = self._clock()
            try:
                response = await self._attempt(messages, tools, params, deadline)
            except ModelProviderError as error:
                attempts.append(self._record(number, attempt_started, delay, error))
                next_delay = self._next_delay(error, number, deadline)
                if next_delay is None:
                    raise self._final(error, attempts, started) from error
                self._log_retry(error, number, next_delay)
                await self._sleep(next_delay)
                delay = next_delay
                continue
            attempts.append(self._record(number, attempt_started, delay, None))
            call = response.call or ModelCallInfo()
            return replace(
                response,
                call=replace(call, attempts=tuple(attempts), elapsed_ms=self._ms_since(started)),
            )
        raise AssertionError("unreachable: the last attempt either returns or raises")

    async def _attempt(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        params: dict[str, Any],
        deadline: float | None,
    ) -> ModelResponse:
        if deadline is None:
            return await self.inner.complete(messages, tools=tools, **params)
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise self._deadline_error()
        try:
            async with asyncio.timeout(remaining) as scope:
                return await self.inner.complete(messages, tools=tools, **params)
        except TimeoutError as exc:
            if not scope.expired():
                raise  # the inner model's own TimeoutError, not our deadline
            raise self._deadline_error() from exc

    def _next_delay(
        self, error: ModelProviderError, number: int, deadline: float | None
    ) -> float | None:
        """The pause before the next attempt, ``None`` when the call ends with ``error``."""

        if number >= self.policy.max_attempts or not self.policy.allows(error):
            return None
        delay = self.policy.delay_for(number, error.retry_after_seconds, self._draw())
        if delay is None:
            return None
        if deadline is not None and self._clock() + delay >= deadline:
            return None
        return delay

    def _final(
        self, error: ModelProviderError, attempts: list[ProviderAttempt], started: float
    ) -> ModelProviderError:
        return error.with_details(attempts=tuple(attempts), elapsed_ms=self._ms_since(started))

    def _deadline_error(self) -> ModelProviderError:
        seconds = self.policy.deadline_seconds
        return ModelProviderError(
            f"{self._provider or 'model'}: no answer within the call deadline of {seconds:g} s",
            provider=self._provider,
            kind="timeout",
            phase="deadline",
        )

    def _record(
        self, number: int, started: float, delay: float, error: ModelProviderError | None
    ) -> ProviderAttempt:
        return ProviderAttempt(
            attempt=number,
            elapsed_ms=self._ms_since(started),
            delay_ms=int(delay * _MS_PER_SECOND),
            kind=error.kind if error is not None else None,
            phase=(error.phase or error.cause) if error is not None else None,
            status_code=error.status_code if error is not None else None,
        )

    def _log_retry(self, error: ModelProviderError, number: int, delay: float) -> None:
        # The message is safe by contract (no key, no header value); the traceback only
        # for transport failures, whose texts quote no response body.
        self._log.warning(
            "model provider attempt %d/%d failed (%s): %s; retrying in %.1f s",
            number,
            self.policy.max_attempts,
            "/".join(part for part in (error.kind, error.phase or error.cause) if part),
            error,
            delay,
        )
        if error.kind in _TRANSPORT_KINDS:
            self._log.debug("model provider failure detail", exc_info=error)

    def _ms_since(self, started: float) -> int:
        return int((self._clock() - started) * _MS_PER_SECOND)


__all__ = ["ProviderCallPolicy", "ResilientChatModel"]
