"""Typed proposed actions for the transitional AutoBrowser model loop."""

from __future__ import annotations

from typing import Any, Literal, Required, TypeAlias, TypedDict

from src.contracts import PlanStep, ToolRequest

ActionKind = Literal[
    "answer",
    "tool_call",
    "ask_user",
    "update_plan",
    "delegate",
    "compact_memory",
    "stop",
]

StopStatus = Literal["blocked", "cancelled", "failed", "done"]


class ProposedActionBase(TypedDict, total=False):
    """Shared fields carried by all proposed actions."""

    id: str
    reason: str
    metadata: dict[str, Any]


class AnswerAction(ProposedActionBase):
    kind: Required[Literal["answer"]]
    final_answer: Required[str]


class ToolCallAction(ProposedActionBase):
    kind: Required[Literal["tool_call"]]
    tool_request: Required[ToolRequest]


class AskUserAction(ProposedActionBase):
    kind: Required[Literal["ask_user"]]
    question: Required[str]
    choices: list[str]
    context: str


class UpdatePlanAction(ProposedActionBase, total=False):
    kind: Required[Literal["update_plan"]]
    reason: Required[str]
    plan: list[PlanStep]
    patch: list[PlanStep]


class DelegateAction(ProposedActionBase):
    kind: Required[Literal["delegate"]]
    role: Required[str]
    objective: Required[str]
    input_artifacts: list[str]
    max_turns: int


class CompactMemoryAction(ProposedActionBase):
    kind: Required[Literal["compact_memory"]]
    summary: Required[str]
    preserve: list[str]


class StopAction(ProposedActionBase):
    kind: Required[Literal["stop"]]
    status: Required[StopStatus]
    message: Required[str]


ProposedAction: TypeAlias = (
    AnswerAction
    | ToolCallAction
    | AskUserAction
    | UpdatePlanAction
    | DelegateAction
    | CompactMemoryAction
    | StopAction
)


def answer_action(
    final_answer: str,
    *,
    reason: str = "",
    action_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> AnswerAction:
    action: AnswerAction = {"kind": "answer", "final_answer": final_answer}
    if reason:
        action["reason"] = reason
    if action_id:
        action["id"] = action_id
    if metadata:
        action["metadata"] = dict(metadata)
    return action


def tool_call_action(
    tool_request: ToolRequest,
    *,
    reason: str = "",
    action_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> ToolCallAction:
    action: ToolCallAction = {
        "kind": "tool_call",
        "tool_request": normalize_tool_request(tool_request),
    }
    if reason:
        action["reason"] = reason
    if action_id:
        action["id"] = action_id
    if metadata:
        action["metadata"] = dict(metadata)
    return action


def ask_user_action(
    question: str,
    *,
    reason: str = "",
    choices: list[str] | None = None,
    context: str = "",
    action_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> AskUserAction:
    action: AskUserAction = {"kind": "ask_user", "question": question}
    if reason:
        action["reason"] = reason
    if choices:
        action["choices"] = list(choices)
    if context:
        action["context"] = context
    if action_id:
        action["id"] = action_id
    if metadata:
        action["metadata"] = dict(metadata)
    return action


def update_plan_action(
    reason: str,
    *,
    plan: list[PlanStep] | None = None,
    patch: list[PlanStep] | None = None,
    action_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> UpdatePlanAction:
    action: UpdatePlanAction = {"kind": "update_plan", "reason": reason}
    if plan:
        action["plan"] = list(plan)
    if patch:
        action["patch"] = list(patch)
    if action_id:
        action["id"] = action_id
    if metadata:
        action["metadata"] = dict(metadata)
    return action


def delegate_action(
    role: str,
    objective: str,
    *,
    input_artifacts: list[str] | None = None,
    max_turns: int | None = None,
    reason: str = "",
    action_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> DelegateAction:
    action: DelegateAction = {
        "kind": "delegate",
        "role": role,
        "objective": objective,
    }
    if input_artifacts:
        action["input_artifacts"] = list(input_artifacts)
    if max_turns is not None:
        action["max_turns"] = max_turns
    if reason:
        action["reason"] = reason
    if action_id:
        action["id"] = action_id
    if metadata:
        action["metadata"] = dict(metadata)
    return action


def compact_memory_action(
    summary: str,
    *,
    preserve: list[str] | None = None,
    reason: str = "",
    action_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> CompactMemoryAction:
    action: CompactMemoryAction = {"kind": "compact_memory", "summary": summary}
    if preserve:
        action["preserve"] = list(preserve)
    if reason:
        action["reason"] = reason
    if action_id:
        action["id"] = action_id
    if metadata:
        action["metadata"] = dict(metadata)
    return action


def stop_action(
    message: str,
    *,
    status: StopStatus = "blocked",
    reason: str = "",
    action_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> StopAction:
    action: StopAction = {"kind": "stop", "status": status, "message": message}
    if reason:
        action["reason"] = reason
    if action_id:
        action["id"] = action_id
    if metadata:
        action["metadata"] = dict(metadata)
    return action


def is_terminal_action(action: ProposedAction | dict[str, Any]) -> bool:
    kind = str(action.get("kind", "") or "")
    return kind in {"answer", "stop"}


def normalize_tool_request(raw_request: Any) -> ToolRequest:
    if not isinstance(raw_request, dict):
        return {"name": "", "args": {}, "reason": ""}

    args = raw_request.get("args")
    normalized: ToolRequest = {
        "name": str(raw_request.get("name", "")).strip(),
        "args": args if isinstance(args, dict) else {},
        "reason": str(raw_request.get("reason", "")).strip(),
    }
    request_id = str(raw_request.get("id", "")).strip()
    if request_id:
        normalized["id"] = request_id
    return normalized


__all__ = [
    "ActionKind",
    "AnswerAction",
    "AskUserAction",
    "CompactMemoryAction",
    "DelegateAction",
    "ProposedAction",
    "ProposedActionBase",
    "StopAction",
    "StopStatus",
    "ToolCallAction",
    "UpdatePlanAction",
    "answer_action",
    "ask_user_action",
    "compact_memory_action",
    "delegate_action",
    "is_terminal_action",
    "normalize_tool_request",
    "stop_action",
    "tool_call_action",
    "update_plan_action",
]
