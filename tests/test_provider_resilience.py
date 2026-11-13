"""Tests for the retry layer (:mod:`src.providers.resilience`), no network.

A fake inner model fails or answers from a script; sleep, jitter and the clock are fakes,
so every pause and deadline is exact. The adapters' classification has its own tests
(``tests/test_openai_compat_provider.py``, ``tests/test_ollama_provider.py``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from src.config import LLMSettings, LoopSettings
from src.llm import ModelCallInfo, ModelProviderError, ModelResponse
from src.messages import user_message
from src.providers.resilience import ProviderCallPolicy, ResilientChatModel

MESSAGES = [user_message("hi")]


def connect_error(**fields: Any) -> ModelProviderError:
    return ModelProviderError(
        "p: connection failed (refused; ConnectError)",
        provider="p",
        kind="connect",
        cause="refused",
        **fields,
    )


def read_timeout() -> ModelProviderError:
    return ModelProviderError("p: read timed out after 60 s", provider="p", kind="timeout", phase="read")


def status(code: int, kind: str, **fields: Any) -> ModelProviderError:
    return ModelProviderError(f"p {code}: failed", provider="p", status_code=code, kind=kind, **fields)


class ScriptedModel:
    """Fails or answers in order; a ``ModelResponse`` in the script is returned."""

    def __init__(self, *script: Any) -> None:
        self.script = list(script)
        self.calls = 0

    async def complete(self, messages: Any, *, tools: Any = (), **params: Any) -> ModelResponse:
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def resilient(
    inner: Any, clock: FakeClock | None = None, *, draw: float = 0.5, **policy: Any
) -> ResilientChatModel:
    clock = clock or FakeClock()
    defaults: dict[str, Any] = {"max_attempts": 3, "base_delay_seconds": 1.0, "max_delay_seconds": 8.0}
    return ResilientChatModel(
        inner,
        ProviderCallPolicy(**{**defaults, **policy}),
        provider="p",
        sleep=clock.sleep,
        draw=lambda: draw,
        clock=clock,
    )


def ok(**fields: Any) -> ModelResponse:
    return ModelResponse(content="ok", **fields)


# --------------------------------------------------------------------------
# Retries
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_connection_failure_is_retried_and_the_answer_reports_the_attempts() -> None:
    inner = ScriptedModel(connect_error(), ok(call=ModelCallInfo(request_id="req-1")))
    clock = FakeClock()

    response = await resilient(inner, clock).complete(MESSAGES)

    assert response.content == "ok"
    assert inner.calls == 2
    assert clock.sleeps == [0.5]  # draw 0.5 x base 1 s
    call = response.call
    assert call is not None and call.request_id == "req-1"
    assert [(a.attempt, a.kind, a.phase) for a in call.attempts] == [
        (1, "connect", "refused"),
        (2, None, None),
    ]
    assert call.attempts[1].delay_ms == 500
    assert call.to_json_dict()["attempts"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        status(429, "rate_limited"),
        status(503, "unavailable"),
        ModelProviderError("p: connect timed out", provider="p", kind="timeout", phase="connect"),
        ModelProviderError("p: pool timed out", provider="p", kind="timeout", phase="pool"),
        ModelProviderError("p: write timed out", provider="p", kind="timeout", phase="write"),
    ],
)
async def test_failures_where_the_model_never_started_are_retried(error: ModelProviderError) -> None:
    inner = ScriptedModel(error, ok())

    assert (await resilient(inner).complete(MESSAGES)).content == "ok"
    assert inner.calls == 2


@pytest.mark.asyncio
async def test_exhausted_attempts_raise_the_last_error_with_every_attempt() -> None:
    first, second, last = connect_error(), connect_error(), connect_error()
    inner = ScriptedModel(first, second, last)
    clock = FakeClock()

    with pytest.raises(ModelProviderError) as caught:
        await resilient(inner, clock).complete(MESSAGES)

    error = caught.value
    assert inner.calls == 3
    assert [a.attempt for a in error.attempts] == [1, 2, 3]
    assert clock.sleeps == [0.5, 1.0]  # draw 0.5 x 1 s, then x 2 s
    assert error.elapsed_ms == 1500
    assert error.__cause__ is last  # the original stays chained
    assert str(error) == str(last)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        read_timeout(),
        status(500, "server_error"),
        status(502, "server_error"),
        ModelProviderError("p: response interrupted", provider="p", kind="interrupted"),
    ],
)
async def test_ambiguous_failures_are_not_retried_by_default(error: ModelProviderError) -> None:
    inner = ScriptedModel(error, ok())

    with pytest.raises(ModelProviderError):
        await resilient(inner).complete(MESSAGES)

    assert inner.calls == 1


@pytest.mark.asyncio
async def test_ambiguous_failures_are_retried_when_allowed() -> None:
    inner = ScriptedModel(read_timeout(), status(502, "server_error"), ok())

    assert (await resilient(inner, retry_ambiguous=True).complete(MESSAGES)).content == "ok"
    assert inner.calls == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        status(401, "auth"),
        status(400, "bad_request"),
        status(404, "not_found"),
        ModelProviderError("p: empty response", provider="p", kind="empty_response"),
        ModelProviderError("p: odd", provider="p"),  # unclassified, not retryable
    ],
)
async def test_failures_a_repeat_cannot_fix_are_raised_at_once(error: ModelProviderError) -> None:
    inner = ScriptedModel(error, ok())

    with pytest.raises(ModelProviderError):
        await resilient(inner, retry_ambiguous=True).complete(MESSAGES)

    assert inner.calls == 1


@pytest.mark.asyncio
async def test_one_attempt_means_no_retry() -> None:
    inner = ScriptedModel(connect_error(), ok())

    with pytest.raises(ModelProviderError):
        await resilient(inner, max_attempts=1).complete(MESSAGES)

    assert inner.calls == 1


@pytest.mark.asyncio
async def test_other_exceptions_propagate_without_a_retry() -> None:
    inner = ScriptedModel(KeyError("bug"), ok())

    with pytest.raises(KeyError):
        await resilient(inner).complete(MESSAGES)

    assert inner.calls == 1


# --------------------------------------------------------------------------
# Backoff and Retry-After
# --------------------------------------------------------------------------


def test_the_backoff_is_full_jitter_doubling_up_to_the_ceiling() -> None:
    policy = ProviderCallPolicy(max_attempts=10, base_delay_seconds=1.0, max_delay_seconds=8.0)

    assert [policy.delay_for(n, None, 1.0) for n in (1, 2, 3, 4, 5)] == [1.0, 2.0, 4.0, 8.0, 8.0]
    assert policy.delay_for(3, None, 0.0) == 0.0
    assert policy.delay_for(3, None, 0.25) == 1.0


@pytest.mark.asyncio
async def test_retry_after_is_waited_out() -> None:
    inner = ScriptedModel(status(429, "rate_limited", retry_after_seconds=3.0), ok())
    clock = FakeClock()

    await resilient(inner, clock).complete(MESSAGES)

    assert clock.sleeps == [3.0]


@pytest.mark.asyncio
async def test_a_retry_after_above_the_cap_fails_at_once() -> None:
    inner = ScriptedModel(status(429, "rate_limited", retry_after_seconds=120.0), ok())

    with pytest.raises(ModelProviderError) as caught:
        await resilient(inner, retry_after_max_seconds=30.0).complete(MESSAGES)

    assert inner.calls == 1
    assert caught.value.retry_after_seconds == 120.0


# --------------------------------------------------------------------------
# The call deadline
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pause_that_would_pass_the_deadline_ends_the_call() -> None:
    inner = ScriptedModel(connect_error(), ok())
    clock = FakeClock()

    with pytest.raises(ModelProviderError):
        # The 0.5 s pause would end exactly at the 0.5 s deadline.
        await resilient(inner, clock, deadline_seconds=0.5).complete(MESSAGES)

    assert inner.calls == 1
    assert clock.sleeps == []


@pytest.mark.asyncio
async def test_a_hung_call_is_cut_by_the_deadline() -> None:
    class Hung:
        async def complete(self, *args: Any, **kwargs: Any) -> ModelResponse:
            await asyncio.sleep(10)
            return ok()

    model = ResilientChatModel(Hung(), ProviderCallPolicy(max_attempts=3, deadline_seconds=0.05), provider="p")

    with pytest.raises(ModelProviderError) as caught:
        await model.complete(MESSAGES)

    error = caught.value
    assert (error.kind, error.phase, error.retry_safety) == ("timeout", "deadline", "never")
    assert "call deadline of 0.05 s" in str(error)
    assert len(error.attempts) == 1


@pytest.mark.asyncio
async def test_the_inner_models_own_timeout_error_is_not_taken_for_the_deadline() -> None:
    inner = ScriptedModel(TimeoutError("inner"), ok())

    with pytest.raises(TimeoutError, match="inner"):
        await resilient(inner, deadline_seconds=30.0).complete(MESSAGES)


def test_the_deadline_is_a_share_of_the_goal_stall_budget() -> None:
    policy = ProviderCallPolicy.from_settings(
        LLMSettings(call_deadline_ratio=0.5), LoopSettings(progress_timeout_seconds=100.0)
    )

    assert policy.deadline_seconds == 50.0
    assert (policy.max_attempts, policy.retry_ambiguous) == (3, False)


# --------------------------------------------------------------------------
# Cancellation and logging
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_cancellation_during_a_pause_is_never_swallowed() -> None:
    inner = ScriptedModel(connect_error(), ok())

    async def cancelled_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    model = ResilientChatModel(
        inner, ProviderCallPolicy(max_attempts=3), provider="p", sleep=cancelled_sleep
    )

    with pytest.raises(asyncio.CancelledError):
        await model.complete(MESSAGES)

    assert inner.calls == 1


@pytest.mark.asyncio
async def test_a_cancellation_during_an_attempt_is_never_retried() -> None:
    inner = ScriptedModel(asyncio.CancelledError(), ok())

    with pytest.raises(asyncio.CancelledError):
        await resilient(inner).complete(MESSAGES)

    assert inner.calls == 1


@pytest.mark.asyncio
async def test_a_retry_is_logged_without_secrets(caplog: pytest.LogCaptureFixture) -> None:
    inner = ScriptedModel(status(503, "unavailable"), ok())

    with caplog.at_level(logging.WARNING, logger="src.providers.resilience"):
        await resilient(inner).complete(MESSAGES)

    assert "attempt 1/3 failed (unavailable): p 503: failed; retrying in 0.5 s" in caplog.text
    assert "Bearer" not in caplog.text
