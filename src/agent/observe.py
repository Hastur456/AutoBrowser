"""Deterministic observation compilation for executor results."""

from __future__ import annotations

from typing import Any

from src.agent.state import (
    AgentState,
    BrowserContext,
    ExecutionEvent,
    ObservationOutcome,
    RecoverySignal,
    StructuredObservation,
    ToolResult,
)

MAX_CONTENT_PREVIEW_CHARS = 1200
MAX_EVENT_SUMMARY_CHARS = 240
MAX_REASONING_CONTEXT_CHARS = 1000
MAX_EXECUTION_EVENTS = 20


def compact_text(value: Any, limit: int) -> str:
    """Return a single deterministic text preview within a character budget."""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""

    lines = [" ".join(line.split()) for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    if len(text) <= limit:
        return text

    omitted = len(text) - limit
    suffix = f"... [truncated {omitted} chars]"
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def classify_tool_result(result: ToolResult) -> ObservationOutcome:
    """Classify a normalized tool result without LLM interpretation."""

    tool_name = str(result.get("name", "")).strip()
    status = result.get("status", "error")
    content = str(result.get("content", "") or "").strip()
    error = str(result.get("error", "") or "").strip()

    if not tool_name or "No tool request was provided" in error:
        return "invalid_request"
    if error.startswith("Unknown tool:"):
        return "unknown_tool"
    if "requires a stronger policy" in error or "approval was denied" in error:
        return "blocked_error"
    if status == "success":
        return "success" if content else "no_output"
    return "transient_error"


def recovery_for_outcome(
    outcome: ObservationOutcome,
    summary: str,
    repeat_count: int,
) -> RecoverySignal:
    """Create a recovery signal that later routers can consume."""

    if outcome == "success":
        return {
            "category": outcome,
            "action": "none",
            "reason": "Tool execution succeeded.",
            "repeat_count": repeat_count,
        }
    if outcome == "no_output":
        return {
            "category": outcome,
            "action": "replan",
            "reason": "Tool succeeded but returned no useful output.",
            "repeat_count": repeat_count,
        }
    if outcome == "invalid_request":
        return {
            "category": outcome,
            "action": "replan",
            "reason": "The tool request was missing or malformed.",
            "repeat_count": repeat_count,
        }
    if outcome == "unknown_tool":
        return {
            "category": outcome,
            "action": "replan",
            "reason": "The selected tool is not available.",
            "repeat_count": repeat_count,
        }
    if outcome == "blocked_error":
        return {
            "category": outcome,
            "action": "ask_human",
            "reason": "The action is blocked by policy or human approval.",
            "repeat_count": repeat_count,
        }

    action = "replan" if repeat_count >= 2 else "retry"
    reason = "Repeated tool failure." if repeat_count >= 2 else "Tool failed once."
    if summary:
        reason = f"{reason} {summary}"
    return {
        "category": outcome,
        "action": action,
        "reason": compact_text(reason, MAX_EVENT_SUMMARY_CHARS),
        "repeat_count": repeat_count,
    }


def _repeat_count(
    events: list[ExecutionEvent],
    tool_name: str,
    outcome: ObservationOutcome,
) -> int:
    count = 1
    for event in reversed(events):
        if event.get("tool_name") != tool_name or event.get("outcome") != outcome:
            break
        count += 1
    return count


def _browser_context(
    state: AgentState,
    observation: StructuredObservation,
) -> BrowserContext:
    previous = dict(state.get("browser_context") or {})
    tool_name = observation.get("tool_name", "")
    status = observation.get("status", "error")
    context: BrowserContext = {
        "last_tool": tool_name,
        "last_status": status,
    }

    page_summary = previous.get("page_summary", "")
    if tool_name.startswith("browser_") and status == "success":
        preview = observation.get("content_preview", "")
        if tool_name == "browser_snapshot" and preview:
            page_summary = preview
        elif tool_name in {"browser_navigate", "browser_click", "browser_type"}:
            page_summary = preview or observation.get("summary", "")

    if page_summary:
        context["page_summary"] = compact_text(page_summary, MAX_CONTENT_PREVIEW_CHARS)
    return context


def _render_reasoning_context(
    observation: StructuredObservation,
    browser_context: BrowserContext,
    recovery_signal: RecoverySignal,
    events: list[ExecutionEvent],
) -> str:
    lines = [
        f"Latest tool: {observation.get('summary', 'No tool output.')}",
        f"Outcome: {observation.get('outcome', 'transient_error')}",
    ]

    page_summary = browser_context.get("page_summary", "")
    if page_summary:
        lines.extend(["Browser context:", page_summary])

    if recovery_signal.get("action") != "none":
        lines.append(
            "Recovery: "
            f"{recovery_signal.get('action')} - {recovery_signal.get('reason', '')}"
        )

    recent = [event.get("summary", "") for event in events[-3:] if event.get("summary")]
    if recent:
        lines.extend(["Recent execution events:", "\n".join(f"- {item}" for item in recent)])

    return compact_text("\n".join(lines), MAX_REASONING_CONTEXT_CHARS)


def compile_observation(state: AgentState) -> dict[str, Any]:
    """Compile executor output into bounded observation state."""

    result = state.get("tool_result") or {}
    tool_name = str(result.get("name", "tool") or "tool").strip()
    status = result.get("status", "error")
    content = str(result.get("content", "") or "")
    error = str(result.get("error", "") or "")
    payload = content if content else error
    content_preview = compact_text(payload or "No tool output.", MAX_CONTENT_PREVIEW_CHARS)
    outcome = classify_tool_result(result)
    summary_payload = compact_text(payload or "No tool output.", MAX_EVENT_SUMMARY_CHARS)
    summary = f"{tool_name} returned {status}: {summary_payload}"

    previous_events = list(state.get("execution_events", []))
    repeat_count = _repeat_count(previous_events, tool_name, outcome)
    observation: StructuredObservation = {
        "tool_name": tool_name,
        "status": status,
        "outcome": outcome,
        "summary": summary,
        "content_preview": content_preview,
        "error": error,
    }
    last_sequence = int(previous_events[-1].get("sequence", 0)) if previous_events else 0
    event: ExecutionEvent = {
        "sequence": last_sequence + 1,
        "tool_name": tool_name,
        "status": status,
        "outcome": outcome,
        "summary": summary,
    }
    events = (previous_events + [event])[-MAX_EXECUTION_EVENTS:]
    browser_context = _browser_context(state, observation)
    recovery_signal = recovery_for_outcome(outcome, summary_payload, repeat_count)
    reasoning_context = _render_reasoning_context(
        observation,
        browser_context,
        recovery_signal,
        events,
    )

    history = list(state.get("history", []))
    if not history or history[-1] != summary:
        history.append(summary)
    history = history[-MAX_EXECUTION_EVENTS:]

    return {
        "latest_observation": observation,
        "browser_context": browser_context,
        "reasoning_context": reasoning_context,
        "recovery_signal": recovery_signal,
        "execution_events": events,
        "observation": reasoning_context,
        "history": history,
        "decision": "tool_call",
        "policy_decision": "",
        "tool_request": {},
    }
