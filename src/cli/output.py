"""Terminal output helpers for CLI agent runs."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

def format_state(value: Any, as_json: bool = False) -> str:
    """Format a graph state value for terminal output."""

    if as_json:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return repr(value)


def print_tools(tools: list[Any], output_fn: Callable[[str], None] = print) -> None:
    """Write loaded MCP tool names (with the owning MCP server when known) to ``output_fn``."""

    output_fn("MCP tools:")
    if not tools:
        output_fn("- none")
        return
    for tool in tools:
        name = getattr(tool, "name", tool)
        server = getattr(tool, "server", None)
        output_fn(f"- {name} [{server}]" if server else f"- {name}")


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
    "format_mcp_status",
    "format_state",
    "print_final_state",
    "print_tools",
]