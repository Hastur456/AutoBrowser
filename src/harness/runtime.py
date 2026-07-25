"""Harness runtime for compiling and running the AutoBrowser agent loop."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any
from uuid import uuid4

from langgraph.errors import GraphRecursionError

from src.harness.context import ContextBuilder
from src.harness.memory import MemoryManager, ensure_message_history
from src.harness.policy import PolicyEngine
from src.harness.telemetry import TelemetryObserver
from src.harness.tools import ToolLoader, ToolRegistry

GraphBuilder = Callable[..., Any]
HARNESS_STATE_OVERRIDES_CONFIG_KEY = "_autobrowser_state_overrides"


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
            tool_registry=self.tools,
            policy_node=self.policy.node,
            history_builder=self._message_history,
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
        run_config, initial_state = self._prepare_invocation(
            task,
            config,
            thread_id=thread_id,
            state_overrides=state_overrides,
        )

        try:
            return await self.graph.ainvoke(initial_state, config=run_config)
        except GraphRecursionError as exc:
            completed_state = await self._completed_state_after_recursion_limit(run_config)
            if completed_state is not None:
                return completed_state
            self.telemetry.log_error(exc, metadata={"task_name": trace.task_name})
            raise
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
        run_config, initial_state = self._prepare_invocation(
            task,
            config,
            thread_id=thread_id,
            state_overrides=state_overrides,
        )

        yielded_done = False
        try:
            async for chunk in self.graph.astream(
                initial_state,
                config=run_config,
                stream_mode="updates",
            ):
                yielded_done = yielded_done or _chunk_contains_done(chunk)
                yield chunk
        except GraphRecursionError as exc:
            completed_state = await self._completed_state_after_recursion_limit(run_config)
            if completed_state is not None:
                if not yielded_done:
                    yield {"agent": completed_state}
                return
            self.telemetry.log_error(exc, metadata={"task_name": trace.task_name})
            raise
        except Exception as exc:
            self.telemetry.log_error(exc, metadata={"task_name": trace.task_name})
            raise

    async def get_state_values(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        thread_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the latest checkpoint values for a graph thread when available."""

        if not hasattr(self.graph, "aget_state"):
            return None

        public_config, _state_overrides = _split_internal_config(config)
        run_config = self._run_config(
            public_config,
            thread_id=thread_id or f"task-{uuid4().hex}",
        )
        snapshot = await self.graph.aget_state(run_config)
        values = getattr(snapshot, "values", None)
        return dict(values) if isinstance(values, Mapping) else None

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

    def _prepare_invocation(
        self,
        task: str,
        config: Mapping[str, Any] | None,
        *,
        thread_id: str | None,
        state_overrides: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        public_config, config_state_overrides = _split_internal_config(config)
        run_config = self._run_config(
            public_config,
            thread_id=thread_id or f"task-{uuid4().hex}",
        )
        merged_state_overrides: dict[str, Any] = {}
        if config_state_overrides:
            merged_state_overrides.update(config_state_overrides)
        if state_overrides:
            merged_state_overrides.update(state_overrides)
        initial_state = self.context.build_initial_state(task, merged_state_overrides)
        return run_config, initial_state

    def _message_history(self, state: Mapping[str, Any]) -> list[Any]:
        """Build message history with the harness-owned system prompt."""

        return ensure_message_history(
            state,
            system_prompt=self.context.get_system_prompt(),
        )

    async def _completed_state_after_recursion_limit(
        self,
        run_config: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Return the latest checkpoint if it already contains a final answer."""

        if not hasattr(self.graph, "aget_state"):
            return None

        snapshot = await self.graph.aget_state(run_config)
        values = getattr(snapshot, "values", None)
        if not isinstance(values, dict):
            return None

        if values.get("decision") == "done" and values.get("final_answer"):
            return values
        return None


def _chunk_contains_done(chunk: Any) -> bool:
    if not isinstance(chunk, Mapping):
        return False
    for update in chunk.values():
        if isinstance(update, Mapping) and update.get("decision") == "done":
            return True
    return False


def _split_internal_config(
    config: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    run_config = dict(config or {})
    raw_state_overrides = run_config.pop(HARNESS_STATE_OVERRIDES_CONFIG_KEY, None)
    state_overrides = (
        dict(raw_state_overrides)
        if isinstance(raw_state_overrides, Mapping)
        else {}
    )
    return run_config, state_overrides


__all__ = [
    "BrowserHarness",
    "GraphBuilder",
    "HARNESS_STATE_OVERRIDES_CONFIG_KEY",
]
