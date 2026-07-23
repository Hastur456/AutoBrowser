"""AutoBrowser agent exports."""

from __future__ import annotations

from typing import Any

__all__ = ["AgentWorkflow", "build_agent_graph", "create_default_llm"]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from src.agent.agent import AgentWorkflow, build_agent_graph, create_default_llm

    exports = {
        "AgentWorkflow": AgentWorkflow,
        "build_agent_graph": build_agent_graph,
        "create_default_llm": create_default_llm,
    }
    return exports[name]
