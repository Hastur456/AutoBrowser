"""Scenario eval harness for the current LangGraph agent loop."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from langchain_core.language_models.fake import FakeListLLM

from src.agent.agent import build_agent_graph
from src.agent_loop.events import EventEmitter, InMemoryEventSink
from src.agent_loop.replay import TraceSummary, print_action_sequence, summarize_trace
from src.browser import FakeBrowserProvider
from src.harness.runtime import HARNESS_EVENT_METADATA_CONFIG_KEY, BrowserHarness
from src.harness.tools import ToolRegistry


@dataclass(frozen=True)
class EvalAssertions:
    """Assertions configured by a scenario fixture."""

    expected_terminal_status: str = "completed"
    final_answer_contains: list[str] = field(default_factory=list)
    max_tool_calls: int | None = None
    max_repeated_actions: int | None = None
    max_policy_blocks: int | None = None


@dataclass(frozen=True)
class EvalScenario:
    """One replayable browser-agent eval scenario."""

    name: str
    task: str
    model_responses: list[str]
    browser_snapshots: list[str]
    assertions: EvalAssertions
    recursion_limit: int = 25


@dataclass(frozen=True)
class EvalResult:
    """Scenario result with metrics and compact trace output."""

    scenario_name: str
    summary: TraceSummary
    action_sequence: str
    final_state: dict[str, Any]

    def metrics(self) -> dict[str, Any]:
        return {
            "terminal_status": self.summary.terminal_status,
            "final_answer": self.summary.final_answer,
            "model_turn_count": self.summary.model_turn_count,
            "tool_call_count": self.summary.tool_call_count,
            "policy_block_count": self.summary.policy_block_count,
            "repeated_action_count": self.summary.repeated_action_count,
            "trace_complete": self.summary.trace_complete,
        }


def load_scenario(path: Path) -> EvalScenario:
    """Load an eval scenario from YAML."""

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    assertions = data.get("assertions") or {}
    return EvalScenario(
        name=str(data["name"]),
        task=str(data["task"]),
        model_responses=[
            _json_response(response)
            for response in (data.get("model", {}).get("responses") or [])
        ],
        browser_snapshots=[str(item) for item in data.get("browser", {}).get("snapshots") or []],
        assertions=EvalAssertions(
            expected_terminal_status=str(
                assertions.get("expected_terminal_status", "completed")
            ),
            final_answer_contains=[
                str(item) for item in assertions.get("final_answer_contains", [])
            ],
            max_tool_calls=assertions.get("max_tool_calls"),
            max_repeated_actions=assertions.get("max_repeated_actions"),
            max_policy_blocks=assertions.get("max_policy_blocks"),
        ),
        recursion_limit=int(data.get("recursion_limit", 25) or 25),
    )


async def run_scenario(scenario: EvalScenario) -> EvalResult:
    """Run one scenario against the current LangGraph loop."""

    sink = InMemoryEventSink()
    session_id = f"eval-{uuid4().hex}"
    task_id = f"task-{uuid4().hex}"
    emitter = EventEmitter(sink, session_id=session_id)
    provider = FakeBrowserProvider(scenario.browser_snapshots)
    harness = BrowserHarness(
        build_agent_graph,
        llm=FakeListLLM(responses=scenario.model_responses),
        tool_registry=ToolRegistry(providers=[provider]),
        event_emitter=emitter,
    )
    emitter.emit(
        "goal.started",
        source="agent_loop.evals",
        payload={"task": scenario.task},
        task_id=task_id,
        goal_id=task_id,
    )
    final_state: dict[str, Any] = {}
    try:
        async for chunk in harness.stream_updates(
            scenario.task,
            config={
                "recursion_limit": scenario.recursion_limit,
                HARNESS_EVENT_METADATA_CONFIG_KEY: {
                    "session_id": session_id,
                    "task_id": task_id,
                    "goal_id": task_id,
                },
            },
        ):
            final_state = _state_from_chunk(chunk)
    except Exception as exc:
        emitter.emit(
            "goal.failed",
            source="agent_loop.evals",
            payload={"task": scenario.task, "error": exc},
            task_id=task_id,
            goal_id=task_id,
        )
    else:
        emitter.emit(
            "goal.completed",
            source="agent_loop.evals",
            payload={"task": scenario.task, "result": final_state},
            task_id=task_id,
            goal_id=task_id,
        )
    events = sink.records
    summary = summarize_trace(events)
    return EvalResult(
        scenario_name=scenario.name,
        summary=summary,
        action_sequence=print_action_sequence(events),
        final_state=final_state,
    )


def assert_scenario_result(scenario: EvalScenario, result: EvalResult) -> None:
    """Assert configured scenario expectations with compact trace diagnostics."""

    metrics = result.metrics()
    action_sequence = result.action_sequence
    expected_status = scenario.assertions.expected_terminal_status
    assert result.summary.terminal_status == expected_status, action_sequence
    for expected_text in scenario.assertions.final_answer_contains:
        assert expected_text in result.summary.final_answer, action_sequence
    if scenario.assertions.max_tool_calls is not None:
        assert result.summary.tool_call_count <= scenario.assertions.max_tool_calls, (
            metrics,
            action_sequence,
        )
    if scenario.assertions.max_repeated_actions is not None:
        assert (
            result.summary.repeated_action_count
            <= scenario.assertions.max_repeated_actions
        ), (metrics, action_sequence)
    if scenario.assertions.max_policy_blocks is not None:
        assert result.summary.policy_block_count <= scenario.assertions.max_policy_blocks, (
            metrics,
            action_sequence,
        )
    assert result.summary.trace_complete is True, action_sequence


def _json_response(response: Any) -> str:
    if isinstance(response, str):
        return response
    return json.dumps(response, ensure_ascii=False)


def _state_from_chunk(chunk: Any) -> dict[str, Any]:
    if not isinstance(chunk, dict) or not chunk:
        return {}
    nested = next(reversed(chunk.values()))
    return dict(nested) if isinstance(nested, dict) else {}


__all__ = [
    "EvalAssertions",
    "EvalResult",
    "EvalScenario",
    "assert_scenario_result",
    "load_scenario",
    "run_scenario",
]
