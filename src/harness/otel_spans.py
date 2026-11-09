"""One task's events as a tree of span specifications, independent of the OpenTelemetry SDK.

``task_span_specs`` turns the events of one task (``goal.started`` up to its terminal event)
into :class:`SpanSpec` values: a root ``invoke_agent autobrowser`` span, ``chat {model}``
children paired by model ``phase`` and ``execute_tool {tool}`` children paired in FIFO order.
Span keys are the ``event_id`` of the opening events, so the same events always give the same
tree. The live sink (``src/harness/otel_sink.py``) materializes the specs through the SDK;
``encode_otlp_json`` writes them as an OTLP/JSON request body instead.

Attribute names follow the OpenTelemetry GenAI conventions where one exists; everything else
is ``autobrowser.*``. Tool and model names are data (span names and attributes), never
branches. With ``capture_content="none"`` only ids, names, counts, statuses and durations are
emitted; ``"answers"`` adds the task text and the final answer on the task span; ``"redacted"``
also tool arguments/results, decision reasons and error messages. Values are read from
``EventRecord.to_json_dict()`` (redacted and truncated), never from the raw payload. See docs/decisions/2026-10-29-otlp-trace-export.md.

``task_trajectory`` flattens the same events, under the same content modes, into a JSON list
of steps (task, plan, tool calls with their gate decisions, model errors, outcome) for a
backend whose evaluators read a single observation and never its children;
``trajectory_summary`` wraps it with the stop reason and final status. With ``trajectory=True``
the task span carries that summary as one JSON object under ``autobrowser.trajectory``. See
docs/decisions/2026-11-08-trajectory-on-the-root-observation.md.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from src.agent_loop.events import EventRecord

AttributeValue = str | int | float | bool | tuple[str, ...]

AGENT_NAME = "autobrowser"
ROOT_SPAN_NAME = f"invoke_agent {AGENT_NAME}"
TERMINAL_EVENTS = {"goal.completed", "goal.failed", "goal.blocked", "goal.cancelled"}
ENVIRONMENT_KEY = "deployment.environment.name"
INCOMPLETE_KEY = "autobrowser.incomplete"
TRAJECTORY_KEY = "autobrowser.trajectory"
TOOL_ERROR_TYPE = "tool_error"
_TOOL_ERROR_STATUSES = {"error", "failed", "failure"}
#: ``model.responded`` usage keys -> span attributes.
_USAGE_ATTRIBUTES = {
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "cached_input_tokens": "autobrowser.usage.cached_input_tokens",
    "reasoning_tokens": "autobrowser.usage.reasoning_tokens",
    "provider_duration_ms": "autobrowser.model.provider_ms",
    "load_duration_ms": "autobrowser.model.load_ms",
}
#: Payload keys copied as span-event attributes, per event type (``reason`` only redacted).
_EVENT_FIELDS = {
    "permission.decided": ("decision", "source", "rule_id", "tool"),
    "approval.requested": ("rule_id",),
    "approval.answered": ("answer", "via"),
    "approval.resolved": ("decision", "by", "scope"),
    "hook.decided": ("hook_id", "event", "decision", "tool"),
    "policy.decided": ("decision", "source", "rule_id"),
}
#: Gate decisions that stop a tool call (permission and hook ``deny``, a progress ``blocked``).
_DENY_DECISIONS = frozenset({"deny", "blocked"})
_OTLP_STATUS_UNSET, _OTLP_STATUS_ERROR = 0, 2


@dataclass(frozen=True)
class SpanEventSpec:
    """A point-in-time event on a span."""

    name: str
    time_ns: int
    attributes: Mapping[str, AttributeValue] = field(default_factory=dict)


@dataclass(frozen=True)
class SpanSpec:
    """One span, ready to be materialized by an SDK or encoded as OTLP/JSON."""

    key: str
    parent_key: str | None
    name: str
    start_ns: int
    end_ns: int
    attributes: Mapping[str, AttributeValue]
    events: tuple[SpanEventSpec, ...] = ()
    #: Status ERROR with this description; ``None`` leaves the status unset.
    error: str | None = None
    #: Never closed by its own event: ended at the terminal (or last) event.
    incomplete: bool = False


@dataclass(frozen=True)
class SpanContext:
    """What a task's events do not carry: the session's identity, labels and model."""

    session_id: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    environment: str | None = None
    model: str | None = None
    provider: str | None = None
    #: The model of a ``phase`` that does not use the session model (the approval judge).
    phase_models: Mapping[str, str] = field(default_factory=dict)
    attribute_aliases: Mapping[str, str] = field(default_factory=dict)


@dataclass
class _Open:
    key: str
    name: str
    start_ns: int
    attributes: dict[str, AttributeValue]


def task_span_specs(
    events: Sequence[EventRecord],
    *,
    capture_content: str,
    context: SpanContext | None = None,
    trajectory: bool = False,
) -> list[SpanSpec]:
    """The span tree of one task's events, the root first; ``[]`` without ``goal.started``.

    ``trajectory`` adds the task's :func:`trajectory_summary` to the root span."""

    context = context or SpanContext()
    redacted = capture_content == "redacted"
    answers = redacted or capture_content == "answers"
    start = next((event for event in events if event.type == "goal.started"), None)
    if start is None:
        return []
    body = list(events[events.index(start) + 1 :])
    terminal = next((event for event in body if event.type in TERMINAL_EVENTS), None)
    if terminal is not None:
        body = body[: body.index(terminal)]
    end_ns = _ns(terminal.timestamp if terminal else (body[-1] if body else start).timestamp)

    root_key = start.event_id
    children: list[SpanSpec] = []
    span_events: list[SpanEventSpec] = []
    open_models: dict[str, list[_Open]] = {}
    open_tools: list[_Open] = []
    server = ""
    for event in body:
        payload = _payload(event)
        if event.type == "model.requested":
            phase = str(payload.get("phase") or "")
            # A cascade tier names its model in the event; otherwise the session's (or the
            # phase's own, e.g. the approval classifier).
            model = str(payload.get("model") or "") or context.phase_models.get(phase) or context.model
            attributes: dict[str, AttributeValue] = {
                "gen_ai.operation.name": "chat",
                "autobrowser.model.phase": phase,
            }
            if model:
                attributes["gen_ai.request.model"] = model
            if context.provider:
                attributes["gen_ai.provider.name"] = context.provider
            name = f"chat {model}" if model else "chat"
            open_models.setdefault(phase, []).append(_Open(event.event_id, name, _ns(event.timestamp), attributes))
        elif event.type == "model.responded":
            pending = open_models.get(str(payload.get("phase") or ""))
            if pending:
                span = pending.pop(0)
                error = _model_response(payload, span.attributes, redacted)
                children.append(_closed(span, root_key, _ns(event.timestamp), error))
        elif event.type == "tool.started":
            request = _mapping(payload.get("tool_request"))
            tool = str(request.get("name") or "")
            attributes = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": tool}
            if request.get("id"):
                attributes["gen_ai.tool.call.id"] = str(request["id"])
            server = str(payload.get("server") or "") or server
            if server:
                attributes["autobrowser.tool.server"] = server
            if redacted:
                attributes["gen_ai.tool.call.arguments"] = _text(request.get("args") or {})
            open_tools.append(_Open(event.event_id, f"execute_tool {tool}".rstrip(), _ns(event.timestamp), attributes))
            server = ""
        elif event.type == "tool.finished":
            if open_tools:
                span = open_tools.pop(0)
                error = _tool_result(_mapping(payload.get("tool_result")), span.attributes, redacted)
                children.append(_closed(span, root_key, _ns(event.timestamp), error))
        else:
            if event.type == "permission.decided":
                server = str(payload.get("server") or "")
            span_event = _span_event(event, payload, redacted)
            if span_event is not None:
                span_events.append(span_event)

    for span in [*(span for pending in open_models.values() for span in pending), *open_tools]:
        span.attributes[INCOMPLETE_KEY] = True
        children.append(_closed(span, root_key, end_ns, None, incomplete=True))
    children.sort(key=lambda spec: (spec.start_ns, spec.key))

    root = _root(start, terminal, root_key, end_ns, tuple(span_events), answers=answers, redacted=redacted)
    if trajectory:
        summary = trajectory_summary(task_trajectory(events, capture_content=capture_content))
        root = replace(root, attributes={**root.attributes, TRAJECTORY_KEY: json.dumps(summary, ensure_ascii=False)})
    return [_with_common(spec, context) for spec in [root, *children]]


def task_trajectory(events: Sequence[EventRecord], *, capture_content: str) -> list[dict[str, Any]]:
    """One task's events as JSON steps, in order; ``[]`` without ``goal.started``.

    Steps: ``task``; ``plan`` (the first plan and each replan); ``tool_call`` (from the proposal
    to the result, with the notable gate decisions made meanwhile in ``gates``; ``status``
    ``blocked`` when one of them denied it, ``not_executed`` when it never started otherwise);
    ``gate`` (a notable decision outside a call: the goal-start and stop hooks, post-tool hooks);
    ``model_error``; ``outcome`` (``incomplete`` without a terminal event). A decision names its
    event under ``gate``. The content modes are those of
    :func:`task_span_specs`: ``none`` keeps names, ids, statuses and counts; ``answers`` adds the
    task text and the final answer; ``redacted`` adds tool arguments/results, reasons, plan
    steps and error messages.
    """

    redacted = capture_content == "redacted"
    answers = redacted or capture_content == "answers"
    start = next((event for event in events if event.type == "goal.started"), None)
    if start is None:
        return []
    task: dict[str, Any] = {"type": "task", "task_id": start.task_id or ""}
    if answers:
        task["task"] = _text(_payload(start).get("task") or "")
    steps: list[dict[str, Any]] = [task]
    action: dict[str, Any] | None = None  # the tool call between its proposal and its result
    replans: int | None = None

    def open_action(request: Mapping[str, Any], source: str) -> dict[str, Any]:
        step: dict[str, Any] = {"type": "tool_call", "tool": str(request.get("name") or ""), "status": "proposed"}
        if request.get("id"):
            step["call_id"] = str(request["id"])
        if source:
            step["source"] = source
        if redacted:
            step["arguments"] = request.get("args") or {}
            if request.get("reason"):
                step["reason"] = _text(request["reason"])
        steps.append(step)
        return step

    def close_action() -> None:
        nonlocal action
        if action is not None and action["status"] == "proposed":
            denied = any(gate.get("decision") in _DENY_DECISIONS for gate in action.get("gates", ()))
            action["status"] = "blocked" if denied else "not_executed"
        action = None

    for event in events[events.index(start) + 1 :]:
        payload = _payload(event)
        if event.type in TERMINAL_EVENTS:
            close_action()
            steps.append(_outcome(event, payload, answers=answers, redacted=redacted))
            return steps
        if event.type == "action.proposed":
            close_action()
            action = open_action(_mapping(payload.get("tool_request")), str(payload.get("source") or ""))
        elif event.type == "tool.started":
            request = _mapping(payload.get("tool_request"))
            if action is None:
                action = open_action(request, "")
            action["status"] = "started"
            if payload.get("server"):
                action["server"] = str(payload["server"])
            if redacted:  # the arguments that ran: a hook may have rewritten the proposal's
                action["arguments"] = request.get("args") or {}
        elif event.type == "tool.finished":
            if action is not None:
                _finish_action(action, _mapping(payload.get("tool_result")), redacted)
            action = None
        elif event.type == "plan.updated":
            count = payload.get("replan_count")
            count = count if isinstance(count, int) else 0
            if count != replans:
                replans = count
                steps.append(_plan_step(payload, count, redacted))
        elif event.type == "model.responded" and payload.get("error"):
            error: dict[str, Any] = {"type": "model_error", "phase": str(payload.get("phase") or "")}
            if redacted:
                error["error"] = _text(payload["error"])
            steps.append(error)
        else:
            mark = _span_event(event, payload, redacted)
            if mark is not None:
                gate = {"gate": mark.name, **mark.attributes}
                if action is not None:
                    action.setdefault("gates", []).append(gate)
                else:
                    steps.append({"type": "gate", **gate})
    close_action()
    steps.append({"type": "outcome", "status": "incomplete"})
    return steps


def trajectory_summary(steps: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """``full_trajectory`` (the steps numbered from 1), ``stop_reason`` and ``final_status``
    of the last outcome (``None`` without one)."""

    outcome = next((step for step in reversed(steps) if step.get("type") == "outcome"), {})
    return {
        "full_trajectory": [{"step": number, **step} for number, step in enumerate(steps, 1)],
        "stop_reason": outcome.get("stop_reason"),
        "final_status": outcome.get("status"),
    }


def _finish_action(action: dict[str, Any], result: Mapping[str, Any], redacted: bool) -> None:
    error = str(result.get("error") or "")
    status = str(result.get("status") or "").lower()
    failed = bool(error) or status in _TOOL_ERROR_STATUSES
    action["status"] = status or ("error" if failed else "success")
    if redacted and "content" in result:
        action["result"] = _text(result.get("content"))
    if failed:
        action["error"] = (error or status) if redacted else TOOL_ERROR_TYPE


def _plan_step(payload: Mapping[str, Any], replans: int, redacted: bool) -> dict[str, Any]:
    raw = payload.get("steps")
    plan = [_mapping(item) for item in raw] if isinstance(raw, list) else []
    step: dict[str, Any] = {"type": "plan", "replan_count": replans, "step_count": len(plan)}
    if redacted:
        step["steps"] = [
            {"description": _text(item.get("description") or ""), "status": str(item.get("status") or "")}
            for item in plan
        ]
    return step


def _outcome(event: EventRecord, payload: Mapping[str, Any], *, answers: bool, redacted: bool) -> dict[str, Any]:
    step: dict[str, Any] = {"type": "outcome", "status": event.type.removeprefix("goal.")}
    if payload.get("stop_reason"):
        step["stop_reason"] = str(payload["stop_reason"])
    if isinstance(payload.get("turns"), int):
        step["turns"] = payload["turns"]
    if answers and payload.get("final_answer"):
        step["final_answer"] = _text(payload["final_answer"])
    failure = _mapping(payload.get("error"))
    if failure:
        error = {"type": str(failure.get("type") or "error")}
        if redacted and failure.get("message"):
            error["message"] = str(failure["message"])
        step["error"] = error
    return step


def encode_otlp_json(
    specs: Sequence[SpanSpec],
    *,
    resource: Mapping[str, AttributeValue],
    trace_id: str,
    span_id_of: Callable[[str], str],
) -> dict[str, Any]:
    """The specs as an OTLP/JSON ``ExportTraceServiceRequest`` body (hex ids, one trace)."""

    spans = []
    for spec in specs:
        span: dict[str, Any] = {
            "traceId": trace_id,
            "spanId": span_id_of(spec.key),
            "name": spec.name,
            "kind": 1,  # SPAN_KIND_INTERNAL
            "startTimeUnixNano": str(spec.start_ns),
            "endTimeUnixNano": str(spec.end_ns),
            "attributes": _otlp_attributes(spec.attributes),
            "events": [
                {
                    "name": event.name,
                    "timeUnixNano": str(event.time_ns),
                    "attributes": _otlp_attributes(event.attributes),
                }
                for event in spec.events
            ],
            "status": (
                {"code": _OTLP_STATUS_ERROR, "message": spec.error}
                if spec.error is not None
                else {"code": _OTLP_STATUS_UNSET}
            ),
        }
        if spec.parent_key is not None:
            span["parentSpanId"] = span_id_of(spec.parent_key)
        spans.append(span)
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _otlp_attributes(resource)},
                "scopeSpans": [{"scope": {"name": AGENT_NAME}, "spans": spans}],
            }
        ]
    }


def _root(
    start: EventRecord,
    terminal: EventRecord | None,
    key: str,
    end_ns: int,
    events: tuple[SpanEventSpec, ...],
    *,
    answers: bool,
    redacted: bool,
) -> SpanSpec:
    attributes: dict[str, AttributeValue] = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": AGENT_NAME,
    }
    if start.session_id:
        attributes["gen_ai.conversation.id"] = start.session_id
    if start.task_id:
        attributes["autobrowser.task.id"] = start.task_id
    if answers:
        attributes["input.value"] = _text(_payload(start).get("task") or "")
    if terminal is None:
        attributes[INCOMPLETE_KEY] = True
        return SpanSpec(key, None, ROOT_SPAN_NAME, _ns(start.timestamp), end_ns, attributes, events, "incomplete", True)

    payload = _payload(terminal)
    status = terminal.type.removeprefix("goal.")
    attributes["autobrowser.goal.status"] = status
    stop_reason = str(payload.get("stop_reason") or "")
    if stop_reason:
        attributes["autobrowser.stop_reason"] = stop_reason
    if isinstance(payload.get("turns"), int):
        attributes["autobrowser.turns"] = payload["turns"]
    if answers and payload.get("final_answer"):
        attributes["output.value"] = _text(payload["final_answer"])
    error: str | None = None
    if status == "failed":
        failure = _mapping(payload.get("error"))
        error_type = str(failure.get("type") or "error")
        attributes["error.type"] = error_type
        message = str(failure.get("message") or "")
        error = f"{error_type}: {message}" if redacted and message else error_type
    elif status != "completed":
        error = stop_reason or status
    return SpanSpec(key, None, ROOT_SPAN_NAME, _ns(start.timestamp), end_ns, attributes, events, error)


def _model_response(payload: Mapping[str, Any], attributes: dict[str, AttributeValue], redacted: bool) -> str | None:
    usage = _mapping(payload.get("usage"))
    for source, target in _USAGE_ATTRIBUTES.items():
        value = usage.get(source)
        if isinstance(value, int) and not isinstance(value, bool):
            attributes[target] = value
    if isinstance(payload.get("needs_approval"), bool):
        attributes["autobrowser.approval.needs_approval"] = payload["needs_approval"]
    error = str(payload.get("error") or "")
    if not error:
        return None
    attributes["error.type"] = "model_error"
    return error if redacted else "model_error"


def _tool_result(result: Mapping[str, Any], attributes: dict[str, AttributeValue], redacted: bool) -> str | None:
    if redacted and "content" in result:
        attributes["gen_ai.tool.call.result"] = _text(result.get("content"))
    error = str(result.get("error") or "")
    status = str(result.get("status") or "").lower()
    if not error and status not in _TOOL_ERROR_STATUSES:
        return None
    attributes["error.type"] = TOOL_ERROR_TYPE
    return (error or status) if redacted else TOOL_ERROR_TYPE


def _span_event(event: EventRecord, payload: Mapping[str, Any], redacted: bool) -> SpanEventSpec | None:
    if event.type == "plan.updated":
        steps = payload.get("steps")
        attributes: dict[str, AttributeValue] = {"steps": len(steps) if isinstance(steps, list) else 0}
        for name in ("current_step", "replan_count"):
            if isinstance(payload.get(name), int):
                attributes[name] = payload[name]
        return SpanEventSpec(event.type, _ns(event.timestamp), attributes)
    fields = _EVENT_FIELDS.get(event.type)
    if fields is None or not _is_notable(event.type, payload):
        return None
    attributes = {name: _scalar(payload[name]) for name in fields if payload.get(name) not in (None, "")}
    if event.type in {"approval.requested", "policy.decided"}:
        tool = _mapping(payload.get("tool_request")).get("name")
        if tool:
            attributes["tool"] = str(tool)
    if redacted and payload.get("reason"):
        attributes["reason"] = _text(payload["reason"])
    return SpanEventSpec(event.type, _ns(event.timestamp), attributes)


def _is_notable(event_type: str, payload: Mapping[str, Any]) -> bool:
    """Gate decisions worth a mark on the task span: everything but the routine allows."""

    decision = payload.get("decision")
    if event_type == "permission.decided":
        return decision != "allow"
    if event_type == "hook.decided":
        return decision in {"deny", "ask"}
    if event_type == "policy.decided":
        return decision == "blocked"
    return True


def _with_common(spec: SpanSpec, context: SpanContext) -> SpanSpec:
    """Session attributes on every span (a label beats the environment setting, the span's own
    attributes beat both), then the configured aliases."""

    attributes: dict[str, AttributeValue] = {}
    if context.session_id:
        attributes["session.id"] = context.session_id
    if context.environment:
        attributes[ENVIRONMENT_KEY] = context.environment
    attributes.update(context.labels)
    attributes.update(spec.attributes)
    for source, target in context.attribute_aliases.items():
        if source in attributes:
            attributes[target] = attributes[source]
    return SpanSpec(
        spec.key,
        spec.parent_key,
        spec.name,
        spec.start_ns,
        spec.end_ns,
        attributes,
        spec.events,
        spec.error,
        spec.incomplete,
    )


def _closed(span: _Open, parent: str, end_ns: int, error: str | None, *, incomplete: bool = False) -> SpanSpec:
    return SpanSpec(span.key, parent, span.name, span.start_ns, max(end_ns, span.start_ns), span.attributes, (), error, incomplete)


def _payload(event: EventRecord) -> Mapping[str, Any]:
    """The redacted, truncated payload: the only source of exported values."""

    return _mapping(event.to_json_dict().get("payload"))


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _scalar(value: Any) -> AttributeValue:
    return value if isinstance(value, (str, int, float, bool)) else _text(value)


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _ns(moment: datetime) -> int:
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000_000 + delta.microseconds * 1_000


def _otlp_attributes(attributes: Mapping[str, AttributeValue]) -> list[dict[str, Any]]:
    return [{"key": key, "value": _otlp_value(value)} for key, value in attributes.items()]


def _otlp_value(value: AttributeValue) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, tuple):
        return {"arrayValue": {"values": [{"stringValue": item} for item in value]}}
    return {"stringValue": value}
