from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.agent_loop.evals import (
    EvalAssertions,
    EvalScenario,
    FakeChatModel,
    ScriptExhaustedError,
    action_signature,
    assert_scenario_result,
    collect_eval_metrics,
    load_scenario,
    run_scenario,
)
from src.agent_loop.comparison import compare, deterministic_rules, load_baseline
from src.agent_loop.events import EventEmitter, InMemoryEventSink
from src.contracts import ToolDef
from src.messages import Message, ToolCall
from src.config import PipelineSettings
from tests.evals.runner import load_scenarios, seed_memory

BASELINE_PATH = Path("tests/evals/baselines/agent_loop_v2.json")
PLAN = {"steps": [{"id": 1, "description": "Do the task", "status": "pending"}]}
SNAPSHOT = {"decision": "tool_call", "tool_request": {"name": "browser_snapshot", "args": {}}}


def _scenario(
    responses: list[dict[str, Any]],
    *,
    expected_status: str = "completed",
    strict: bool = False,
) -> EvalScenario:
    return EvalScenario(
        name="inline",
        task="Inspect the page.",
        model_responses=[json.dumps(response) for response in responses],
        browser_snapshots=['- textbox "Search" ref=e8'],
        assertions=EvalAssertions(expected_terminal_status=expected_status),
        strict=strict,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", load_scenarios(), ids=lambda scenario: scenario.name)
async def test_eval_scenarios_pass_configured_assertions(scenario) -> None:
    result = await run_scenario(scenario)

    assert_scenario_result(scenario, result)


@pytest.mark.asyncio
async def test_eval_scenarios_match_baseline() -> None:
    baseline = load_baseline(BASELINE_PATH)
    runs = await collect_eval_metrics(load_scenarios())
    results = {run.name: run.result.baseline_metrics() for run in runs}

    report = compare(
        results, baseline.scenarios, deterministic_rules(PipelineSettings()), suite="agent_loop"
    )

    assert not report.failed, "\n".join(finding.describe() for finding in report.findings)


@pytest.mark.asyncio
async def test_eval_scenarios_with_memory_match_the_memory_block() -> None:
    baseline = load_baseline(BASELINE_PATH)
    assert baseline.scenarios_with_memory is not None
    runs = await collect_eval_metrics(load_scenarios(), memory_factory=seed_memory)
    results = {run.name: run.result.baseline_metrics() for run in runs}

    report = compare(
        results,
        baseline.scenarios_with_memory,
        deterministic_rules(PipelineSettings()),
        suite="agent_loop+memory",
    )

    assert not report.failed, "\n".join(finding.describe() for finding in report.findings)


@pytest.mark.asyncio
async def test_a_cancelled_stop_ends_the_goal_as_cancelled() -> None:
    scenario = _scenario(
        [PLAN, {"decision": "stop", "status": "cancelled", "message": "The user withdrew it."}],
        expected_status="cancelled",
    )

    result = await run_scenario(scenario)

    assert result.summary.terminal_status == "cancelled"
    assert_scenario_result(scenario, result)


@pytest.mark.asyncio
async def test_a_strict_script_that_runs_out_fails_the_scenario() -> None:
    scenario = _scenario([PLAN, SNAPSHOT], strict=True)

    result = await run_scenario(scenario)
    (run,) = await collect_eval_metrics([scenario])

    assert result.script_exhausted is True
    assert result.summary.terminal_status == "failed"
    assert "the strict model script ran out of responses" in run.problems


@pytest.mark.asyncio
async def test_the_default_script_cycles_and_a_strict_one_raises() -> None:
    cycling = FakeChatModel(["a", "b"])
    strict = FakeChatModel(["a"], strict=True)

    replies = [(await cycling.complete([])).content for _ in range(3)]
    await strict.complete([])
    with pytest.raises(ScriptExhaustedError):
        await strict.complete([])

    assert replies == ["a", "b", "a"]
    assert strict.exhausted is True


@pytest.mark.asyncio
async def test_request_chars_count_messages_tool_calls_and_tool_schemas() -> None:
    model = FakeChatModel(["{}"])
    call = ToolCall(id="c1", name="echo", arguments={"text": "hi"})
    messages = [Message(role="user", content="hello"), Message(role="assistant", tool_calls=(call,))]
    tool = ToolDef(name="echo", description="Echo.", input_schema={"type": "object"})

    await model.complete(messages, tools=[tool])

    call_chars = len(json.dumps([call.to_dict()], ensure_ascii=False, sort_keys=True))
    schema = {"name": "echo", "description": "Echo.", "input_schema": {"type": "object"}}
    schema_chars = len(json.dumps(schema, ensure_ascii=False, sort_keys=True))
    assert model.prompt_chars == len("hello")
    assert model.request_chars == len("hello") + call_chars + schema_chars


@pytest.mark.asyncio
async def test_collect_eval_metrics_reports_a_failed_assertion_instead_of_raising() -> None:
    scenario = _scenario(
        [PLAN, {"decision": "done", "final_answer": "Done."}], expected_status="blocked"
    )

    (run,) = await collect_eval_metrics([scenario])

    assert run.name == "inline"
    assert run.problems == ("terminal_status: expected 'blocked', got 'completed'",)


def _signature(*requests: dict[str, Any]) -> str:
    sink = InMemoryEventSink()
    emitter = EventEmitter(sink, session_id="s")
    for request in requests:
        emitter.emit("action.proposed", source="test", payload={"tool_request": request})
    return action_signature(sink.records)


def test_the_action_signature_ignores_argument_values_but_not_keys() -> None:
    base = _signature({"name": "type", "args": {"ref": "e1", "text": "a"}})

    assert _signature({"name": "type", "args": {"text": "b", "ref": "e2"}}) == base
    assert _signature({"name": "type", "args": {"ref": "e1"}}) != base
    assert _signature({"name": "fill", "args": {"ref": "e1", "text": "a"}}) != base
    assert base.startswith("sha256:")


@pytest.mark.asyncio
async def test_baseline_metrics_extend_the_v1_metrics() -> None:
    (scenario,) = [item for item in load_scenarios() if item.name == "stale_ref_recovery"]

    result = await run_scenario(scenario)
    record = result.baseline_metrics()

    assert {key: record[key] for key in result.metrics() if key != "final_answer"} == {
        key: value for key, value in result.metrics().items() if key != "final_answer"
    }
    assert record["error_count"] == 1
    assert record["observation_count"] == result.summary.tool_call_count
    assert record["request_chars"] > result.prompt_chars


def test_a_scenario_file_can_ask_for_a_strict_script(tmp_path: Path) -> None:
    path = tmp_path / "strict.yaml"
    path.write_text("name: s\ntask: t\nstrict: true\n", encoding="utf-8")

    assert load_scenario(path).strict is True


def test_every_suite_scenario_is_strict() -> None:
    lenient = [scenario.name for scenario in load_scenarios() if not scenario.strict]

    assert lenient == [], "a suite scenario must not cycle its model script: add `strict: true`"
