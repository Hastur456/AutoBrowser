"""Engine-native execution loop: ``plan -> agent -> policy -> execute -> observe``.

This is the first **real** engine-native control flow for AutoBrowser — the explicit
rewrite of what the LangGraph graph does today (``START -> plan -> agent -> policy ->
executor -> observe -> agent``), driven by :class:`~src.agent_loop.actions.ProposedAction`
over the typed :class:`~src.agent_loop.execution.state.LoopState` (never ``AgentState``).

Firing order mirrors ``create_agent_node`` (``src/agent/nodes.py``) verbatim so the native
path stays behaviorally aligned with the graph on the deterministic fake-browser scenarios:
terminal guard first, then pending-tab activation, stale-snapshot retry, forced fresh
snapshot, and only then a model turn whose parsed action is classified directly. Blocked
policy short-circuits before execute/observe so ``consecutive_failures`` is counted once per
blocked turn (matching v1's ``policy -> agent`` edge that skips the observer).

Decoupling: this module imports **nothing** from ``src/agent/``. It reuses the already
engine-native model/action layer (:mod:`src.agent_loop.model`, :mod:`src.agent_loop.actions`),
the ported control logic in this package, the neutral contracts in :mod:`src.contracts`, and
harness leaves (message history, tool registry, event metadata keys). The planner prompt is
reached through :meth:`ContextBuilder.build_plan_prompt` — the sanctioned prompt boundary —
rather than importing planner prompts directly.

Event contract: only existing ``EventType`` literals are emitted (``model.requested`` /
``model.responded``, ``action.proposed``, ``policy.decided``, ``approval.requested``,
``tool.started`` / ``tool.finished``, ``observation.compiled``); ``goal.*`` stays owned by
:class:`~src.agent_loop.goals.GoalRunner`. **At least one event is emitted per continuing
turn** so ``GoalRunner._watch_progress`` (which polls ``EventEmitter.sequence``) never
false-times-out. ``tool.finished`` carries ``{"tool_result": dict(result)}`` — the exact
shape v1 emits — so the parity test can read the tool-name sequence identically.

Parity note: v1 emits ``model.responded`` for *both* plan and agent nodes, so the
``langgraph_v1.json`` ``model_turn_count`` includes planner turns. This loop emits one
``model.responded`` per real model call and does **not** target ``model_turn_count`` parity
in this slice; the parity test asserts terminal status + ``final_answer`` + ``tool.finished``
sequence only. A fresh v2 baseline is a later (Commit-8) concern.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage

from src.browser import is_browser_tool_name, to_canonical_browser_name
from src.contracts import PlanStep, ToolRequest, ToolResult
from src.harness.memory import ensure_message_history
from src.harness.runtime import (
    HARNESS_EVENT_METADATA_CONFIG_KEY,
    HARNESS_STATE_OVERRIDES_CONFIG_KEY,
)
from src.harness.tools import tool_name

from src.agent_loop.actions import normalize_tool_request
from src.agent_loop.model import ModelDriver
from src.agent_loop.outcomes import CompletionStatus
from src.agent_loop.execution.guards import (
    blocked_response,
    done_response,
    fresh_snapshot_request,
    pending_tab_activation_request,
    replan_response,
    stale_snapshot_retry_update,
    terminal_guard,
    tool_request_update,
)
from src.agent_loop.execution.observation import compile_observation
from src.agent_loop.execution.policy import classify_tool_request, policy_updates
from src.agent_loop.execution.resources import EngineResources
from src.agent_loop.execution.state import LoopState
from src.agent_loop.execution.tools import ToolBroker

HumanInputCallback = Callable[[ToolRequest, str], Awaitable[bool]]

EVENT_SOURCE = "agent_loop.execution"
DEFAULT_TURN_CAP = 50


@dataclass(frozen=True)
class EngineRunResult:
    """Terminal outcome of one native execution-loop run.

    ``status`` is always terminal (``"done"``/``"blocked"``/``"cancelled"``, never
    ``"continue"``); ``session_state`` is the ``SESSION_STATE_KEYS`` carry-forward produced by
    :meth:`LoopState.to_session_state`; ``state`` is the terminal :class:`LoopState` kept for
    debugging and native latest-state loading.
    """

    status: CompletionStatus
    final_answer: str
    session_state: dict[str, Any]
    state: LoopState
    turns: int = 0


async def _deny_human_input(request: ToolRequest, reason: str) -> bool:
    """Default human-in-the-loop callback for the native path: always deny.

    Mirrors ``human_input_node`` denial semantics for tests/headless runs until interactive
    HITL is wired for the native CLI. Tests inject an approving callback where needed.
    """

    return False


def _json_object(text: str) -> dict[str, Any]:
    """Parse the first ``{...}`` object out of ``text`` (planner JSON extraction).

    Ports ``_json_object`` from ``plan_node``: slice from the first ``{`` to the last ``}``
    and ``json.loads`` it; return ``{}`` on any failure.
    """

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _message_content(response: Any) -> str:
    return str(getattr(response, "content", response))


def _normalize_steps(raw_steps: Any, task: str) -> list[PlanStep]:
    """Normalize planner output into ``PlanStep`` dicts (ports ``plan_node._normalize_steps``)."""

    default: list[PlanStep] = [{"id": 1, "description": task, "status": "pending"}]
    if not isinstance(raw_steps, list):
        return default

    steps: list[PlanStep] = []
    for index, raw_step in enumerate(raw_steps, start=1):
        if isinstance(raw_step, str):
            description = raw_step.strip()
        elif isinstance(raw_step, Mapping):
            description = str(raw_step.get("description", "")).strip()
        else:
            description = ""
        if description:
            steps.append(
                {"id": index, "description": description, "status": "pending"}
            )
    return steps or default


def _is_browser_tabs_tool(name: str) -> bool:
    return is_browser_tool_name(name) and to_canonical_browser_name(name) == "browser_tabs"


class AgentExecutionLoop:
    """Drive one goal natively from a plan through repeated execute/observe turns."""

    def __init__(
        self,
        resources: EngineResources,
        *,
        compress_tools: bool = False,
        human_input: HumanInputCallback | None = None,
        source: str = EVENT_SOURCE,
    ) -> None:
        self._resources = resources
        self._broker = ToolBroker(
            resources.tool_registry,
            browser_providers=list(resources.browser_providers or []),
        )
        self._model_driver = ModelDriver(
            resources.llm,
            tool_registry=resources.tool_registry,
        )
        self._compress_tools = compress_tools
        self._human_input = human_input or _deny_human_input
        self._source = source
        self._tools: list[Any] = []
        self._browser_tabs_available = False
        self._event_ctx: dict[str, Any] = {}

    async def run(
        self,
        task: str,
        *,
        task_id: str = "",
        goal_id: str = "",
        session_id: str | None = None,
        state_overrides: Mapping[str, Any] | None = None,
        recursion_limit: int | None = None,
        event_context: Mapping[str, Any] | None = None,
    ) -> EngineRunResult:
        """Run the loop to a terminal state and return the :class:`EngineRunResult`."""

        ctx = dict(event_context or {})
        self._event_ctx = {
            "session_id": ctx.get("session_id", session_id),
            "task_id": ctx.get("task_id", task_id),
            "goal_id": ctx.get("goal_id", goal_id or task_id),
        }
        turn_cap = max(1, int(recursion_limit or DEFAULT_TURN_CAP))

        self._tools = list(await self._resources.tool_registry.get_all())
        self._browser_tabs_available = any(
            _is_browser_tabs_tool(tool_name(tool)) for tool in self._tools
        )

        state = LoopState.from_state_overrides(state_overrides)
        state = state.apply({"task": task})
        if task_id and not state.task_id:
            state = state.apply({"task_id": task_id})

        # Model call #0: build the initial plan (consumes the planner response).
        state = await self._run_plan(state)

        final_status: CompletionStatus | None = None
        turn = 0
        while True:
            turn += 1
            if turn > turn_cap:
                state = state.apply(
                    blocked_response(
                        state,
                        (
                            f"Blocked: reached the maximum of {turn_cap} agent turns "
                            "without a final answer."
                        ),
                    )
                )
                final_status = "blocked"
                break

            agent_update, status_override = await self._agent_step(state)
            state = state.apply(agent_update)
            decision = str(state.decision or "")

            if decision == "done":
                final_status = status_override or self._status_from_state(state)
                break
            if decision == "replan":
                state = await self._run_plan(state)
                continue
            if decision == "tool_call":
                state, terminal_status = await self._run_tool_turn(state)
                if terminal_status is not None:
                    final_status = terminal_status
                    break
                continue

            # Defensive: an unexpected decision must not spin forever.
            state = state.apply(
                blocked_response(
                    state,
                    f"Blocked: unexpected loop decision '{decision}'.",
                )
            )
            final_status = "blocked"
            break

        return EngineRunResult(
            status=final_status or "blocked",
            final_answer=str(state.final_answer or ""),
            session_state=state.to_session_state(),
            state=state,
            turns=turn,
        )

    async def _run_plan(self, state: LoopState) -> LoopState:
        """Build/replace the plan via a raw planner model call (ports ``plan_node``)."""

        messages = self._history(state)
        prior_replans = int(state.replan_count or 0)
        replan_count = prior_replans + 1 if state.plan else prior_replans

        plan_prompt = self._resources.context.build_plan_prompt(self._plan_mapping(state))
        self._emit("model.requested", {"phase": "plan"})
        response = await self._resources.llm.ainvoke([*messages, HumanMessage(plan_prompt)])
        self._emit("model.responded", {"phase": "plan"})

        data = _json_object(_message_content(response))
        plan = _normalize_steps(data.get("steps"), state.task or "Complete the task.")
        return state.apply(
            {
                "plan": plan,
                "current_step": 0,
                "decision": "replan",
                "replan_count": replan_count,
                "error": "",
                "stale_snapshot_retries": 0,
                "invalid_ref_recovery_count": 0,
                "needs_fresh_snapshot": False,
                "messages": messages,
            }
        )

    async def _agent_step(
        self,
        state: LoopState,
    ) -> tuple[dict[str, Any], CompletionStatus | None]:
        """One agent turn (ports ``create_agent_node``): returns (update, status_override).

        ``status_override`` is non-``None`` only when the model itself terminated the run via
        a ``stop``/``answer`` action, so the loop records the exact terminal status instead of
        inferring it from ``final_answer`` text.
        """

        terminal = terminal_guard(state)
        if terminal is not None:
            return terminal, None

        if not state.plan:
            return replan_response("No plan is available."), None

        messages = self._history(state)

        if self._browser_tabs_available:
            pending_tab = pending_tab_activation_request(state)
            if pending_tab is not None:
                return tool_request_update(state, messages, pending_tab), None

        stale_snapshot_update = stale_snapshot_retry_update(state)
        if stale_snapshot_update.get("decision") == "replan":
            return stale_snapshot_update, None

        if state.browser.needs_fresh_snapshot:
            snapshot_request = fresh_snapshot_request(state, messages)
            snapshot_request.update(stale_snapshot_update)
            return snapshot_request, None

        turn_prompt = self._resources.context.build_turn_prompt(
            self._prompt_mapping(state),
            self._tools,
        )
        self._emit("model.requested", {"phase": "agent", "tool_count": len(self._tools)})
        model_turn = await self._model_driver.invoke(
            [*messages, HumanMessage(turn_prompt)],
            tools=self._tools,
        )
        self._emit("model.responded", {"phase": "agent", "metadata": dict(model_turn.metadata or {})})

        actions = list(model_turn.actions or [])
        if not actions:
            return (
                blocked_response(state, "Blocked: the model returned no actionable step."),
                "blocked",
            )
        return self._classify_action(state, actions[0], messages)

    def _classify_action(
        self,
        state: LoopState,
        action: Mapping[str, Any],
        messages: list[BaseMessage],
    ) -> tuple[dict[str, Any], CompletionStatus | None]:
        """Map a ``ProposedAction`` to a LoopState update (never via legacy adapters)."""

        kind = str(action.get("kind", "") or "")

        if kind == "answer":
            final_answer = str(action.get("final_answer", "") or "").strip()
            return done_response(state, final_answer, messages=messages), "done"

        if kind == "stop":
            status = str(action.get("status", "blocked") or "blocked")
            message = str(action.get("message", "") or "").strip()
            if status == "done":
                return done_response(state, message, messages=messages), "done"
            override: CompletionStatus = "cancelled" if status == "cancelled" else "blocked"
            return (
                blocked_response(state, message or "Blocked: the model stopped the task."),
                override,
            )

        if kind == "tool_call":
            request = normalize_tool_request(action.get("tool_request"))
            return tool_request_update(state, messages, request), None

        if kind == "update_plan":
            reason = str(action.get("reason", "") or "Replanning requested.")
            return replan_response(reason), None

        if kind == "ask_user":
            question = str(action.get("question", "") or "user input").strip()
            return (
                blocked_response(state, f"Blocked: user input required: {question}"),
                "blocked",
            )

        return (
            blocked_response(state, f"Blocked: unsupported action kind '{kind}'."),
            "blocked",
        )

    async def _run_tool_turn(
        self,
        state: LoopState,
    ) -> tuple[LoopState, CompletionStatus | None]:
        """Run policy -> (execute -> observe) for a tool-call decision.

        Returns the new state and a terminal status if the turn ended the run (human denial
        or the unchanged-snapshot observation terminal); ``None`` means continue looping. A
        blocked policy decision short-circuits before execute/observe so
        ``consecutive_failures`` is incremented exactly once (matching v1's policy->agent edge).
        """

        request = dict(state.tool_request or {})
        self._emit("action.proposed", {"tool_request": request})

        decision, reason = classify_tool_request(state, request)
        state = state.apply(policy_updates(state, decision, reason))
        self._emit("policy.decided", dict(state.policy_event or {}))

        if decision == "blocked":
            return state, None

        if decision == "needs_human":
            self._emit("approval.requested", {"tool_request": request, "reason": reason})
            approved = await self._human_input(request, reason)
            if not approved:
                denied = request.get("name") or "the requested tool"
                state = state.apply(
                    blocked_response(
                        state,
                        f"Blocked: human approval was denied for {denied}.",
                    )
                )
                return state, "blocked"

        self._emit("tool.started", {"tool_request": request})
        result: ToolResult = await self._broker.execute(request, state.snapshot_mapping())
        self._emit("tool.finished", {"tool_result": dict(result)})

        state = state.apply({"tool_result": result})
        observation_update = compile_observation(
            state,
            compress_tool_output=self._compress_tools,
        )
        state = state.apply(observation_update)
        self._emit(
            "observation.compiled",
            {"observation": state.observation, "has_snapshot": bool(state.browser.snapshot)},
        )

        if str(state.decision or "") == "done":
            return state, self._status_from_state(state)
        return state, None

    def _history(self, state: LoopState) -> list[BaseMessage]:
        """Seed durable message history with the real system prompt (as ``BrowserHarness`` does).

        Copies ``state.messages`` before handing it to ``ensure_message_history`` so the
        frozen dataclass's list is never mutated in place.
        """

        return ensure_message_history(
            {
                "messages": list(state.messages),
                "task": state.task,
                "task_id": state.task_id,
            },
            system_prompt=self._resources.context.get_system_prompt(),
        )

    def _prompt_mapping(self, state: LoopState) -> dict[str, Any]:
        mapping = dict(state.snapshot_mapping())
        mapping.update(
            {
                "task": state.task,
                "task_id": state.task_id,
                "plan": list(state.plan),
                "current_step": state.current_step,
                "observation": state.observation,
                "consecutive_failures": state.consecutive_failures,
                "repeat_count": state.repeat_count,
                "replan_count": state.replan_count,
                "messages": list(state.messages),
                "decision": state.decision,
                "tool_request": dict(state.tool_request),
                "final_answer": state.final_answer,
            }
        )
        return mapping

    def _plan_mapping(self, state: LoopState) -> dict[str, Any]:
        return {"task": state.task, "observation": state.observation}

    @staticmethod
    def _status_from_state(state: LoopState) -> CompletionStatus:
        """Derive a terminal status from LoopState (ports ``_completion_status_from_agent_state``)."""

        final_answer = str(state.final_answer or "").strip()
        if not final_answer:
            return "continue"
        if final_answer.lower().startswith("blocked:"):
            return "blocked"
        if str(state.decision or "").strip().lower() == "blocked":
            return "blocked"
        return "done"

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        self._resources.events.emit(
            event_type,
            source=self._source,
            payload=dict(payload),
            session_id=self._event_ctx.get("session_id"),
            goal_id=self._event_ctx.get("goal_id"),
            task_id=self._event_ctx.get("task_id"),
        )


def native_task_runner(
    resources: EngineResources,
    *,
    human_input: HumanInputCallback | None = None,
) -> Callable[[Any, str, Any, dict[str, Any]], Awaitable[EngineRunResult]]:
    """Build a ``TaskRunner`` (see :data:`src.agent_loop.goals.TaskRunner`) over ``resources``.

    The returned coroutine matches ``(harness, task, session_config, task_config)`` and returns
    an :class:`EngineRunResult`. It reads the carried state overrides and event metadata the
    harness stamped onto ``task_config`` and threads them into the loop so emitted events carry
    the right ``session_id``/``task_id``/``goal_id`` and the ``GoalRunner`` watchdog observes
    progress on the shared ``EventEmitter``.
    """

    async def _run(
        harness: Any,
        task: str,
        session_config: Any,
        task_config: dict[str, Any],
    ) -> EngineRunResult:
        state_overrides = task_config.get(HARNESS_STATE_OVERRIDES_CONFIG_KEY) or {}
        event_metadata = dict(task_config.get(HARNESS_EVENT_METADATA_CONFIG_KEY) or {})
        recursion_limit = int(
            task_config.get("recursion_limit")
            or getattr(session_config, "recursion_limit", DEFAULT_TURN_CAP)
            or DEFAULT_TURN_CAP
        )
        compress_tools = bool(getattr(session_config, "compress_tools", False))

        loop = AgentExecutionLoop(
            resources,
            compress_tools=compress_tools,
            human_input=human_input,
        )
        return await loop.run(
            task,
            task_id=str(event_metadata.get("task_id", "")),
            goal_id=str(event_metadata.get("goal_id", "")),
            session_id=event_metadata.get("session_id"),
            state_overrides=state_overrides,
            recursion_limit=recursion_limit,
            event_context=event_metadata,
        )

    return _run


__all__ = [
    "AgentExecutionLoop",
    "EngineRunResult",
    "HumanInputCallback",
    "native_task_runner",
]
