"""Engine-native guards, terminal checks, and repeat tracking.

Ported from the legacy agent-loop guard helpers. State access is typed
:class:`~src.agent_loop.execution.state.LoopState` attribute access. Each function returns
a flat update dict that the loop applies through :meth:`LoopState.apply` (which routes
browser-scoped keys into ``BrowserState``).

Server-neutral: no element-ref handling, no canonical tool-name mapping and no
ineffective-action tracking. Repeat tracking compares tool names and arguments as given.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.config import get_settings
from src.contracts import ToolRequest
from src.harness.memory import (
    append_ai_tool_call,
    append_final_ai_response,
    append_tool_message,
    ensure_message_history,
    with_tool_call_id,
)

from src.agent_loop.execution.policy import (
    SNAPSHOT_REUSE_MARKER,
    SNAPSHOT_REUSE_MARKERS,
    SNAPSHOT_TOOL,
    TABS_TOOL,
    _snapshot_reuse_was_blocked,
)
from src.agent_loop.execution.state import LoopState

if TYPE_CHECKING:
    from src.contracts import CompletionStatus

REPEATED_SNAPSHOT_OBSERVATION_FINAL_ANSWER = (
    "Stopped because browser_snapshot returned the same visible state "
    "three consecutive times. Latest observation:\n\n{observation}"
)

REPEATED_SNAPSHOT_FINAL_ANSWER = (
    "Stopped because browser.snapshot was requested three consecutive times "
    "without a meaningful state change. Latest observation:\n\n{observation}"
)

FRESH_SNAPSHOT_REASON = (
    "The browser state is unknown. Capture a fresh snapshot before the next browser action."
)


def _message_state(state: LoopState) -> dict[str, Any]:
    """Return the minimal dict ``ensure_message_history`` reads from the typed state."""

    return {
        "messages": list(state.messages),
        "task": state.task,
        "task_id": state.task_id,
    }


def _blocked_response(state: LoopState, reason: str) -> dict[str, Any]:
    messages = ensure_message_history(_message_state(state))
    return {
        "decision": "done",
        "final_answer": reason,
        "observation": reason,
        "error": reason,
        "messages": append_final_ai_response(messages, reason),
    }


def _done_response(
    state: LoopState,
    final_answer: str,
    *,
    messages: list[Any] | None = None,
    **updates: Any,
) -> dict[str, Any]:
    history = messages if messages is not None else ensure_message_history(_message_state(state))
    response = {
        "decision": "done",
        "final_answer": final_answer,
        "messages": append_final_ai_response(history, final_answer),
        **_complete_plan_update(state),
    }
    response.update(updates)
    return response


def _complete_plan_update(state: LoopState) -> dict[str, Any]:
    plan = state.plan or []
    if not plan:
        return {}

    return {
        "plan": [{**step, "status": "completed"} for step in plan],
        "current_step": len(plan),
    }


def _replan_response(observation: str, **updates: Any) -> dict[str, Any]:
    response = {
        "decision": "replan",
        "observation": observation,
        "needs_fresh_snapshot": False,
    }
    response.update(updates)
    return response


def _terminal_guard(state: LoopState) -> dict[str, Any] | None:
    limits = get_settings().loop

    if state.decision == "done" and state.final_answer:
        return _done_response(state, str(state.final_answer or ""))

    replan_count = int(state.replan_count or 0)
    if replan_count >= limits.max_replans:
        observation = str(state.observation or "No observation available.")
        return _blocked_response(
            state,
            (
                f"Blocked: replanning reached the limit of {limits.max_replans}. "
                f"Last observation: {observation}"
            ),
        )

    failures = int(state.consecutive_failures or 0)
    if failures >= limits.max_consecutive_failures:
        error = str(state.error or "Unknown tool failure.")
        return _blocked_response(
            state,
            (
                "Blocked: tool execution failed "
                f"{limits.max_consecutive_failures} consecutive times. "
                f"Last error: {error}"
            ),
        )

    steps_without_plan_advance = int(state.steps_without_plan_advance or 0)
    if steps_without_plan_advance >= limits.max_steps_without_plan_advance:
        observation = str(state.observation or "No observation available.")
        return _blocked_response(
            state,
            (
                "Blocked: the current plan step did not advance after "
                f"{limits.max_steps_without_plan_advance} successful tool steps. "
                f"Last observation: {observation}"
            ),
        )

    return None


class CompletionController:
    """Single owner of every terminal decision on the native loop.

    Consolidates the pre-turn :func:`_terminal_guard` (replan / consecutive-failure /
    stalled-plan limits) and the unchanged-snapshot terminal.
    :class:`~src.agent_loop.execution.observation.ObservationCompiler` delegates the
    observation terminal here so no completion policy is scattered through observation
    building, and the loop derives its terminal status through :meth:`status_from_state`.
    """

    def pre_turn_terminal(self, state: LoopState) -> dict[str, Any] | None:
        """Return a terminal update if a pre-model-turn limit/decision fired, else ``None``."""

        return _terminal_guard(state)

    def observation_terminal_update(
        self,
        *,
        is_snapshot: bool,
        status: Any,
        unchanged_snapshot_count: int,
        observation: str,
    ) -> dict[str, Any] | None:
        """Terminate the run when ``browser_snapshot`` repeats an unchanged view.

        Only a successful snapshot whose unchanged streak reached
        ``settings.loop.max_unchanged_snapshots`` ends the goal; the terminal observation is
        replaced by the final-answer text.
        """

        if status != "success" or not is_snapshot:
            return None
        if int(unchanged_snapshot_count or 0) < get_settings().loop.max_unchanged_snapshots:
            return None
        final_answer = REPEATED_SNAPSHOT_OBSERVATION_FINAL_ANSWER.format(
            observation=observation
        )
        return {
            "decision": "done",
            "final_answer": final_answer,
            "observation": final_answer,
        }

    @staticmethod
    def status_from_state(state: LoopState) -> "CompletionStatus":
        """Derive a terminal status from ``LoopState`` (ports ``_completion_status_from_agent_state``)."""

        final_answer = str(state.final_answer or "").strip()
        if not final_answer:
            return "continue"
        if final_answer.lower().startswith("blocked:"):
            return "blocked"
        if str(state.decision or "").strip().lower() == "blocked":
            return "blocked"
        return "done"


def _has_reusable_current_snapshot(state: LoopState) -> bool:
    return bool(str(state.browser.snapshot or "").strip()) and not bool(
        state.browser.needs_fresh_snapshot
    )


def _snapshot_reuse_replan_update(state: LoopState) -> dict[str, Any]:
    return _replan_response(
        (
            "browser.snapshot was just blocked because the current snapshot is "
            "already reusable. Continue from the existing snapshot; use "
            "browser_find or browser.evaluate only if the current snapshot cannot "
            "answer the next step. Do not request another browser.snapshot just "
            "to vary depth."
        ),
        last_tool=state.last_tool,
        last_args=state.last_args,
        repeat_count=int(state.repeat_count or 0),
    )


def _snapshot_tool_request(reason: str) -> ToolRequest:
    return with_tool_call_id(
        {
            "name": SNAPSHOT_TOOL,
            "args": {},
            "reason": reason,
        }
    )


def _pending_tab_activation_request(state: LoopState) -> ToolRequest | None:
    """Select a tab recorded in ``browser.pending_browser_tab_index`` (if any).

    Nothing in the native loop sets the pending tab any more (server-specific tab-text
    parsing was removed); a value carried over in session state is still honoured once.
    """

    tab_index = int(state.browser.pending_browser_tab_index or 0)
    if tab_index <= 0:
        return None

    reason = str(state.browser.pending_browser_tab_reason or "").strip()
    if not reason:
        reason = (
            f"Switch to browser tab {tab_index} that was opened by the last "
            "browser action before taking a snapshot."
        )
    return with_tool_call_id(
        {
            "name": TABS_TOOL,
            "args": {"action": "select", "index": tab_index},
            "reason": reason,
        }
    )


def _snapshot_tool_call_update(state: LoopState, reason: str) -> dict[str, Any]:
    request = _snapshot_tool_request(reason)
    return {
        "decision": "tool_call",
        "tool_request": request,
        "policy_decision": "",
        "error": str(state.error or ""),
        "needs_fresh_snapshot": False,
        "last_tool": request["name"],
        "last_args": request["args"],
        "last_tool_request": request,
        "repeat_count": 1,
    }


def _guard_tool_request(state: LoopState, request: ToolRequest) -> dict[str, Any] | None:
    if (
        request.get("name") == SNAPSHOT_TOOL
        and _has_reusable_current_snapshot(state)
        and _snapshot_reuse_was_blocked(state)
    ):
        return _snapshot_reuse_replan_update(state)

    if (
        _repeat_tracking_key(state.last_tool, state.last_args)
        == _repeat_tracking_key(request.get("name", ""), request.get("args", {}))
        and int(state.repeat_count or 0) >= 2
    ):
        if request.get("name") == SNAPSHOT_TOOL:
            if int(state.unchanged_snapshot_count or 0) < 2:
                return None
            observation = str(state.observation or "No observation available.")
            return {
                **_done_response(
                    state,
                    REPEATED_SNAPSHOT_FINAL_ANSWER.format(observation=observation),
                ),
                "last_tool": request.get("name", ""),
                "last_args": request.get("args", {}),
                "repeat_count": 3,
            }

        return {
            **_replan_response(
                "The same tool with the same arguments was requested three "
                "consecutive times. Replan before trying another action."
            ),
            "last_tool": request.get("name", ""),
            "last_args": request.get("args", {}),
            "repeat_count": 3,
        }

    return None


def _repeat_tracking_key(
    tool_name: Any,
    args: dict[str, Any] | None,
) -> tuple[str, tuple[tuple[str, Any], ...]]:
    name = str(tool_name or "")
    normalized_args = dict(args or {})
    if name == SNAPSHOT_TOOL:
        normalized_args.pop("depth", None)
    return name, tuple(sorted(normalized_args.items()))


def _request_tracking_update(state: LoopState, request: ToolRequest) -> dict[str, Any]:
    tool_name = request.get("name", "")
    args = request.get("args", {})
    if _repeat_tracking_key(state.last_tool, state.last_args) == _repeat_tracking_key(
        tool_name, args
    ):
        repeat_count = int(state.repeat_count or 0) + 1
    else:
        repeat_count = 1
    tool_request = {**request, "args": args}
    return {
        "last_tool": tool_name,
        "last_args": args,
        "last_tool_request": tool_request,
        "repeat_count": repeat_count,
    }


def _tool_request_update(
    state: LoopState,
    messages: list[Any],
    tool_request: ToolRequest,
) -> dict[str, Any]:
    guarded = _guard_tool_request(state, tool_request)
    if guarded is not None:
        guarded_request = guarded.get("tool_request")
        if guarded.get("decision") == "tool_call" and isinstance(guarded_request, dict):
            guarded["messages"] = append_ai_tool_call(messages, guarded_request)
            return guarded

        if guarded.get("decision") == "done":
            return guarded

        messages_with_call = append_ai_tool_call(messages, tool_request)
        guarded["messages"] = append_tool_message(
            messages_with_call,
            tool_request,
            (
                f"{tool_request.get('name', '')}\n\n"
                f"{guarded.get('observation', '')}"
            ),
        )
        return guarded

    return {
        "decision": "tool_call",
        "tool_request": tool_request,
        "policy_decision": "",
        "error": "",
        "messages": append_ai_tool_call(messages, tool_request),
        **_request_tracking_update(state, tool_request),
    }


def _fresh_snapshot_request(state: LoopState, messages: list[Any]) -> dict[str, Any]:
    """Force a ``browser_snapshot`` when ``browser.needs_fresh_snapshot`` is set."""

    update = _snapshot_tool_call_update(state, FRESH_SNAPSHOT_REASON)
    request = update["tool_request"]
    return {
        **update,
        "messages": append_ai_tool_call(messages, request),
    }


def _stale_snapshot_retry_update(state: LoopState) -> dict[str, Any]:
    """Deprecated no-op kept for ``loop.py`` compatibility.

    The invalid-ref retry/replan cycle was removed; the call in
    ``TurnController._agent_step`` can be deleted together with this function.
    """

    return {}


# Public aliases for loop/tests; internal bodies keep the ported names.
blocked_response = _blocked_response
done_response = _done_response
complete_plan_update = _complete_plan_update
replan_response = _replan_response
terminal_guard = _terminal_guard
has_reusable_current_snapshot = _has_reusable_current_snapshot
snapshot_reuse_was_blocked = _snapshot_reuse_was_blocked
snapshot_reuse_replan_update = _snapshot_reuse_replan_update
snapshot_tool_request = _snapshot_tool_request
pending_tab_activation_request = _pending_tab_activation_request
snapshot_tool_call_update = _snapshot_tool_call_update
guard_tool_request = _guard_tool_request
repeat_tracking_key = _repeat_tracking_key
request_tracking_update = _request_tracking_update
tool_request_update = _tool_request_update
fresh_snapshot_request = _fresh_snapshot_request
stale_snapshot_retry_update = _stale_snapshot_retry_update


__all__ = [
    "FRESH_SNAPSHOT_REASON",
    "REPEATED_SNAPSHOT_FINAL_ANSWER",
    "REPEATED_SNAPSHOT_OBSERVATION_FINAL_ANSWER",
    "SNAPSHOT_REUSE_MARKER",
    "SNAPSHOT_REUSE_MARKERS",
    "CompletionController",
    "blocked_response",
    "complete_plan_update",
    "done_response",
    "fresh_snapshot_request",
    "guard_tool_request",
    "has_reusable_current_snapshot",
    "pending_tab_activation_request",
    "repeat_tracking_key",
    "replan_response",
    "request_tracking_update",
    "snapshot_reuse_replan_update",
    "snapshot_reuse_was_blocked",
    "snapshot_tool_call_update",
    "snapshot_tool_request",
    "stale_snapshot_retry_update",
    "terminal_guard",
    "tool_request_update",
]