"""Stateless LLM compression for Playwright MCP tool results."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from .prompts import OBSERVER_SYSTEM_PROMPT
from src.agent.state import CompactToolObservation, ToolResult

MAX_OBSERVER_FALLBACK_CHARS = 1200

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


def _compact_text(value: Any, limit: int = MAX_OBSERVER_FALLBACK_CHARS) -> str:
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


def _normalize_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_compact_observation(value: Any) -> CompactToolObservation:
    """Normalize arbitrary LLM JSON into the compact observation contract."""

    data = value if isinstance(value, dict) else {}
    return {
        "summary": _compact_text(data.get("summary"), 400),
        "visible_state": _compact_text(data.get("visible_state"), 900),
        "important_refs": _normalize_list(data.get("important_refs")),
        "errors": _normalize_list(data.get("errors")),
        "next_observation_hint": _compact_text(data.get("next_observation_hint"), 300),
    }


def fallback_compact_observation(result: ToolResult) -> CompactToolObservation:
    """Create a deterministic compact observation when no observer LLM is usable."""

    tool_name = str(result.get("name", "tool") or "tool").strip()
    status = str(result.get("status", "error") or "error")
    content = str(result.get("content", "") or "")
    error = str(result.get("error", "") or "")
    payload = content if content else error
    errors = [error] if error else []
    return {
        "summary": _compact_text(f"{tool_name} returned {status}: {payload}", 400),
        "visible_state": _compact_text(payload or "No tool output."),
        "important_refs": [],
        "errors": errors,
        "next_observation_hint": "",
    }


async def compress_tool_result(
    result: ToolResult,
    observer_llm: Any | None,
) -> CompactToolObservation:
    """Compress one ToolResult without reading or receiving agent state."""

    if observer_llm is None or not hasattr(observer_llm, "ainvoke"):
        return fallback_compact_observation(result)

    try:
        response = await observer_llm.ainvoke(
            [
                SystemMessage(content=OBSERVER_SYSTEM_PROMPT),
                HumanMessage(
                    content=json.dumps(
                        {
                            "name": result.get("name", ""),
                            "status": result.get("status", "error"),
                            "content": result.get("content", ""),
                            "error": result.get("error", ""),
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                ),
            ]
        )
    except Exception:
        return fallback_compact_observation(result)

    compact = normalize_compact_observation(_json_object(_message_content(response)))
    if not any(
        compact.get(key)
        for key in ("summary", "visible_state", "important_refs", "errors")
    ):
        return fallback_compact_observation(result)
    return compact
