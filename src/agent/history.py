"""Compatibility exports for history helpers now owned by the harness."""

from src.harness.memory import (
    append_ai_tool_call,
    append_final_ai_response,
    append_tool_message,
    ensure_message_history,
    tool_result_message_content,
    with_tool_call_id,
)

__all__ = [
    "append_ai_tool_call",
    "append_final_ai_response",
    "append_tool_message",
    "ensure_message_history",
    "tool_result_message_content",
    "with_tool_call_id",
]
