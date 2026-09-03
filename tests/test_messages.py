"""Tests for the provider-neutral message model, tool schemas, and model contract.

These lock the wire-role semantics (``system``/``user``/``assistant``/``tool``),
the exact ``tool_call_id`` pairing rule, and the OpenAI-compatible ``ToolCall``
shape that the engine drives over ``ChatModel.complete``.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from src.contracts import ToolDef
from src.llm import ChatModel, ModelResponse
from src.messages import (
    Message,
    ToolCall,
    assistant_message,
    system_message,
    tool_message,
    user_message,
)


def test_builders_produce_canonical_roles() -> None:
    assert system_message("s").role == "system"
    assert user_message("u").role == "user"
    assert assistant_message("a").role == "assistant"
    assert tool_message("call_1", "ok").role == "tool"


def test_tool_call_openai_wire_shape() -> None:
    call = ToolCall(id="call_1", name="click", arguments={"ref": "e14"})

    assert call.to_dict() == {
        "id": "call_1",
        "type": "function",
        "function": {"name": "click", "arguments": {"ref": "e14"}},
    }


def test_tool_result_pairs_to_tool_call_by_id() -> None:
    call = ToolCall(id="call_1", name="click", arguments={"ref": "e14"})
    assistant = assistant_message(tool_calls=(call,))
    result = tool_message(tool_call_id=call.id, content="clicked", name=call.name)

    assert assistant.is_tool_call and not assistant.is_tool_result
    assert result.is_tool_result and not result.is_tool_call
    assert result.tool_call_id == call.id


def test_tool_call_id_mismatch_breaks_pairing() -> None:
    call = ToolCall(id="call_1", name="click", arguments={})
    result = tool_message(tool_call_id="call_OTHER", content="clicked", name="click")

    assert result.tool_call_id != call.id


def test_chronological_history_keeps_tool_pair() -> None:
    system = system_message("prompt")
    user = user_message("task")
    call = ToolCall(id="call_9", name="browser_snapshot", arguments={})
    assistant = assistant_message(tool_calls=(call,))
    result = tool_message(call.id, "snapshot captured", name=call.name)

    history = [system, user, assistant, result]

    assert [m.role for m in history] == [
        "system",
        "user",
        "assistant",
        "tool",
    ]
    # The tool result must immediately follow the assistant that requested it.
    assert history[2].tool_calls[0].id == history[3].tool_call_id


def test_messages_are_immutable() -> None:
    message = user_message("task")

    with pytest.raises(FrozenInstanceError):
        message.content = "other"  # type: ignore[misc]


def test_tool_def_is_schema_only() -> None:
    tool_def = ToolDef(name="click", description="Click a ref", input_schema={"x": 1})

    assert tool_def.name == "click"
    assert tool_def.description == "Click a ref"
    assert tool_def.input_schema == {"x": 1}
    assert ToolDef(name="empty").input_schema == {}


def test_model_response_flags_tool_calls() -> None:
    empty = ModelResponse(content="final")
    call = ToolCall(id="call_1", name="click", arguments={})
    with_calls = ModelResponse(content="", tool_calls=(call,), finish_reason="tool_calls")

    assert not empty.has_tool_calls
    assert with_calls.has_tool_calls
    assert with_calls.finish_reason == "tool_calls"


def test_chat_model_is_runtime_protocol() -> None:
    # A structural match must satisfy the protocol without subclassing.
    class Stub:
        async def complete(self, messages, *, tools=(), **params):  # type: ignore[no-untyped-def]
            return ModelResponse(content="ok")

    assert isinstance(Stub(), ChatModel)
    assert not isinstance(object(), ChatModel)
