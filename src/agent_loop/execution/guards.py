"""Engine-native guards, terminal checks, and repeat tracking.

Ported from the legacy agent-loop guard helpers. State access is typed
:class:`~src.agent_loop.execution.state.LoopState` attribute access. Each function returns
a flat update dict that the loop applies through :meth:`LoopState.apply` (which routes
browser-scoped keys into ``BrowserState``).

Server-neutral: no element-ref handling and no canonical tool-name mapping. Repeat tracking
compares tool names and arguments as given; the progress guard reads the action journal
(:mod:`~src.agent_loop.execution.progress`).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from src.browser.names import TABS_TOOL
from src.config import get_settings
from src.contracts import ToolRequest
from src.harness.memory import (
    append_ai_tool_call,
    append_final_ai_response,
    append_tool_message,
    ensure_message_history,
    with_tool_call_id,
)

from src.agent_loop.execution.progress import ineffective_repeat_reason
from src.agent_loop.execution.state import LoopState

if TYPE_CHECKING:
    from src.contracts import CompletionStatus


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
        "completion_status": "blocked",
        "observation": reason,
        "error": reason,
        "messages": append_final_ai_response(messages, reason),
    }


def _done_response(
    state: LoopState,
    final_answer: str,
    *,
    messages: list[Any] | None = None,
    status: str = "done",
    **updates: Any,
) -> dict[str, Any]:
    """Terminal update; ``status`` is the explicit completion status of the run."""

    history = messages if messages is not None else ensure_message_history(_message_state(state))
    response = {
        "decision": "done",
        "final_answer": final_answer,
        "completion_status": status,
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
    }
    response.update(updates)
    return response


def _terminal_guard(state: LoopState) -> dict[str, Any] | None:
    limits = get_settings().loop

    if state.decision == "done" and state.final_answer:
        return _done_response(
            state,
            str(state.final_answer or ""),
            status=str(state.completion_status or "done"),
        )

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

    Wraps the pre-turn :func:`_terminal_guard` (replan / consecutive-failure /
    stalled-plan limits); the loop derives its terminal status through
    :meth:`status_from_state`.
    """

    def pre_turn_terminal(self, state: LoopState) -> dict[str, Any] | None:
        """Return a terminal update if a pre-model-turn limit/decision fired, else ``None``."""

        return _terminal_guard(state)

    @staticmethod
    def status_from_state(state: LoopState) -> "CompletionStatus":
        """Derive a terminal status from ``LoopState``.

        The explicit ``completion_status`` set by the deciding code wins; the final-answer
        prefix check is only a fallback for states that predate the field.
        """

        explicit = str(state.completion_status or "").strip().lower()
        if explicit in {"done", "blocked", "cancelled"}:
            return explicit  # type: ignore[return-value]
        final_answer = str(state.final_answer or "").strip()
        if not final_answer:
            return "continue"
        if final_answer.lower().startswith("blocked:"):
            return "blocked"
        if str(state.decision or "").strip().lower() == "blocked":
            return "blocked"
        return "done"


def _progress_block_reason(state: LoopState, request: ToolRequest | None) -> str | None:
    """Progress guard on the raw request: a reason to skip the call, else ``None``.

    Validation and quality, not authorization: an empty request, or a call whose identical
    outcome already repeated ``loop.max_ineffective_actions`` times.
    """

    if not request or not request.get("name"):
        return "No tool request was provided."
    return ineffective_repeat_reason(
        state.action_history,
        str(request["name"]).strip(),
        request.get("args") or {},
        get_settings().loop.max_ineffective_actions,
    )


def _tool_block_updates(
    state: LoopState,
    reason: str,
    *,
    event: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Non-terminal block of the current tool call: the model sees ``reason`` as the tool output.

    The shared path for a progress-guard block, a hook deny and a permission deny:
    ``consecutive_failures`` grows by one, the reason is appended as the tool message, and
    ``policy_event`` records the block (``event`` adds fields such as ``source``/``rule_id``).
    """

    request = state.tool_request or {}
    return {
        "policy_decision": "blocked",
        "observation": reason,
        "error": reason,
        "consecutive_failures": int(state.consecutive_failures or 0) + 1,
        "policy_event": {
            "decision": "blocked",
            "reason": reason,
            "tool_request": request,
            **dict(event or {}),
        },
        "messages": append_tool_message(
            list(state.messages or []),
            request,
            f"{request.get('name', '')}\n\n{reason}",
        ),
    }


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


def _guard_tool_request(state: LoopState, request: ToolRequest) -> dict[str, Any] | None:
    if (
        _repeat_tracking_key(state.last_tool, state.last_args)
        == _repeat_tracking_key(request.get("name", ""), request.get("args", {}))
        and int(state.repeat_count or 0) >= 2
    ):
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
    return str(tool_name or ""), tuple(sorted(dict(args or {}).items()))


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


# Public aliases for loop/tests; internal bodies keep the ported names.
blocked_response = _blocked_response
done_response = _done_response
complete_plan_update = _complete_plan_update
replan_response = _replan_response
terminal_guard = _terminal_guard
pending_tab_activation_request = _pending_tab_activation_request
progress_block_reason = _progress_block_reason
tool_block_updates = _tool_block_updates
guard_tool_request = _guard_tool_request
repeat_tracking_key = _repeat_tracking_key
request_tracking_update = _request_tracking_update
tool_request_update = _tool_request_update


__all__ = [
    "CompletionController",
    "blocked_response",
    "complete_plan_update",
    "done_response",
    "guard_tool_request",
    "pending_tab_activation_request",
    "progress_block_reason",
    "repeat_tracking_key",
    "replan_response",
    "request_tracking_update",
    "terminal_guard",
    "tool_block_updates",
    "tool_request_update",
]