"""``src/harness/otel_spans.py``: one task's events -> a span tree, without the OTel SDK.

Synthetic ``EventRecord`` lists with explicit timestamps, as ``tests/test_benchmarks.py``
builds them.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from src.agent_loop.events import EventRecord
from src.harness.otel_spans import (
    SpanContext,
    SpanSpec,
    encode_otlp_json,
    task_span_specs,
)

T0 = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
T0_NS = int(T0.timestamp()) * 1_000_000_000
TASK = "найди цену книги Sapiens"
ANSWER = "Цена книги: £54.23"
ARGS = {"element": "Add to basket", "ref": "e42", "api_key": "sk-live-1"}
RESULT = "Clicked the button; page text: In stock (22 available)"
REASON = "Blocked by rule shop-checkout"


def ev(kind: str, ms: int, payload: dict[str, Any] | None = None, *, event_id: str | None = None) -> EventRecord:
    return EventRecord(
        type=kind,  # type: ignore[arg-type]
        source="test",
        payload=payload or {},
        event_id=event_id or f"{kind}-{ms}",
        timestamp=T0 + timedelta(milliseconds=ms),
        session_id="sess-1",
        goal_id="task-1",
        task_id="task-1",
    )


def ns(ms: int) -> int:
    return T0_NS + ms * 1_000_000


def trace(terminal: EventRecord | None = None) -> list[EventRecord]:
    usage = {
        "input_tokens": 1200,
        "output_tokens": 40,
        "cached_input_tokens": 800,
        "reasoning_tokens": 12,
        "provider_duration_ms": 45,
        "load_duration_ms": 5,
    }
    events = [
        ev("goal.started", 0, {"task": TASK}),
        ev("model.requested", 10, {"phase": "plan"}),
        ev("model.responded", 60, {"phase": "plan", "usage": usage}),
        ev("plan.updated", 61, {"steps": [{"id": 1, "description": TASK, "status": "pending"}], "current_step": 0, "replan_count": 0}),
        ev("model.requested", 70, {"phase": "agent", "tool_count": 3}),
        ev("model.responded", 120, {"phase": "agent", "usage": {"input_tokens": 2000, "output_tokens": 30}}),
        ev("permission.decided", 121, {"tool": "click", "server": "browser", "decision": "allow", "source": "mode", "rule_id": "", "reason": REASON}),
        ev("tool.started", 122, {"tool_request": {"name": "click", "args": ARGS, "id": "call-1"}}),
        ev("tool.finished", 222, {"tool_result": {"name": "click", "status": "success", "content": RESULT, "error": ""}}),
        ev("model.requested", 230, {"phase": "agent", "tool_count": 3}),
        ev("model.responded", 260, {"phase": "agent"}),
    ]
    events.append(
        terminal
        if terminal is not None
        else ev("goal.completed", 270, {"task": TASK, "status": "completed", "final_answer": ANSWER, "turns": 2})
    )
    return events


CONTEXT = SpanContext(session_id="sess-1", model="gemma4:31b-cloud", provider="ollama")


def specs(events: list[EventRecord] | None = None, *, capture: str = "none", context: SpanContext = CONTEXT) -> list[SpanSpec]:
    return task_span_specs(trace() if events is None else events, capture_content=capture, context=context)


def all_values(spans: list[SpanSpec]) -> list[str]:
    values: list[str] = []
    for span in spans:
        values += [str(value) for value in span.attributes.values()]
        for event in span.events:
            values += [str(value) for value in event.attributes.values()]
        values.append(span.error or "")
    return values


# -- tree and timings ------------------------------------------------------------------------


def test_a_task_is_a_root_span_with_chat_and_tool_children() -> None:
    spans = specs()

    root = spans[0]
    assert [span.name for span in spans] == [
        "invoke_agent autobrowser",
        "chat gemma4:31b-cloud",
        "chat gemma4:31b-cloud",
        "execute_tool click",
        "chat gemma4:31b-cloud",
    ]
    assert root.parent_key is None
    assert all(span.parent_key == root.key for span in spans[1:])
    assert (root.start_ns, root.end_ns) == (ns(0), ns(270))
    assert [(span.start_ns, span.end_ns) for span in spans[1:]] == [
        (ns(10), ns(60)),
        (ns(70), ns(120)),
        (ns(122), ns(222)),
        (ns(230), ns(260)),
    ]
    assert root.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert root.attributes["gen_ai.agent.name"] == "autobrowser"
    assert root.attributes["gen_ai.conversation.id"] == "sess-1"
    assert root.attributes["autobrowser.task.id"] == "task-1"
    assert root.attributes["autobrowser.goal.status"] == "completed"
    assert root.error is None and not any(span.incomplete for span in spans)


def test_span_keys_are_the_opening_event_ids_and_stable() -> None:
    spans = specs()

    assert [span.key for span in spans] == [
        "goal.started-0",
        "model.requested-10",
        "model.requested-70",
        "tool.started-122",
        "model.requested-230",
    ]
    assert specs() == spans


def test_events_without_a_goal_start_build_no_spans() -> None:
    assert task_span_specs(trace()[1:], capture_content="none") == []


def test_model_spans_pair_by_phase() -> None:
    events = [
        ev("goal.started", 0, {"task": TASK}),
        ev("model.requested", 10, {"phase": "agent"}),
        ev("model.requested", 20, {"phase": "approval", "tool": "click"}),
        ev("model.responded", 30, {"phase": "approval", "tool": "click", "needs_approval": True, "error": ""}),
        ev("model.responded", 50, {"phase": "agent"}),
        ev("goal.completed", 60, {"status": "completed"}),
    ]

    spans = specs(events, context=SpanContext(model="main-model", phase_models={"approval": "judge-model"}))

    agent, approval = spans[1], spans[2]
    assert (agent.name, agent.start_ns, agent.end_ns) == ("chat main-model", ns(10), ns(50))
    assert (approval.name, approval.start_ns, approval.end_ns) == ("chat judge-model", ns(20), ns(30))
    assert approval.attributes["gen_ai.request.model"] == "judge-model"
    assert approval.attributes["autobrowser.model.phase"] == "approval"
    assert approval.attributes["autobrowser.approval.needs_approval"] is True


def test_tool_spans_pair_in_fifo_order_and_take_the_server_of_their_permission() -> None:
    spans = specs()

    tool = spans[3]
    assert tool.attributes["gen_ai.operation.name"] == "execute_tool"
    assert tool.attributes["gen_ai.tool.name"] == "click"
    assert tool.attributes["gen_ai.tool.call.id"] == "call-1"
    assert tool.attributes["autobrowser.tool.server"] == "browser"


# -- usage, model, provider ------------------------------------------------------------------


def test_chat_spans_carry_model_provider_and_usage() -> None:
    plan, agent, last = specs()[1], specs()[2], specs()[4]

    assert plan.attributes["gen_ai.operation.name"] == "chat"
    assert plan.attributes["gen_ai.request.model"] == "gemma4:31b-cloud"
    assert plan.attributes["gen_ai.provider.name"] == "ollama"
    assert plan.attributes["autobrowser.model.phase"] == "plan"
    assert plan.attributes["gen_ai.usage.input_tokens"] == 1200
    assert plan.attributes["gen_ai.usage.output_tokens"] == 40
    assert plan.attributes["autobrowser.usage.cached_input_tokens"] == 800
    assert plan.attributes["autobrowser.usage.reasoning_tokens"] == 12
    assert plan.attributes["autobrowser.model.provider_ms"] == 45
    assert plan.attributes["autobrowser.model.load_ms"] == 5
    assert agent.attributes["gen_ai.usage.input_tokens"] == 2000
    assert "gen_ai.usage.input_tokens" not in last.attributes


def test_an_unknown_model_names_the_span_by_the_operation() -> None:
    spans = specs(context=SpanContext())

    assert spans[1].name == "chat"
    assert "gen_ai.request.model" not in spans[1].attributes
    assert "gen_ai.provider.name" not in spans[1].attributes


# -- statuses ----------------------------------------------------------------------------------


def test_a_tool_error_is_an_error_span() -> None:
    events = trace()
    events[8] = ev("tool.finished", 222, {"tool_result": {"name": "click", "status": "error", "content": "", "error": RESULT}})

    tool = specs(events)[3]

    assert tool.error == "tool_error"
    assert tool.attributes["error.type"] == "tool_error"
    assert specs(events, capture="redacted")[3].error == RESULT


def test_a_failed_goal_is_an_error_root_with_its_type() -> None:
    failed = ev("goal.failed", 270, {"task": TASK, "status": "failed", "error": {"type": "ModelProviderError", "message": "ollama 502: " + TASK}})

    root = specs(trace(failed))[0]

    assert root.attributes["autobrowser.goal.status"] == "failed"
    assert root.attributes["error.type"] == "ModelProviderError"
    assert root.error == "ModelProviderError"
    assert TASK in (specs(trace(failed), capture="redacted")[0].error or "")


def test_a_blocked_goal_is_an_error_root_with_its_stop_reason() -> None:
    blocked = ev("goal.blocked", 270, {"task": TASK, "status": "blocked", "final_answer": "no", "stop_reason": "approval_denied"})

    root = specs(trace(blocked))[0]

    assert root.attributes["autobrowser.goal.status"] == "blocked"
    assert root.attributes["autobrowser.stop_reason"] == "approval_denied"
    assert root.error == "approval_denied"


def test_unclosed_spans_end_at_the_terminal_event_as_incomplete() -> None:
    events = trace()[:8] + [ev("goal.cancelled", 300, {"status": "cancelled", "stop_reason": "cancelled"})]
    events.insert(7, ev("model.requested", 121, {"phase": "approval", "tool": "click"}))

    spans = specs(events)

    unclosed = [span for span in spans if span.incomplete]
    assert {span.key for span in unclosed} == {"model.requested-121", "tool.started-122"}
    assert all(span.end_ns == ns(300) for span in unclosed)
    assert all(span.attributes["autobrowser.incomplete"] is True for span in unclosed)
    assert spans[0].error == "cancelled" and not spans[0].incomplete


def test_a_task_without_a_terminal_event_ends_at_its_last_event() -> None:
    spans = specs(trace()[:9])

    root = spans[0]
    assert root.incomplete and root.attributes["autobrowser.incomplete"] is True
    assert root.end_ns == ns(222)


# -- span events on the task span ------------------------------------------------------------


def test_decisions_and_plan_updates_are_events_on_the_task_span() -> None:
    events = trace()
    gates = [
        ev("permission.decided", 223, {"tool": "click", "server": "browser", "decision": "deny", "source": "rule", "rule_id": "r1", "reason": REASON}),
        ev("permission.decided", 224, {"tool": "click", "server": "browser", "decision": "ask", "source": "rule", "rule_id": "r2", "reason": REASON}),
        ev("approval.requested", 225, {"tool_request": {"name": "click", "args": ARGS}, "reason": REASON, "rule_id": "r2"}),
        ev("approval.answered", 226, {"ticket_id": "t", "answer": "once", "via": "tui"}),
        ev("approval.resolved", 227, {"decision": "allow", "by": "human", "scope": "once"}),
        ev("hook.decided", 228, {"hook_id": "h1", "event": "pre_tool_use", "tool": "click", "decision": "deny", "reason": REASON}),
        ev("hook.decided", 229, {"hook_id": "h2", "event": "post_tool_use", "tool": "click", "decision": None, "reason": ""}),
        ev("policy.decided", 229, {"decision": "blocked", "reason": REASON, "tool_request": {"name": "click", "args": ARGS}}),
    ]
    events[9:9] = gates

    root = specs(events)[0]

    names = [event.name for event in root.events]
    assert names == [
        "plan.updated",
        "permission.decided",
        "permission.decided",
        "approval.requested",
        "approval.answered",
        "approval.resolved",
        "hook.decided",
        "policy.decided",
    ]
    deny = root.events[1]
    assert deny.time_ns == ns(223)
    assert deny.attributes == {"decision": "deny", "source": "rule", "rule_id": "r1", "tool": "click"}
    assert root.events[0].attributes == {"steps": 1, "current_step": 0, "replan_count": 0}
    redacted = specs(events, capture="redacted")[0]
    assert redacted.events[1].attributes["reason"] == REASON


# -- attributes on every span ------------------------------------------------------------------


def test_session_labels_and_environment_are_on_every_span() -> None:
    context = SpanContext(
        session_id="sess-1",
        labels={"autobrowser.batch.id": "b-1", "autobrowser.scenario.id": "books-1"},
        environment="interactive",
    )

    for span in specs(context=context):
        assert span.attributes["session.id"] == "sess-1"
        assert span.attributes["autobrowser.batch.id"] == "b-1"
        assert span.attributes["autobrowser.scenario.id"] == "books-1"
        assert span.attributes["deployment.environment.name"] == "interactive"


def test_an_environment_label_wins_over_the_setting() -> None:
    context = SpanContext(labels={"deployment.environment.name": "batch"}, environment="interactive")

    assert {span.attributes["deployment.environment.name"] for span in specs(context=context)} == {"batch"}


def test_a_label_does_not_override_a_span_attribute() -> None:
    context = SpanContext(labels={"gen_ai.operation.name": "spoofed"})

    assert specs(context=context)[0].attributes["gen_ai.operation.name"] == "invoke_agent"


def test_no_environment_means_no_attribute() -> None:
    assert all("deployment.environment.name" not in span.attributes for span in specs())


def test_attribute_aliases_copy_a_key_on_every_span_that_has_it() -> None:
    context = SpanContext(
        labels={"autobrowser.batch.id": "b-1"},
        attribute_aliases={"autobrowser.batch.id": "backend.metadata.batch_id", "gen_ai.tool.name": "backend.tool", "absent.key": "never"},
    )

    spans = specs(context=context)

    assert all(span.attributes["backend.metadata.batch_id"] == "b-1" for span in spans)
    assert spans[3].attributes["backend.tool"] == "click"
    assert "backend.tool" not in spans[0].attributes
    assert all("never" not in span.attributes for span in spans)


# -- content capture ---------------------------------------------------------------------------


def test_capture_none_exports_no_text() -> None:
    events = trace()
    events.insert(9, ev("permission.decided", 223, {"tool": "click", "server": "b", "decision": "deny", "source": "rule", "rule_id": "r", "reason": REASON}))

    values = all_values(specs(events, capture="none"))

    for text in (TASK, ANSWER, RESULT, REASON, "Add to basket", "sk-live-1"):
        assert not any(text in value for value in values), text
    root = specs(events)[0]
    for key in ("input.value", "output.value"):
        assert key not in root.attributes
    assert "gen_ai.tool.call.arguments" not in specs(events)[3].attributes


def test_capture_redacted_exports_the_redacted_task_answer_and_tool_io() -> None:
    spans = specs(capture="redacted")

    root, tool = spans[0], spans[3]
    assert root.attributes["input.value"] == TASK
    assert root.attributes["output.value"] == ANSWER
    arguments = json.loads(str(tool.attributes["gen_ai.tool.call.arguments"]))
    assert arguments == {"element": "Add to basket", "ref": "e42", "api_key": "[REDACTED]"}
    assert tool.attributes["gen_ai.tool.call.result"] == RESULT
    assert "sk-live-1" not in " ".join(all_values(spans))


def test_capture_answers_exports_the_task_and_answer_only() -> None:
    events = trace()
    events.insert(9, ev("permission.decided", 223, {"tool": "click", "server": "b", "decision": "deny", "source": "rule", "rule_id": "r", "reason": REASON}))
    events[8] = ev("tool.finished", 222, {"tool_result": {"name": "click", "status": "error", "content": RESULT, "error": RESULT}})

    spans = specs(events, capture="answers")

    root = spans[0]
    assert root.attributes["input.value"] == TASK and root.attributes["output.value"] == ANSWER
    tool = next(span for span in spans if span.name.startswith("execute_tool"))
    assert "gen_ai.tool.call.arguments" not in tool.attributes and "gen_ai.tool.call.result" not in tool.attributes
    assert tool.error == "tool_error"
    assert all("reason" not in event.attributes for event in root.events)
    values = [value for value in all_values(spans) if value not in {TASK, ANSWER}]
    for text in (RESULT, REASON, "Add to basket"):
        assert not any(text in value for value in values), text


# -- OTLP/JSON -------------------------------------------------------------------------------------


def test_encode_otlp_json_builds_resource_spans() -> None:
    events = trace()
    events[8] = ev("tool.finished", 222, {"tool_result": {"name": "click", "status": "error", "error": "x"}})
    spans = specs(events)
    ids = {span.key: f"{index + 1:016x}" for index, span in enumerate(spans)}

    body = encode_otlp_json(spans, resource={"service.name": "autobrowser"}, trace_id="ab" * 16, span_id_of=ids.__getitem__)

    resource_spans = body["resourceSpans"][0]
    assert resource_spans["resource"]["attributes"] == [{"key": "service.name", "value": {"stringValue": "autobrowser"}}]
    encoded = resource_spans["scopeSpans"][0]["spans"]
    assert [span["spanId"] for span in encoded] == [ids[span.key] for span in spans]
    assert all(span["traceId"] == "ab" * 16 for span in encoded)
    assert "parentSpanId" not in encoded[0]
    assert all(span["parentSpanId"] == ids[spans[0].key] for span in encoded[1:])
    assert encoded[0]["startTimeUnixNano"] == str(ns(0))
    assert encoded[0]["endTimeUnixNano"] == str(ns(270))
    plan_attributes = {item["key"]: item["value"] for item in encoded[1]["attributes"]}
    assert plan_attributes["gen_ai.usage.input_tokens"] == {"intValue": "1200"}
    assert plan_attributes["gen_ai.operation.name"] == {"stringValue": "chat"}
    assert encoded[3]["status"] == {"code": 2, "message": "tool_error"}
    assert encoded[0]["status"] == {"code": 0}
    assert encoded[0]["events"][0]["name"] == "plan.updated"
    assert encoded[0]["events"][0]["timeUnixNano"] == str(ns(61))
    json.dumps(body)


def test_a_tool_span_takes_the_server_of_its_tool_started_event() -> None:
    events = [
        ev("goal.started", 0, {"task": TASK}),
        ev("tool.started", 10, {"tool_request": {"name": "lookup"}, "server": "docs", "read_only": True}),
        ev("tool.finished", 20, {"tool_result": {"name": "lookup", "status": "success", "content": "x"}}),
        ev("goal.completed", 30, {"task": TASK, "status": "completed", "final_answer": ANSWER, "turns": 1}),
    ]

    (tool,) = [span for span in specs(events) if span.attributes.get("gen_ai.operation.name") == "execute_tool"]
    assert tool.attributes["autobrowser.tool.server"] == "docs"
