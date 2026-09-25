"""Terminal output helpers for CLI agent runs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

_NODE_LABELS = {
    "plan": "PLAN",
    "agent": "AGENT",
    "policy": "POLICY",
    "human_input": "HUMAN",
    "executor": "EXECUTOR",
    "observe": "OBSERVE",
}

SNAPSHOT_REDACTION = "[browser_snapshot hidden]"


def _redact_snapshot_value(value: Any, *, parent_key: str = "") -> Any:
    """Return a terminal-safe copy with browser snapshots removed."""

    if parent_key == "snapshot":
        return SNAPSHOT_REDACTION

    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        is_snapshot_result = str(value.get("name", "") or "") == "browser_snapshot"
        has_snapshot_field = bool(str(value.get("snapshot", "") or "").strip())
        for key, item in value.items():
            key_text = str(key)
            if key_text == "snapshot":
                redacted[key] = SNAPSHOT_REDACTION
            elif key_text == "content" and is_snapshot_result:
                redacted[key] = SNAPSHOT_REDACTION
            elif key_text == "observation" and has_snapshot_field:
                redacted[key] = SNAPSHOT_REDACTION
            else:
                redacted[key] = _redact_snapshot_value(item, parent_key=key_text)
        return redacted

    if isinstance(value, list):
        return [_redact_snapshot_value(item, parent_key=parent_key) for item in value]

    return value


def format_state(value: Any, as_json: bool = False) -> str:
    """Format a graph state value for terminal output."""

    if as_json:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return repr(value)


def print_step(
    node_name: str,
    update: Any,
    as_json: bool = False,
    *,
    hide_snapshot: bool = False,
) -> None:
    """Print a compact node update after a graph step."""

    label = _NODE_LABELS.get(node_name, node_name.upper())
    printable_update = _redact_snapshot_value(update) if hide_snapshot else update
    if as_json:
        print(f"[{label}] {format_state(printable_update, as_json=True)}")
        return

    print(f"\n[{label}]")
    if not isinstance(printable_update, dict):
        print(format_state(printable_update))
        return

    for key in (
        "plan",
        "decision",
        "tool_request",
        "policy_decision",
        "tool_result",
        "observation",
        "snapshot",
        "last_tool",
        "last_args",
        "repeat_count",
        "final_answer",
        "error",
    ):
        value = printable_update.get(key)
        if value not in (None, "", [], {}):
            print(f"{key}: {format_state(value)}")


def print_tools(tools: list[Any]) -> None:
    """Print loaded MCP tool names (with the owning MCP server when known)."""

    print("MCP tools:")
    if not tools:
        print("- none")
        return
    for tool in tools:
        name = getattr(tool, "name", tool)
        server = getattr(tool, "server", None)
        print(f"- {name} [{server}]" if server else f"- {name}")


def format_mcp_status(statuses: Mapping[str, Any]) -> list[str]:
    """One line per MCP server: state, generation, last error."""

    lines: list[str] = []
    for name, status in statuses.items():
        state = getattr(getattr(status, "state", None), "value", getattr(status, "state", "?"))
        line = f"{name:<16} {state:<12} gen={getattr(status, 'generation', 0)}"
        if getattr(status, "stateful", False):
            line += " stateful"
        if getattr(status, "catalog_stale", False):
            line += " catalog=stale"
        error = getattr(status, "last_error", None)
        if error:
            line += f" error={error}"
        lines.append(line)
    return lines


def print_final_state(result: Any, as_json: bool) -> None:
    """Print the final graph result for a CLI task."""

    if as_json:
        print(format_state(result, as_json=True))
        return

    if isinstance(result, dict):
        if result.get("final_answer"):
            print(result["final_answer"])
            return
        if result.get("error"):
            print(f"Error: {result['error']}")
            return

    print(format_state(result))


__all__ = [
    "SNAPSHOT_REDACTION",
    "format_mcp_status",
    "format_state",
    "print_final_state",
    "print_step",
    "print_tools",
]