"""Model invocation and proposed-action parsing for the transitional loop."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from src.agent_loop.actions import (
    ActionKind,
    ProposedAction,
    answer_action,
    ask_user_action,
    compact_memory_action,
    delegate_action,
    normalize_tool_request,
    stop_action,
    tool_call_action,
    update_plan_action,
)


@dataclass(frozen=True)
class ModelTurn:
    """One raw model response together with parsed actions."""

    response: Any
    actions: list[ProposedAction]
    metadata: dict[str, Any] = field(default_factory=dict)


class ActionParser:
    """Normalize one model response into typed proposed actions."""

    def parse(self, response: Any) -> list[ProposedAction]:
        tool_calls = list(getattr(response, "tool_calls", None) or [])
        if tool_calls:
            actions = [
                self._tool_call_to_action(tool_call)
                for tool_call in tool_calls
            ]
            actions = [action for action in actions if action is not None]
            if actions:
                return actions

        content = _message_content(response)
        payload = _json_object(content)
        if payload:
            actions = self.parse_payload(payload, fallback_content=content)
            if actions:
                return actions

        content = content.strip()
        if content:
            return [answer_action(content)]
        return [stop_action("Empty model response.", status="blocked")]

    def parse_payload(
        self,
        payload: Any,
        *,
        fallback_content: str = "",
    ) -> list[ProposedAction]:
        if isinstance(payload, list):
            actions: list[ProposedAction] = []
            for item in payload:
                if isinstance(item, Mapping):
                    actions.extend(
                        self._parse_record(dict(item), fallback_content=fallback_content)
                    )
            return actions
        if isinstance(payload, Mapping):
            if isinstance(payload.get("actions"), list):
                actions: list[ProposedAction] = []
                for item in payload["actions"]:
                    if isinstance(item, Mapping):
                        actions.extend(
                            self._parse_record(
                                dict(item),
                                fallback_content=fallback_content,
                            )
                        )
                if actions:
                    return actions
            return self._parse_record(dict(payload), fallback_content=fallback_content)
        return []

    def _parse_record(
        self,
        record: dict[str, Any],
        *,
        fallback_content: str,
    ) -> list[ProposedAction]:
        kind = str(
            record.get("kind")
            or record.get("type")
            or record.get("decision")
            or ""
        ).strip()

        if kind in {"tool_call", "tool"}:
            return [self._tool_call_from_record(record, fallback_content=fallback_content)]
        if kind in {"answer", "done", "final"}:
            return [self._answer_from_record(record, fallback_content=fallback_content)]
        if kind in {"ask_user", "ask"}:
            return [self._ask_user_from_record(record, fallback_content=fallback_content)]
        if kind in {"update_plan", "replan", "plan"}:
            return [self._update_plan_from_record(record, fallback_content=fallback_content)]
        if kind == "delegate":
            return [self._delegate_from_record(record, fallback_content=fallback_content)]
        if kind in {"compact_memory", "compact"}:
            return [self._compact_memory_from_record(record, fallback_content=fallback_content)]
        if kind in {"stop", "blocked", "cancelled", "failed"}:
            return [self._stop_from_record(record, fallback_content=fallback_content)]

        if "tool_request" in record or "name" in record:
            return [self._tool_call_from_record(record, fallback_content=fallback_content)]
        if "final_answer" in record:
            return [self._answer_from_record(record, fallback_content=fallback_content)]
        if fallback_content.strip():
            return [answer_action(fallback_content.strip())]
        return [stop_action("Empty model response.", status="blocked")]

    def _tool_call_to_action(self, tool_call: Any) -> ProposedAction | None:
        if isinstance(tool_call, Mapping):
            request = normalize_tool_request(tool_call)
            if not request.get("name"):
                return None
            return tool_call_action(
                request,
                reason="Selected by bound tool call.",
                action_id=str(tool_call.get("id", "") or ""),
            )

        request = normalize_tool_request(
            {
                "name": getattr(tool_call, "name", ""),
                "args": getattr(tool_call, "args", {}),
                "id": getattr(tool_call, "id", ""),
            }
        )
        if not request.get("name"):
            return None
        return tool_call_action(request, reason="Selected by bound tool call.")

    def _tool_call_from_record(
        self,
        record: dict[str, Any],
        *,
        fallback_content: str,
    ) -> ProposedAction:
        tool_request = normalize_tool_request(record.get("tool_request") or record)
        if tool_request.get("name"):
            return tool_call_action(
                tool_request,
                reason=_action_reason(record, fallback_content)
                or "Selected by model.",
                action_id=str(record.get("id", "") or tool_request.get("id", "") or ""),
            )
        return update_plan_action(
            _action_reason(record, fallback_content)
            or "The model selected a tool call without a tool name.",
            action_id=str(record.get("id", "") or ""),
        )

    def _answer_from_record(
        self,
        record: dict[str, Any],
        *,
        fallback_content: str,
    ) -> ProposedAction:
        final_answer = str(record.get("final_answer") or fallback_content or "").strip()
        return answer_action(
            final_answer or fallback_content.strip(),
            reason=_action_reason(record, fallback_content),
            action_id=str(record.get("id", "") or ""),
        )

    def _ask_user_from_record(
        self,
        record: dict[str, Any],
        *,
        fallback_content: str,
    ) -> ProposedAction:
        question = str(
            record.get("question")
            or record.get("prompt")
            or record.get("reason")
            or fallback_content
            or "User input required."
        ).strip()
        choices = record.get("choices")
        return ask_user_action(
            question,
            reason=_action_reason(record, fallback_content),
            choices=list(choices) if isinstance(choices, list) else None,
            context=str(record.get("context", "") or ""),
            action_id=str(record.get("id", "") or ""),
        )

    def _update_plan_from_record(
        self,
        record: dict[str, Any],
        *,
        fallback_content: str,
    ) -> ProposedAction:
        plan = record.get("plan")
        patch = record.get("patch")
        return update_plan_action(
            _action_reason(record, fallback_content)
            or "Replanning requested.",
            plan=list(plan) if isinstance(plan, list) else None,
            patch=list(patch) if isinstance(patch, list) else None,
            action_id=str(record.get("id", "") or ""),
        )

    def _delegate_from_record(
        self,
        record: dict[str, Any],
        *,
        fallback_content: str,
    ) -> ProposedAction:
        role = str(record.get("role", "") or "").strip()
        objective = str(record.get("objective", "") or fallback_content or "").strip()
        artifacts = record.get("input_artifacts")
        max_turns = record.get("max_turns")
        return delegate_action(
            role=role,
            objective=objective,
            input_artifacts=list(artifacts) if isinstance(artifacts, list) else None,
            max_turns=int(max_turns) if isinstance(max_turns, int) else None,
            reason=_action_reason(record, fallback_content),
            action_id=str(record.get("id", "") or ""),
        )

    def _compact_memory_from_record(
        self,
        record: dict[str, Any],
        *,
        fallback_content: str,
    ) -> ProposedAction:
        summary = str(record.get("summary") or fallback_content or "").strip()
        preserve = record.get("preserve")
        return compact_memory_action(
            summary or "Memory compaction requested.",
            preserve=list(preserve) if isinstance(preserve, list) else None,
            reason=_action_reason(record, fallback_content),
            action_id=str(record.get("id", "") or ""),
        )

    def _stop_from_record(
        self,
        record: dict[str, Any],
        *,
        fallback_content: str,
    ) -> ProposedAction:
        status = str(record.get("status") or record.get("decision") or "blocked").strip()
        if status not in {"blocked", "cancelled", "failed", "done"}:
            status = "blocked"
        message = str(
            record.get("message")
            or record.get("reason")
            or fallback_content
            or "Stop requested."
        ).strip()
        return stop_action(
            message,
            status=status,  # type: ignore[arg-type]
            reason=_action_reason(record, fallback_content),
            action_id=str(record.get("id", "") or ""),
        )

    def _parse_payload(
        self,
        payload: dict[str, Any],
        *,
        fallback_content: str,
    ) -> list[ProposedAction]:
        return self._parse_record(payload, fallback_content=fallback_content)


class ModelDriver:
    """Bind tools, invoke the model, and parse the resulting actions."""

    def __init__(
        self,
        llm: Any,
        *,
        tools: Sequence[Any] | None = None,
        tool_registry: Any | None = None,
        action_parser: ActionParser | None = None,
    ) -> None:
        self._llm = llm
        self._tools = list(tools) if tools is not None else None
        self._tool_registry = tool_registry
        self._bound_llm: Any | None = None
        self._action_parser = action_parser or ActionParser()

    async def invoke(
        self,
        messages: Sequence[Any],
        *,
        tools: Sequence[Any] | None = None,
    ) -> ModelTurn:
        available_tools = await self._resolve_tools(tools)
        model = self._bind_tools(available_tools)
        response = await model.ainvoke(list(messages))
        actions = self._action_parser.parse(response)
        metadata = {
            "response_type": type(response).__name__,
            "tool_count": len(available_tools),
            "tool_call_count": len(list(getattr(response, "tool_calls", None) or [])),
        }
        return ModelTurn(response=response, actions=actions, metadata=metadata)

    async def _resolve_tools(self, tools: Sequence[Any] | None) -> list[Any]:
        if tools is not None:
            return list(tools)
        if self._tools is not None:
            return list(self._tools)
        if self._tool_registry is not None:
            resolved = await self._tool_registry.get_all()
            self._tools = list(resolved)
            return list(resolved)
        return []

    def _bind_tools(self, tools: Sequence[Any]) -> Any:
        if self._bound_llm is not None:
            return self._bound_llm
        if not tools or not hasattr(self._llm, "bind_tools"):
            self._bound_llm = self._llm
            return self._bound_llm
        try:
            self._bound_llm = self._llm.bind_tools(list(tools))
        except NotImplementedError:
            self._bound_llm = self._llm
        return self._bound_llm


def _message_content(response: Any) -> str:
    return str(getattr(response, "content", response))


def _json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _action_reason(record: dict[str, Any], fallback_content: str) -> str:
    reason = str(record.get("reason", "") or "").strip()
    if reason:
        return reason
    return fallback_content.strip()


__all__ = [
    "ActionParser",
    "ModelDriver",
    "ModelTurn",
]
