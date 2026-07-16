"""Harness runtime for compiling and running the AutoBrowser agent loop."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any
from uuid import uuid4

from src.harness.context import ContextBuilder
from src.harness.memory import MemoryManager
from src.harness.policy import PolicyEngine
from src.harness.telemetry import TelemetryObserver
from src.harness.tools import ToolLoader, ToolRegistry

GraphBuilder = Callable[..., Any]


class BrowserHarness:
    """Compose harness infrastructure and inject it into the LangGraph agent loop."""

    def __init__(
        self,
        graph_builder: GraphBuilder,
        *,
        llm: Any | None = None,
        observer_llm: Any | None = None,
        tools: Sequence[Any] | None = None,
        tool_loader: ToolLoader | None = None,
        tool_registry: ToolRegistry | None = None,
        memory_manager: MemoryManager | None = None,
        context_builder: ContextBuilder | None = None,
        telemetry: TelemetryObserver | None = None,
        policy_engine: PolicyEngine | None = None,
        compress_tools: bool = False,
        graph_options: Mapping[str, Any] | None = None,
    ) -> None:
        self.telemetry = telemetry or TelemetryObserver()
        self.memory = memory_manager or MemoryManager()
        self.context = context_builder or ContextBuilder()
        self.tools = tool_registry or ToolRegistry(tools=tools, tool_loader=tool_loader)
        self.policy = policy_engine or PolicyEngine()

        self.graph = graph_builder(
            llm=llm,
            observer_llm=observer_llm,
            tools=tools,
            tool_loader=tool_loader,
            tool_registry=self.tools,
            checkpointer=self.memory.get_checkpoint_saver(),
            compress_tools=compress_tools,
            **dict(graph_options or {}),
        )

    async def run(
        self,
        task: str,
        config: Mapping[str, Any] | None = None,
        *,
        thread_id: str | None = None,
        state_overrides: Mapping[str, Any] | None = None,
    ) -> Any:
        """Run one task through the compiled agent graph."""

        trace = self.telemetry.start_trace(task)
        run_config = self._run_config(config, thread_id=thread_id or f"task-{uuid4().hex}")
        initial_state = self.context.build_initial_state(task, state_overrides)

        try:
            return await self.graph.ainvoke(initial_state, config=run_config)
        except Exception as exc:
            self.telemetry.log_error(exc, metadata={"task_name": trace.task_name})
            raise

    async def stream_updates(
        self,
        task: str,
        config: Mapping[str, Any] | None = None,
        *,
        thread_id: str | None = None,
        state_overrides: Mapping[str, Any] | None = None,
    ) -> Any:
        """Stream graph node updates for one task."""

        trace = self.telemetry.start_trace(task)
        run_config = self._run_config(config, thread_id=thread_id or f"task-{uuid4().hex}")
        initial_state = self.context.build_initial_state(task, state_overrides)

        try:
            async for chunk in self.graph.astream(
                initial_state,
                config=run_config,
                stream_mode="updates",
            ):
                yield chunk
        except Exception as exc:
            self.telemetry.log_error(exc, metadata={"task_name": trace.task_name})
            raise

    def _run_config(
        self,
        config: Mapping[str, Any] | None,
        *,
        thread_id: str,
    ) -> dict[str, Any]:
        run_config = dict(config or {})
        configurable = dict(run_config.get("configurable") or {})
        configurable.setdefault("thread_id", thread_id or f"task-{uuid4().hex}")
        run_config["configurable"] = configurable
        return run_config


__all__ = ["BrowserHarness", "GraphBuilder"]
