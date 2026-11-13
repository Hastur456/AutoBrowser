"""Provider diagnostics reach the session trace: ``model.responded`` carries how a call
went (``call``), ``goal.failed`` the classification of the provider error that ended it.

The engine stays out of retrying: it only reports what the model layer recorded
(``docs/decisions/2026-11-13-provider-call-resilience.md``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.agent_loop.events import EventEmitter, EventRecord, InMemoryEventSink
from src.agent_loop.execution.loop import Agent, AgentLoopEngine
from src.agent_loop.execution.resources import EngineResources
from src.agent_loop.goals import GoalLimits, GoalRunner
from src.contracts import TaskRequest
from src.harness.hooks import NullHookEngine
from src.harness.runtime import BrowserHarness
from src.harness.tools import ToolRegistry
from src.llm import ModelCallInfo, ModelProviderError, ModelResponse, ProviderAttempt
from src.providers.resilience import ProviderCallPolicy, ResilientChatModel

PLAN = {"steps": [{"id": 1, "description": "Read the price", "status": "pending"}]}
DONE = {"decision": "done", "final_answer": "All done."}


class ScriptedModel:
    def __init__(self, *script: Any) -> None:
        self.script = list(script)

    async def complete(self, messages: Any, *, tools: Any = (), **params: Any) -> ModelResponse:
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


async def no_sleep(_seconds: float) -> None:
    return None


def reply(payload: dict[str, Any], **fields: Any) -> ModelResponse:
    return ModelResponse(content=json.dumps(payload), **fields)


def resources_for(llm: Any, emitter: EventEmitter) -> EngineResources:
    harness = BrowserHarness(llm=llm, tool_registry=ToolRegistry(tools=[]), event_emitter=emitter)
    return EngineResources.from_harness(harness, llm=llm, events=emitter, hooks=NullHookEngine())


def of_type(records: list[EventRecord], event_type: str) -> list[EventRecord]:
    return [record for record in records if record.type == event_type]


@pytest.mark.asyncio
async def test_model_responded_reports_the_attempts_of_a_retried_call() -> None:
    sink = InMemoryEventSink()
    emitter = EventEmitter(sink, session_id="session-1")
    refused = ModelProviderError(
        "p: connection failed (refused; ConnectError)", provider="p", kind="connect", cause="refused"
    )
    inner = ScriptedModel(
        refused, reply(PLAN, call=ModelCallInfo(request_id="req-plan")), reply(DONE)
    )
    llm = ResilientChatModel(inner, ProviderCallPolicy(max_attempts=3), provider="p", sleep=no_sleep)

    result = await AgentLoopEngine(resources_for(llm, emitter)).run(
        "Find the price.", task_id="task-1", goal_id="task-1", session_id="session-1"
    )

    assert result.status == "done"
    plan, agent = of_type(list(sink.records), "model.responded")
    assert plan.payload["call"]["attempts"] == 2
    assert plan.payload["call"]["request_id"] == "req-plan"
    failed = plan.payload["call"]["failed_attempts"]
    assert [(a["attempt"], a["kind"], a["phase"]) for a in failed] == [(1, "connect", "refused")]
    assert agent.payload["call"]["attempts"] == 1


@pytest.mark.asyncio
async def test_goal_failed_carries_the_classified_provider_error() -> None:
    sink = InMemoryEventSink()
    emitter = EventEmitter(sink, session_id="session-1")
    timeout = ModelProviderError(
        "openai_compat: read timed out after 60 s",
        provider="openai_compat",
        kind="timeout",
        phase="read",
        endpoint="https://llm.test/v1",
        model="m",
    )
    inner = ScriptedModel(timeout)
    llm = ResilientChatModel(inner, ProviderCallPolicy(max_attempts=3), provider="openai_compat")
    agent = Agent(resources_for(llm, emitter))
    runner = GoalRunner(
        task_runner=agent.run,
        events=emitter,
        limits=GoalLimits(timeout_seconds=5.0, progress_timeout_seconds=5.0),
    )

    with pytest.raises(ModelProviderError):
        await runner.run(TaskRequest(task="Find the price.", task_id="task-1", session_id="session-1"))

    (failed,) = of_type(list(sink.records), "goal.failed")
    error = failed.to_json_dict()["payload"]["error"]
    assert error["type"] == "ModelProviderError"
    assert (error["kind"], error["phase"], error["retry_safety"]) == ("timeout", "read", "ambiguous")
    assert (error["endpoint"], error["model"]) == ("https://llm.test/v1", "m")
    # A read timeout is not retried by default: one attempt.
    assert [attempt["attempt"] for attempt in error["attempts"]] == [1]


def test_provider_attempt_payload_leaves_out_unset_fields() -> None:
    assert ProviderAttempt(attempt=2, elapsed_ms=5).to_json_dict() == {
        "attempt": 2,
        "elapsed_ms": 5,
        "delay_ms": 0,
    }
