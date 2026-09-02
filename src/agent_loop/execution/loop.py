"""Engine-native execution loop: ``plan -> agent -> policy -> execute -> observe``.

This is the **real** engine-native control flow for AutoBrowser — the explicit loop
(``START -> plan -> agent -> policy -> executor -> observe -> agent``), driven by
:class:`~src.agent_loop.actions.ProposedAction` over the typed
:class:`~src.agent_loop.execution.state.LoopState`.

Ownership is split so the engine is a control-flow owner, not a wrapper:

- :class:`AgentLoopEngine` owns the explicit ``while`` turn loop, the turn cap, plan
  (re)building, and the terminal :class:`AgentLoopResult`.
- :class:`TurnController` owns exactly one turn — the ``agent`` step (terminal guard,
  pending-tab activation, stale-snapshot retry, forced fresh snapshot, then a model turn whose
  action is classified) and, for a tool-call decision, the ``policy -> execute -> observe``
  sub-turn — returning a structured :class:`TurnResult` the engine acts on.
- Completion decisions live in :class:`~src.agent_loop.execution.guards.CompletionController`
  and observation building in :class:`~src.agent_loop.execution.observation.ObservationCompiler`.

Firing order mirrors the legacy ``create_agent_node`` (now removed) so the native path stays
behaviorally aligned on the deterministic fake-browser scenarios.
Blocked policy short-circuits before execute/observe so ``consecutive_failures`` is counted
once per blocked turn (matching v1's ``policy -> agent`` edge that skips the observer).

Decoupling: this module imports **nothing** from ``src/agent/``. It reuses the already
engine-native model/action layer (:mod:`src.agent_loop.model`, :mod:`src.agent_loop.actions`),
the ported control logic in this package, the neutral contracts in :mod:`src.contracts`, and
harness leaves (message history, tool registry, event metadata keys). The planner prompt is
reached through :meth:`ContextBuilder.build_plan_prompt` — the sanctioned prompt boundary.

Event contract: only existing ``EventType`` literals are emitted (``model.requested`` /
``model.responded``, ``action.proposed``, ``policy.decided``, ``approval.requested``,
``tool.started`` / ``tool.finished``, ``observation.compiled``); ``goal.*`` stays owned by
:class:`~src.agent_loop.goals.GoalRunner`. **At least one event is emitted per continuing
turn** so ``GoalRunner._watch_progress`` (which polls ``EventEmitter.sequence``) never
false-times-out. ``tool.finished`` carries ``{"tool_result": dict(result)}`` — the exact
shape v1 emits — so the parity test can read the tool-name sequence identically.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from src.browser import is_browser_tool_name, to_canonical_browser_name
from src.contracts import PlanStep, ToolRequest, ToolResult
from src.harness.memory import ensure_message_history
from src.harness.runtime import (
    HARNESS_EVENT_METADATA_CONFIG_KEY,
    HARNESS_STATE_OVERRIDES_CONFIG_KEY,
)
from src.harness.tools import tool_name
from src.messages import Message, user_message

from src.agent_loop.actions import normalize_tool_request
from src.agent_loop.model import ModelDriver
from src.agent_loop.outcomes import CompletionStatus
from src.agent_loop.execution.guards import (
    CompletionController,
    blocked_response,
    done_response,
    fresh_snapshot_request,
    pending_tab_activation_request,
    replan_response,
    stale_snapshot_retry_update,
    tool_request_update,
)
from src.agent_loop.execution.observation import ObservationCompiler
from src.agent_loop.execution.policy import classify_tool_request, policy_updates
from src.agent_loop.execution.resources import EngineResources
from src.agent_loop.execution.state import LoopState
from src.agent_loop.execution.tools import ToolBroker

HumanInputCallback = Callable[[ToolRequest, str], Awaitable[bool]]

EVENT_SOURCE = "agent_loop.execution"
DEFAULT_TURN_CAP = 50


@dataclass(frozen=True)
class AgentLoopResult:
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


@dataclass(frozen=True)
class TurnResult:
    """Outcome of a single :meth:`TurnController.run_turn`.

    ``status`` non-``None`` means the turn ended the run with that terminal status;
    ``replan`` means the engine should (re)build the plan and keep looping; otherwise the
    engine simply continues with the returned ``state``.
    """

    state: LoopState
    status: CompletionStatus | None = None
    replan: bool = False


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


def _build_history(resources: EngineResources, state: LoopState) -> list[Message]:
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
        system_prompt=resources.context.get_system_prompt(),
    )


def _emit_event(
    resources: EngineResources,
    *,
    source: str,
    event_ctx: Mapping[str, Any],
    event_type: str,
    payload: Mapping[str, Any],
) -> None:
    resources.events.emit(
        event_type,
        source=source,
        payload=dict(payload),
        session_id=event_ctx.get("session_id"),
        goal_id=event_ctx.get("goal_id"),
        task_id=event_ctx.get("task_id"),
    )


class TurnController:
    """Drive exactly one agent turn (and its policy -> execute -> observe sub-turn)."""

    def __init__(
        self,
        resources: EngineResources,
        *,
        tools: list[Any],
        browser_tabs_available: bool,
        event_ctx: Mapping[str, Any],
        completion: CompletionController,
        compress_tools: bool = False,
        human_input: HumanInputCallback | None = None,
        source: str = EVENT_SOURCE,
    ) -> None:
        self._resources = resources
        self._tools = tools
        self._browser_tabs_available = browser_tabs_available
        self._event_ctx = dict(event_ctx)
        self._completion = completion
        self._compress_tools = compress_tools
        self._human_input = human_input or _deny_human_input
        self._source = source
        self._broker = ToolBroker(
            resources.tool_registry,
            browser_providers=list(resources.browser_providers or []),
        )
        self._model_driver = ModelDriver(
            resources.llm,
            tool_registry=resources.tool_registry,
        )
        self._observation = ObservationCompiler(completion=completion)

    async def run_turn(self, state: LoopState) -> TurnResult:
        """Run one turn and describe what the engine should do next."""

        state = state.apply(await self._agent_step(state))
        decision = str(state.decision or "")

        if decision == "done":
            # A ``done`` decision is terminal by construction; an empty final answer would
            # otherwise derive ``"continue"`` (the legacy status contract), so coerce it.
            status = self._completion.status_from_state(state)
            return TurnResult(state=state, status="done" if status == "continue" else status)
        if decision == "replan":
            return TurnResult(state=state, replan=True)
        if decision == "tool_call":
            state, terminal_status = await self._run_tool_turn(state)
            return TurnResult(state=state, status=terminal_status)

        # Defensive: an unexpected decision must not spin forever.
        state = state.apply(
            blocked_response(state, f"Blocked: unexpected loop decision '{decision}'.")
        )
        return TurnResult(state=state, status="blocked")

    async def _agent_step(self, state: LoopState) -> dict[str, Any]:
        """One agent turn (ports ``create_agent_node``): return the flat ``LoopState`` update.

        Terminal status is **not** decided here — the engine derives it from the resulting
        ``LoopState`` via :meth:`CompletionController.status_from_state`, exactly as the legacy
        graph derived completion from the terminal ``AgentState`` rather than from the node.
        """

        terminal = self._completion.pre_turn_terminal(state)
        if terminal is not None:
            return terminal

        if not state.plan:
            return replan_response("No plan is available.")

        messages = self._history(state)

        if self._browser_tabs_available:
            pending_tab = pending_tab_activation_request(state)
            if pending_tab is not None:
                return tool_request_update(state, messages, pending_tab)

        stale_snapshot_update = stale_snapshot_retry_update(state)
        if stale_snapshot_update.get("decision") == "replan":
            return stale_snapshot_update

        if state.browser.needs_fresh_snapshot:
            snapshot_request = fresh_snapshot_request(state, messages)
            snapshot_request.update(stale_snapshot_update)
            return snapshot_request

        turn_prompt = self._resources.context.build_turn_prompt(
            self._prompt_mapping(state),
            self._tools,
        )
        self._emit("model.requested", {"phase": "agent", "tool_count": len(self._tools)})
        model_turn = await self._model_driver.invoke(
            [*messages, user_message(turn_prompt)],
            tools=self._tools,
        )
        self._emit(
            "model.responded",
            {"phase": "agent", "metadata": dict(model_turn.metadata or {})},
        )

        actions = list(model_turn.actions or [])
        if not actions:
            # ActionParser always returns >=1 action; this is a defensive terminal only.
            return blocked_response(state, "Blocked: the model returned no actionable step.")
        return self._classify_action(state, actions[0], messages)

    def _classify_action(
        self,
        state: LoopState,
        action: Mapping[str, Any],
        messages: list[Message],
    ) -> dict[str, Any]:
        """Map a ``ProposedAction`` to a flat ``LoopState`` update.

        Reproduces the behavior of the removed ``proposed_action_to_legacy_update`` adapter
        natively so the engine no longer imports from ``src/agent/``: ``answer`` and a ``done``
        ``stop`` finish; every other ``stop`` finishes with a status-prefixed final answer
        whose terminal status ``status_from_state`` derives; ``tool_call`` proposes a tool; and
        ``update_plan``/``ask_user``/``delegate``/``compact_memory`` (and any unrecognized
        kind) fall back to a replan. The ``reason``/``action`` metadata the adapter appended is
        intentionally dropped — ``LoopState.apply`` rejects unknown keys and nothing read them.
        """

        kind = str(action.get("kind", "") or "")

        if kind == "answer":
            return done_response(
                state,
                str(action.get("final_answer", "") or ""),
                messages=messages,
            )

        if kind == "tool_call":
            request = normalize_tool_request(action.get("tool_request"))
            return tool_request_update(state, messages, request)

        if kind == "update_plan":
            return replan_response(str(action.get("reason", "") or "Replanning requested."))

        if kind == "ask_user":
            question = str(action.get("question", "") or action.get("reason", "") or "")
            return replan_response(question or "Human input requested.")

        if kind == "delegate":
            objective = str(action.get("objective", "") or "")
            role = str(action.get("role", "") or "")
            reason = str(action.get("reason", "") or "").strip()
            return replan_response(
                reason
                or f"Delegation requested for {role or 'another agent'}: {objective}".strip()
            )

        if kind == "compact_memory":
            summary = str(action.get("summary", "") or "")
            return replan_response(
                str(action.get("reason", "") or summary or "Memory compaction requested.")
            )

        if kind == "stop":
            message = str(action.get("message", "") or "")
            status = str(action.get("status", "") or "blocked")
            if status == "done":
                return done_response(state, message, messages=messages)
            if status == "cancelled":
                return done_response(
                    state,
                    f"Cancelled: {message}" if message else "Cancelled.",
                    messages=messages,
                )
            if status == "failed":
                return done_response(
                    state,
                    f"Failed: {message}" if message else "Failed.",
                    messages=messages,
                )
            return done_response(
                state,
                f"Blocked: {message}" if message else "Blocked.",
                messages=messages,
            )

        return replan_response("Unrecognized proposed action.")

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
        observation_update = self._observation.compile(
            state,
            compress_tool_output=self._compress_tools,
        )
        state = state.apply(observation_update)
        self._emit(
            "observation.compiled",
            {"observation": state.observation, "has_snapshot": bool(state.browser.snapshot)},
        )

        if str(state.decision or "") == "done":
            return state, self._completion.status_from_state(state)
        return state, None

    def _history(self, state: LoopState) -> list[Message]:
        return _build_history(self._resources, state)

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

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        _emit_event(
            self._resources,
            source=self._source,
            event_ctx=self._event_ctx,
            event_type=event_type,
            payload=payload,
        )


class AgentLoopEngine:
    """Own the explicit ``plan -> (turn)*`` control flow for one native goal run.

    This is the real control-flow owner (not a wrapper): it builds the initial plan, then
    drives a bounded ``while`` loop of :class:`TurnController` turns, (re)planning or
    terminating as each :class:`TurnResult` directs, and returns the terminal
    :class:`AgentLoopResult`. Lifecycle concerns (goal events, watchdog, timeouts) stay in the
    enclosing :class:`~src.agent_loop.goals.GoalRunner`, which drives this engine through
    :func:`native_task_runner`.
    """

    def __init__(
        self,
        resources: EngineResources,
        *,
        compress_tools: bool = False,
        human_input: HumanInputCallback | None = None,
        source: str = EVENT_SOURCE,
    ) -> None:
        self._resources = resources
        self._compress_tools = compress_tools
        self._human_input = human_input or _deny_human_input
        self._source = source
        self._completion = CompletionController()
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
    ) -> AgentLoopResult:
        """Run the loop to a terminal state and return the :class:`AgentLoopResult`."""

        ctx = dict(event_context or {})
        self._event_ctx = {
            "session_id": ctx.get("session_id", session_id),
            "task_id": ctx.get("task_id", task_id),
            "goal_id": ctx.get("goal_id", goal_id or task_id),
        }
        turn_cap = max(1, int(recursion_limit or DEFAULT_TURN_CAP))

        tools = list(await self._resources.tool_registry.get_all())
        browser_tabs_available = any(
            _is_browser_tabs_tool(tool_name(tool)) for tool in tools
        )
        turn_controller = TurnController(
            self._resources,
            tools=tools,
            browser_tabs_available=browser_tabs_available,
            event_ctx=self._event_ctx,
            completion=self._completion,
            compress_tools=self._compress_tools,
            human_input=self._human_input,
            source=self._source,
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

            result = await turn_controller.run_turn(state)
            state = result.state
            if result.status is not None:
                final_status = result.status
                break
            if result.replan:
                state = await self._run_plan(state)
                continue

        return AgentLoopResult(
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
        response = await self._resources.llm.complete([*messages, user_message(plan_prompt)])
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

    def _history(self, state: LoopState) -> list[Message]:
        return _build_history(self._resources, state)

    def _plan_mapping(self, state: LoopState) -> dict[str, Any]:
        return {"task": state.task, "observation": state.observation}

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        _emit_event(
            self._resources,
            source=self._source,
            event_ctx=self._event_ctx,
            event_type=event_type,
            payload=payload,
        )


def native_task_runner(
    resources: EngineResources,
    *,
    human_input: HumanInputCallback | None = None,
) -> Callable[[Any, str, Any, dict[str, Any]], Awaitable[AgentLoopResult]]:
    """Build a ``TaskRunner`` (see :data:`src.agent_loop.goals.TaskRunner`) over ``resources``.

    The returned coroutine matches ``(harness, task, session_config, task_config)`` and returns
    an :class:`AgentLoopResult`. It reads the carried state overrides and event metadata the
    harness stamped onto ``task_config`` and threads them into the engine so emitted events
    carry the right ``session_id``/``task_id``/``goal_id`` and the ``GoalRunner`` watchdog
    observes progress on the shared ``EventEmitter``.
    """

    async def _run(
        harness: Any,
        task: str,
        session_config: Any,
        task_config: dict[str, Any],
    ) -> AgentLoopResult:
        state_overrides = task_config.get(HARNESS_STATE_OVERRIDES_CONFIG_KEY) or {}
        event_metadata = dict(task_config.get(HARNESS_EVENT_METADATA_CONFIG_KEY) or {})
        recursion_limit = int(
            task_config.get("recursion_limit")
            or getattr(session_config, "recursion_limit", DEFAULT_TURN_CAP)
            or DEFAULT_TURN_CAP
        )
        compress_tools = bool(getattr(session_config, "compress_tools", False))

        engine = AgentLoopEngine(
            resources,
            compress_tools=compress_tools,
            human_input=human_input,
        )
        return await engine.run(
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
    "DEFAULT_TURN_CAP",
    "EVENT_SOURCE",
    "AgentLoopEngine",
    "AgentLoopResult",
    "HumanInputCallback",
    "TurnController",
    "TurnResult",
    "native_task_runner",
]
