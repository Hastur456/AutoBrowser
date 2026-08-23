"""Engine-native guards, terminal checks, and repeat/ineffective tracking.

Ported from ``src/agent/utils.py`` (the LangGraph agent-node helpers). The control
logic and every user-facing string are preserved verbatim; only the state access is
rewritten from ``AgentState``-dict ``.get(...)`` reads to typed
:class:`~src.agent_loop.execution.state.LoopState` attribute access. Each function
still returns the same flat update dict the legacy node returned, so the loop applies
it through :meth:`LoopState.apply` (which routes browser-scoped keys into
``BrowserState``).

Stateless leaf helpers (``element_description_from_snapshot``, the ref utilities, the
message builders) are reused directly from their existing modules — only the stateful
control logic is re-homed here.
"""

from __future__ import annotations

from typing import Any

from src.contracts import ToolRequest
from src.browser.observation import (
    REF_INTERACTION_TOOLS,
    has_invalid_ref_text,
    request_ref_value,
    snapshot_contains_ref,
)
from src.browser.adapters import element_description_from_snapshot
from src.harness.memory import (
    append_ai_tool_call,
    append_final_ai_response,
    append_tool_message,
    ensure_message_history,
    with_tool_call_id,
)

from src.agent_loop.execution.state import (
    MAX_CONSECUTIVE_FAILURES,
    MAX_REPLANS,
    MAX_STEPS_WITHOUT_PLAN_ADVANCE,
    LoopState,
)

SNAPSHOT_REUSE_MARKERS = (
    "browser.snapshot is already current",
    "browser_snapshot is already current",
)
SNAPSHOT_REUSE_MARKER = SNAPSHOT_REUSE_MARKERS[0]
REPEATED_SNAPSHOT_FINAL_ANSWER = (
    "Stopped because browser.snapshot was requested three consecutive times "
    "without a meaningful state change. Latest observation:\n\n{observation}"
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
        "stale_snapshot_retries": 0,
        "needs_fresh_snapshot": False,
    }
    response.update(updates)
    return response


def _terminal_guard(state: LoopState) -> dict[str, Any] | None:
    if state.decision == "done" and state.final_answer:
        return _done_response(state, str(state.final_answer or ""))

    replan_count = int(state.replan_count or 0)
    if replan_count >= MAX_REPLANS:
        observation = str(state.observation or "No observation available.")
        return _blocked_response(
            state,
            (
                f"Blocked: replanning reached the limit of {MAX_REPLANS}. "
                f"Last observation: {observation}"
            ),
        )

    failures = int(state.consecutive_failures or 0)
    if failures >= MAX_CONSECUTIVE_FAILURES:
        error = str(state.error or "Unknown tool failure.")
        return _blocked_response(
            state,
            (
                "Blocked: tool execution failed "
                f"{MAX_CONSECUTIVE_FAILURES} consecutive times. Last error: {error}"
            ),
        )

    steps_without_plan_advance = int(state.steps_without_plan_advance or 0)
    if steps_without_plan_advance >= MAX_STEPS_WITHOUT_PLAN_ADVANCE:
        observation = str(state.observation or "No observation available.")
        return _blocked_response(
            state,
            (
                "Blocked: the current plan step did not advance after "
                f"{MAX_STEPS_WITHOUT_PLAN_ADVANCE} successful tool steps. "
                f"Last observation: {observation}"
            ),
        )

    return None


def _has_reusable_current_snapshot(state: LoopState) -> bool:
    return (
        bool(str(state.browser.snapshot or "").strip())
        and not bool(state.browser.needs_fresh_snapshot)
        and not has_invalid_ref_text(state.error)
    )


def _snapshot_reuse_was_blocked(state: LoopState) -> bool:
    policy_event = state.policy_event or {}
    reason = str(policy_event.get("reason", "") or "")
    observation = str(state.observation or "")
    error = str(state.error or "")
    payload = "\n".join([reason, observation, error]).lower()
    return any(marker in payload for marker in SNAPSHOT_REUSE_MARKERS)


def _snapshot_reuse_replan_update(state: LoopState) -> dict[str, Any]:
    return _replan_response(
        (
            "browser.snapshot was just blocked because the current snapshot is "
            "already reusable. Continue from the existing snapshot and refs; use "
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
            "name": "browser_snapshot",
            "args": {},
            "reason": reason,
        }
    )


def _pending_tab_activation_request(state: LoopState) -> ToolRequest | None:
    tab_index = int(state.browser.pending_browser_tab_index or 0)
    if tab_index <= 0:
        return None

    reason = str(state.browser.pending_browser_tab_reason or "").strip()
    if not reason:
        reason = (
            f"Switch to browser tab {tab_index} that was opened by the last "
            "browser action before taking a snapshot or using page refs."
        )
    return with_tool_call_id(
        {
            "name": "browser_tabs",
            "args": {"action": "select", "index": tab_index},
            "reason": reason,
        }
    )


def _snapshot_tool_call_update(
    state: LoopState,
    reason: str,
    *,
    increment_invalid_ref_recovery: bool = False,
) -> dict[str, Any]:
    request = _snapshot_tool_request(reason)
    recovery_count = int(state.invalid_ref_recovery_count or 0)
    return {
        "decision": "tool_call",
        "tool_request": request,
        "policy_decision": "",
        "error": str(state.error or ""),
        "needs_fresh_snapshot": False,
        "invalid_ref_recovery_count": (
            recovery_count + 1 if increment_invalid_ref_recovery else recovery_count
        ),
        "last_tool": request["name"],
        "last_args": request["args"],
        "last_tool_request": request,
        "repeat_count": 1,
    }


def _ref_action_snapshot_guard(
    state: LoopState,
    request: ToolRequest,
) -> dict[str, Any] | None:
    tool_name = str(request.get("name", "") or "").lower()
    if tool_name not in REF_INTERACTION_TOOLS:
        return None

    requested_ref = request_ref_value(request)
    if not requested_ref:
        return None

    snapshot = str(state.browser.snapshot or "")
    if not snapshot.strip():
        return _snapshot_tool_call_update(
            state,
            (
                "A ref-based browser action was requested without a current "
                "browser.snapshot. Capture a fresh snapshot before using refs "
                "from history or a prior page."
            ),
        )

    if snapshot_contains_ref(snapshot, requested_ref):
        return None

    return {
        **_replan_response(
            (
                f"Requested ref {requested_ref} is not present in the latest "
                "browser.snapshot. Use only refs from the current snapshot "
                "instead of reusing refs from history or a prior page."
            )
        ),
        **_request_tracking_update(state, request),
    }


def _browser_action_key(
    action: ToolRequest | dict[str, Any],
    snapshot: str = "",
) -> tuple[str, tuple[tuple[str, Any], ...]]:
    name = str(action.get("name", "") or "").lower()
    args = dict(action.get("args") or {})
    target = args.pop("ref", None) or args.pop("target", None)
    if target is not None:
        target_description = str(action.get("target_description", "") or "")
        if not target_description and snapshot:
            target_description = element_description_from_snapshot(snapshot, str(target))
        args["target"] = target_description or str(target)
    return name, tuple(sorted(args.items()))


def _ineffective_action_repeat_update(
    state: LoopState,
    request: ToolRequest,
) -> dict[str, Any] | None:
    ineffective_actions = list(state.browser.ineffective_browser_actions or [])
    ineffective_action = state.browser.ineffective_browser_action or {}
    if ineffective_action:
        ineffective_actions.append(ineffective_action)
    if not ineffective_actions:
        return None

    count = int(state.ineffective_action_count or 0)
    if count <= 0:
        return None

    request_key = _browser_action_key(request, str(state.browser.snapshot or ""))
    if not any(_browser_action_key(action) == request_key for action in ineffective_actions):
        return None

    return _replan_response(
        (
            f"{request.get('name', 'browser action')} with the same target did not "
            "change the visible browser_snapshot. Replan before trying another "
            "action: choose a different visible control/ref, request a deeper "
            "snapshot, use evaluate only if the snapshot cannot expose the "
            "control, or use a fallback route."
        ),
        ineffective_action_count=count,
    )


def _guard_tool_request(state: LoopState, request: ToolRequest) -> dict[str, Any] | None:
    ineffective_repeat = _ineffective_action_repeat_update(state, request)
    if ineffective_repeat is not None:
        return ineffective_repeat

    if (
        request.get("name") == "browser_snapshot"
        and _has_reusable_current_snapshot(state)
        and _snapshot_reuse_was_blocked(state)
    ):
        return _snapshot_reuse_replan_update(state)

    snapshot = str(state.browser.snapshot or "")
    if (
        _repeat_tracking_key(
            state.last_tool,
            state.last_args,
            snapshot,
        )
        == _repeat_tracking_key(request.get("name", ""), request.get("args", {}), snapshot)
        and int(state.repeat_count or 0) >= 2
    ):
        if request.get("name") == "browser_snapshot":
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

    ref_snapshot_guard = _ref_action_snapshot_guard(state, request)
    if ref_snapshot_guard is not None:
        return ref_snapshot_guard

    return None


def _repeat_tracking_key(
    tool_name: Any,
    args: dict[str, Any] | None,
    snapshot: str = "",
) -> tuple[str, tuple[tuple[str, Any], ...]]:
    name = str(tool_name or "")
    normalized_args = dict(args or {})
    if name == "browser_snapshot":
        normalized_args.pop("depth", None)
    target = normalized_args.pop("ref", None) or normalized_args.pop("target", None)
    if target is not None:
        target_description = (
            element_description_from_snapshot(snapshot, str(target)) if snapshot else ""
        )
        normalized_args["target"] = target_description or str(target)
    return name, tuple(sorted(normalized_args.items()))


def _request_tracking_update(state: LoopState, request: ToolRequest) -> dict[str, Any]:
    tool_name = request.get("name", "")
    args = request.get("args", {})
    snapshot = str(state.browser.snapshot or "")
    if _repeat_tracking_key(state.last_tool, state.last_args, snapshot) == (
        _repeat_tracking_key(tool_name, args, snapshot)
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
    update = _snapshot_tool_call_update(
        state,
        (
            "Previous browser ref is no longer valid for the current "
            "browser.snapshot. Capture a fresh snapshot before any "
            "ref-based action."
        ),
        increment_invalid_ref_recovery=True,
    )
    request = update["tool_request"]
    return {
        **update,
        "messages": append_ai_tool_call(messages, request),
    }


def _stale_snapshot_retry_update(state: LoopState) -> dict[str, Any]:
    if not state.browser.needs_fresh_snapshot:
        return {"stale_snapshot_retries": 0}

    if not has_invalid_ref_text(state.error):
        return {"stale_snapshot_retries": 0}

    if int(state.invalid_ref_recovery_count or 0) <= 0:
        return {"stale_snapshot_retries": 0}

    retries = int(state.stale_snapshot_retries or 0) + 1
    if retries >= 2:
        return _replan_response(
            (
                "A fresh browser_snapshot did not resolve the invalid ref twice. "
                "Replan before attempting another ref-based browser action."
            ),
            stale_snapshot_retries=0,
            invalid_ref_recovery_count=0,
            snapshot="",
        )

    return {"stale_snapshot_retries": retries}


# Public aliases for loop/tests; internal bodies keep the ported names verbatim.
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
ref_action_snapshot_guard = _ref_action_snapshot_guard
browser_action_key = _browser_action_key
ineffective_action_repeat_update = _ineffective_action_repeat_update
guard_tool_request = _guard_tool_request
repeat_tracking_key = _repeat_tracking_key
request_tracking_update = _request_tracking_update
tool_request_update = _tool_request_update
fresh_snapshot_request = _fresh_snapshot_request
stale_snapshot_retry_update = _stale_snapshot_retry_update


__all__ = [
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_REPLANS",
    "MAX_STEPS_WITHOUT_PLAN_ADVANCE",
    "REPEATED_SNAPSHOT_FINAL_ANSWER",
    "SNAPSHOT_REUSE_MARKER",
    "SNAPSHOT_REUSE_MARKERS",
    "blocked_response",
    "browser_action_key",
    "complete_plan_update",
    "done_response",
    "fresh_snapshot_request",
    "guard_tool_request",
    "has_reusable_current_snapshot",
    "ineffective_action_repeat_update",
    "pending_tab_activation_request",
    "ref_action_snapshot_guard",
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
