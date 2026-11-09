"""Engine-native execution loop: ``plan -> agent -> policy -> execute -> observe``.

This is the **real** engine-native control flow for AutoBrowser — the explicit loop
(``START -> plan -> agent -> policy -> executor -> observe -> agent``), driven by
:class:`~src.agent_loop.actions.ProposedAction` over the typed
:class:`~src.agent_loop.execution.state.LoopState`.

Ownership is split so the engine is a control-flow owner, not a wrapper:

- :class:`AgentLoopEngine` owns the explicit ``while`` turn loop, the turn cap, plan
  (re)building, and the terminal :class:`AgentLoopResult`.
- :class:`TurnController` owns exactly one turn — the ``agent`` step (terminal guard,
  then a model turn whose action is classified) and, for a tool-call
  decision, the ``policy -> execute -> observe`` sub-turn — returning a structured :class:`TurnResult` the engine acts on.
- Completion decisions live in :class:`~src.agent_loop.execution.guards.CompletionController`
  and observation building in :class:`~src.agent_loop.execution.observation.ObservationCompiler`.

Firing order mirrors the legacy ``create_agent_node`` (now removed) so the native path stays
behaviorally aligned on the deterministic fake-browser scenarios.
Blocked policy short-circuits before execute/observe so ``consecutive_failures`` is counted
once per blocked turn (matching v1's ``policy -> agent`` edge that skips the observer).

Decoupling: this module imports **nothing** from ``src/agent/``. It reuses the already
engine-native model/action layer (:mod:`src.agent_loop.model`, :mod:`src.agent_loop.actions`),
the ported control logic in this package, the neutral contracts in :mod:`src.contracts`, and
harness leaves (message history, tool registry). The planner prompt is
reached through :meth:`ContextAssembler.plan_prompt` — the sanctioned prompt boundary.

Event contract: only existing ``EventType`` literals are emitted (``model.requested`` /
``model.responded``, ``action.proposed``, ``policy.decided`` (progress guard only),
``permission.decided``, ``approval.requested`` / ``approval.resolved``, ``tool.started`` /
``tool.finished``, ``observation.compiled``, and one ``hook.decided`` per lifecycle-hook
handler run; a playback run adds ``playback.started``/``playback.step``/``playback.diverged``
and ``ladder.escalated``); ``goal.*`` stays owned by
:class:`~src.agent_loop.goals.GoalRunner`. **At least one event is emitted per continuing
turn** so ``GoalRunner._watch_progress`` (which polls ``EventEmitter.sequence``) never
false-times-out. ``tool.finished`` carries ``{"tool_result": dict(result)}`` — the exact
shape v1 emits — so the parity test can read the tool-name sequence identically.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from src.agent_loop.actions import normalize_tool_request
from src.agent_loop.execution.approval import (
    offer_approval_argument,
    split_approval_request,
)
from src.agent_loop.execution.guards import (
    NO_ACTION_SIGNAL,
    CompletionController,
    blocked_response,
    done_response,
    escalation_possible,
    progress_block_reason,
    replan_response,
    tool_block_updates,
    tool_gate_updates,
    tool_request_update,
    tier_escalation,
)
from src.agent_loop.execution.notes import offer_notes_argument, split_notes
from src.agent_loop.execution.observation import ObservationCompiler
from src.agent_loop.execution.progress import render_action_history
from src.agent_loop.execution.resources import EngineResources
from src.agent_loop.execution.state import LoopState, TierMarks
from src.agent_loop.execution.tools import PreparedToolCall, ToolBroker, tool_catalog
from src.agent_loop.execution.usage import add_call, budget_exhausted, effective_budget
from src.agent_loop.model import ModelDriver, response_usage
from src.config import get_settings
from src.contracts import (
    ApprovalAnswer,
    CompletionStatus,
    HookEvent,
    HookEventName,
    LadderStage,
    PermissionCheck,
    PermissionVerdict,
    PlanStep,
    RunBudget,
    RunProfile,
    StepFinal,
    StepHandoff,
    StepProposal,
    StepSource,
    StepView,
    TaskRequest,
    TaskUsage,
    ToolRequest,
    ToolResult,
    ladder_rank,
)
from src.harness.hooks import HookDecisionRecord, HookOutcome
from src.harness.memory import ensure_message_history
from src.harness.tools import tool_is_read_only
from src.llm import ModelTiers
from src.messages import Message, user_message

#: Asked when a call needs approval: ``(request, reason, verdict) -> once | session | deny``.
#: ``True``/``False`` are accepted as ``once``/``deny``.
HumanInputCallback = Callable[
    [ToolRequest, str, PermissionVerdict], Awaitable["ApprovalAnswer | bool"]
]

EVENT_SOURCE = "agent_loop.execution"


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
    #: Machine-readable terminal reason (a ``StopReason``); empty only for foreign results.
    stop_reason: str = ""
    #: The task's model accounting (calls, tokens, cost, ladder stage); ``None`` only for
    #: foreign results.
    usage: TaskUsage | None = None


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


async def _deny_human_input(
    request: ToolRequest,
    reason: str,
    verdict: PermissionVerdict,
) -> ApprovalAnswer:
    """Default human-in-the-loop callback for the native path: always deny.

    Used by headless runs and tests that inject no callback; tests inject an approving one
    where needed.
    """

    return "deny"


def _approval_answer(value: Any) -> ApprovalAnswer:
    """Normalize a callback answer: ``True`` -> ``once``; anything unknown -> ``deny``."""

    if value is True:
        return "once"
    if value in ("once", "session", "deny"):
        return value
    return "deny"


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


def tier_fields(tiers: ModelTiers, index: int) -> dict[str, Any]:
    """``{"tier", "model"}`` for ``model.*`` payloads; empty with one tier, so a run without a
    cascade emits exactly the payloads it did before."""

    if len(tiers.tiers) < 2:
        return {}
    return {"tier": index, "model": tiers.tier(index).label}


def _empty_response(response: Any) -> bool:
    """No text and no tool call: the model gave no action at all (name-free)."""

    return not getattr(response, "tool_calls", None) and not _message_content(response).strip()


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


def _hook_event(
    name: HookEventName,
    state: LoopState,
    event_ctx: Mapping[str, Any],
    **fields: Any,
) -> HookEvent:
    """Build the neutral :class:`HookEvent` for ``name``; ``args``/``result`` are deep copies."""

    for key in ("args", "result"):
        if key in fields:
            fields[key] = copy.deepcopy(dict(fields[key] or {}))
    return HookEvent(
        name=name,
        session_id=event_ctx.get("session_id"),
        goal_id=str(event_ctx.get("goal_id") or ""),
        task_id=str(state.task_id or event_ctx.get("task_id") or ""),
        task=state.task,
        **fields,
    )


def _hook_record_payload(record: HookDecisionRecord, event: HookEvent) -> dict[str, Any]:
    """``hook.decided`` payload: the decision only — never the tool arguments or result."""

    return {
        "hook_id": record.hook_id,
        "event": record.event,
        "tool": event.tool,
        "server": event.server,
        "decision": record.decision,
        "reason": record.reason,
        "modified": record.modified,
        "duration_ms": record.duration_ms,
        "error": record.error,
        "skipped": record.skipped,
    }


def _permission_payload(
    verdict: PermissionVerdict,
    prepared: PreparedToolCall,
    mode: str,
) -> dict[str, Any]:
    """``permission.decided`` payload: the decision only -- never the tool arguments."""

    return {
        "tool": str(prepared.request.get("name", "") or ""),
        "server": prepared.server,
        "decision": verdict.decision,
        "source": verdict.source,
        "rule_id": verdict.rule_id,
        "mode": mode,
        "reason": verdict.reason,
    }


def _harness_note(texts: list[str]) -> Message | None:
    """One ``[harness]`` user message carrying hook context, kept apart from tool output."""

    body = "\n\n".join(text for text in texts if text)
    return user_message(f"[harness] {body}") if body else None


#: Ladder stages in which a step source, not the model, proposes the next step.
SOURCE_STAGES = frozenset({"playback", "retarget"})


def _reached(usage: TaskUsage, stage: str) -> TaskUsage:
    """``usage`` with ``stage_reached`` raised to ``stage`` (never lowered)."""

    if ladder_rank(stage) > ladder_rank(usage.stage_reached):
        return dataclasses.replace(usage, stage_reached=stage)  # type: ignore[arg-type]
    return usage


class TurnController:
    """Drive exactly one agent turn (and its policy -> execute -> observe sub-turn)."""

    def __init__(
        self,
        resources: EngineResources,
        *,
        tools: list[Any],
        event_ctx: Mapping[str, Any],
        completion: CompletionController,
        compress_tools: bool = False,
        human_input: HumanInputCallback | None = None,
        source: str = EVENT_SOURCE,
        step_source: StepSource | None = None,
        profile: RunProfile | None = None,
        budget: RunBudget | None = None,
    ) -> None:
        self._resources = resources
        self._tools = tools
        self._event_ctx = dict(event_ctx)
        self._completion = completion
        self._compress_tools = compress_tools
        self._human_input = human_input or _deny_human_input
        self._source = source
        # Playback: the step source asked before the model while ``ladder_stage`` is a
        # source stage; ``None`` keeps today's model-only turns.
        self._step_source = step_source
        self._profile = profile or RunProfile()
        self._catalog: dict[str, dict[str, Any]] = tool_catalog(tools) if step_source else {}
        self._proposal: StepProposal | None = None
        self._source_turns = 0
        #: The engine's turn number, set before every :meth:`run_turn`.
        self.turn = 0
        self._hooks = resources.hooks
        self._permissions = resources.permissions
        self._approval = resources.approval
        # What the model sees: with the model judge on, every state-changing tool offers the
        # optional ``approval_request`` argument (stripped again in ``_run_tool_turn``).
        self._model_tools: list[Any] = list(tools)
        self._approval_colliding: frozenset[str] = frozenset()
        if self._approval.model_signal:
            self._model_tools, self._approval_colliding = offer_approval_argument(tools)
        # Working notes (memory.working_notes_max_chars > 0): every tool offers the optional
        # ``notes`` argument, stripped again in ``_classify_action``.
        self._notes_max_chars = get_settings().memory.working_notes_max_chars
        self._notes_colliding: frozenset[str] = frozenset()
        if self._notes_max_chars > 0:
            self._model_tools, self._notes_colliding = offer_notes_argument(self._model_tools)
        # normalizers are registered with the tool registry (ToolRegistry.get_normalizers)
        self._broker = ToolBroker(resources.tool_registry)
        # The session's model tiers (one without a cascade: ``resources.llm`` unless the session
        # passed its labelled, priced tier); the agent runs on ``state.model_tier``.
        self._tiers = resources.tiers()
        self._drivers: dict[int, ModelDriver] = {}
        self._budget = budget or RunBudget()
        self._source_calls_seen = 0
        self._observation = ObservationCompiler()

    async def run_turn(self, state: LoopState) -> TurnResult:
        """Run one turn and describe what the engine should do next."""

        if state.approval_request:
            state = state.apply({"approval_request": ""})
        state = state.apply(await self._agent_step(state))
        decision = str(state.decision or "")

        if decision == "done":
            # A ``done`` decision is terminal by construction; an empty final answer would
            # otherwise derive ``"continue"`` (the legacy status contract), so coerce it.
            status = self._completion.status_from_state(state)
            return TurnResult(state=state, status="done" if status == "continue" else status)
        if decision == "replan":
            return TurnResult(state=state, replan=True)
        if decision == "continue":
            # A stop hook rejected the model's completion; the feedback is in ``messages``.
            return TurnResult(state=state)
        if decision == "tool_call":
            proposal, self._proposal = self._proposal, None
            state, terminal_status = await self._run_tool_turn(
                state, source=proposal.stage if proposal is not None else "model"
            )
            if proposal is not None and terminal_status is None:
                self._observe_proposal(state, proposal)
            return TurnResult(state=state, status=terminal_status)

        # Defensive: an unexpected decision must not spin forever.
        state = state.apply(
            blocked_response(
                state,
                f"Blocked: unexpected loop decision '{decision}'.",
                stop_reason="invalid_decision",
            )
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

        if self._step_source is not None and state.ladder_stage in SOURCE_STAGES:
            return await self._source_step(state)
        return await self._model_step(state)

    async def _model_step(self, state: LoopState) -> dict[str, Any]:
        """One model turn: ask the model and classify its action into a state update.

        The run budget is checked first: a task that used it up ends ``blocked`` with
        ``budget_exhausted`` before the call, and the call is added to ``usage`` after it.
        """

        exhausted = budget_exhausted(state.usage, self._budget)
        if exhausted:
            return blocked_response(
                state, f"Blocked: {exhausted}.", stop_reason="budget_exhausted"
            )
        messages = self._history(state)

        mapping = self._prompt_mapping(state)
        turn_prompt = self._resources.context.user_turn_prompt(
            mapping,
            tools=self._tools,
            memory=self._resources.memory.render(mapping),
        )
        tier = tier_fields(self._tiers, state.model_tier)
        self._emit(
            "model.requested", {"phase": "agent", "tool_count": len(self._tools), **tier}
        )
        model_turn = await self._driver(state.model_tier).invoke(
            [*messages, user_message(turn_prompt)],
            tools=self._model_tools,
        )
        usage = response_usage(model_turn.response)
        self._emit(
            "model.responded",
            {
                "phase": "agent",
                "metadata": dict(model_turn.metadata or {}),
                **({"usage": usage} if usage else {}),
                **tier,
            },
        )
        accounted = self._account(
            state.usage, "agent", model_turn.response, usage, tier=state.model_tier
        )

        if (
            _empty_response(model_turn.response)
            and get_settings().cascade.escalate_on_no_action
            and escalation_possible(state, tier_count=len(self._tiers.tiers))
        ):
            # A higher tier answers instead of the empty-response terminal; the engine
            # escalates before the next turn (``tier_escalation``).
            return {"decision": "continue", "tier_signal": NO_ACTION_SIGNAL, "usage": accounted}

        actions = list(model_turn.actions or [])
        if not actions:
            # ActionParser always returns >=1 action; this is a defensive terminal only.
            return {
                **blocked_response(
                    state, "Blocked: the model returned no actionable step.", stop_reason="no_action"
                ),
                "usage": accounted,
            }
        update = self._classify_action(state, actions[0], messages)
        # Only a successful completion *from the model* is checked; guard terminals (limits)
        # and blocked/cancelled stops are never second-guessed.
        if update.get("decision") == "done" and update.get("completion_status") == "done":
            update = await self._stop_check(state, update)
        return {**update, "usage": accounted}

    def _driver(self, index: int) -> ModelDriver:
        """The model driver of tier ``index``, built on first use."""

        driver = self._drivers.get(index)
        if driver is None:
            driver = ModelDriver(
                self._tiers.tier(index).model,
                tool_registry=self._resources.tool_registry,
            )
            self._drivers[index] = driver
        return driver

    def _account(
        self,
        usage: TaskUsage,
        stage: str,
        response: Any,
        tokens: Mapping[str, int] | None,
        *,
        tier: int = 0,
    ) -> TaskUsage:
        """``usage`` plus one call of tier ``tier``'s model."""

        model_tier = self._tiers.tier(tier)
        price = (
            model_tier.price.cost(getattr(response, "usage", None)) if model_tier.price else None
        )
        return add_call(usage, stage=stage, model=model_tier.label, tokens=tokens, cost=price)

    async def _stop_check(self, state: LoopState, update: dict[str, Any]) -> dict[str, Any]:
        """Run ``stop`` hooks on a model ``done`` before it is applied to state.

        A ``deny`` replaces the terminal update with ``decision: "continue"``: the final answer,
        completion status and plan completion are dropped, the model's answer stays in the
        history followed by the ``[harness]`` rejection feedback, and ``stop_blocks`` grows.
        Once ``stop_blocks`` reaches the engine's ``max_stop_blocks`` the hooks are skipped and
        ``done`` is accepted.
        """

        if not self._hooks.has("stop"):
            return update
        event = _hook_event(
            "stop",
            state,
            self._event_ctx,
            final_answer=str(update.get("final_answer", "") or ""),
            evidence=tuple(
                text
                for text in (state.observation, *state.environment.snapshots.values())
                if str(text or "")
            ),
            stop_hook_active=state.stop_blocks > 0,
        )
        if state.stop_blocks >= self._hooks.max_stop_blocks:
            for record in self._hooks.skip("stop", "stop_budget_exhausted"):
                self._emit("hook.decided", _hook_record_payload(record, event))
            return update

        outcome = await self._run_hooks(event)
        if outcome.decision != "deny":
            return update
        feedback = f"[harness] Completion rejected: {outcome.reason or 'no reason given'}"
        if outcome.additional_context:
            feedback = f"{feedback}\n{outcome.additional_context}"
        return {
            "decision": "continue",
            "stop_blocks": state.stop_blocks + 1,
            # The done update's history already ends with the model's final answer.
            "messages": [*update["messages"], user_message(feedback)],
        }

    # -- playback: a step source proposes before the model -----------------------------------

    async def _source_step(self, state: LoopState) -> dict[str, Any]:
        """Ask the step source for the next step; a handoff runs the model in this same turn.

        A proposal becomes a synthetic assistant tool call and then goes through the normal
        pipeline (progress guard, hooks, permissions, approval, broker, observation). The
        source's turns do not count toward the agent's turn cap; they are bounded by the
        recording itself (``replay.max_steps`` plus the retargets).
        """

        source = self._step_source
        assert source is not None
        self._source_turns += 1
        replay = get_settings().replay
        if self._source_turns > replay.max_steps + replay.max_retargets_per_run + 1:
            outcome: Any = StepHandoff(
                reason="Playback stopped after too many replayed steps. Continue the task yourself.",
                kind="exhausted",
            )
        else:
            outcome = await source.next(self._step_view(state))
        # The source's own model calls (retargets) count toward the task's usage.
        state = self._with_source_usage(state)
        update = await self._source_outcome(state, outcome)
        update.setdefault("usage", state.usage)
        return update

    async def _source_outcome(self, state: LoopState, outcome: Any) -> dict[str, Any]:
        """Turn the source's answer into the state update of this turn."""

        if isinstance(outcome, StepProposal):
            request: ToolRequest = dict(outcome.request)  # type: ignore[assignment]
            # A call id, like a native tool call carries, so the history pairs the
            # synthetic assistant call with its tool result.
            request.setdefault("id", f"{outcome.stage}-{outcome.step}-{self._source_turns}")
            update = tool_request_update(state, self._history(state), request)
            if update.get("decision") != "tool_call":
                # The repeat guard would replan, which costs a model call: hand over instead.
                return await self._handoff(
                    state,
                    StepHandoff(
                        reason=(
                            "Playback stopped: the recorded step repeated the same call. "
                            "Continue the task yourself."
                        ),
                        kind="repeat",
                        step=outcome.step,
                    ),
                )
            self._proposal = outcome
            current = state.current_step
            if outcome.plan_step is not None and state.plan:
                current = max(0, min(int(outcome.plan_step), len(state.plan) - 1))
            return {
                **update,
                "ladder_stage": outcome.stage,
                "current_step": current,
                "usage": _reached(state.usage, outcome.stage),
            }

        if isinstance(outcome, StepFinal):
            update = done_response(
                state,
                outcome.final_answer,
                messages=self._history(state),
                stop_reason="playback_completed",
            )
            update = await self._stop_check(state, update)
            if update.get("decision") == "continue":
                # A stop hook rejected the replayed answer: the agent continues from its
                # feedback (already in the history).
                handoff = StepHandoff(
                    reason="The replayed answer was rejected. Finish the task yourself.",
                    kind="answer",
                )
                return {
                    "stop_blocks": update["stop_blocks"],
                    **await self._handoff(state.apply(update), handoff),
                }
            return {**update, "usage": _reached(state.usage, outcome.stage)}

        if not isinstance(outcome, StepHandoff):
            outcome = StepHandoff(
                reason="Playback stopped. Continue the task yourself.",
                kind="exhausted",
                detail=f"unexpected step outcome {type(outcome).__name__}",
            )
        return await self._handoff(state, outcome)

    def _with_source_usage(self, state: LoopState) -> LoopState:
        source = self._step_source
        records = list(source.usage) if source is not None else []
        fresh, self._source_calls_seen = records[self._source_calls_seen :], len(records)
        usage = state.usage
        for record in fresh:
            usage = add_call(
                usage, stage=record.phase, model=record.model, tokens=record.usage, cost=record.cost
            )
        return state.apply({"usage": usage}) if fresh else state

    def _call_stage(self, state: LoopState) -> str:
        """The ladder stage a model call of this turn is accounted to."""

        return state.ladder_stage if self._step_source is not None else "agent"

    async def _handoff(self, state: LoopState, handoff: StepHandoff) -> dict[str, Any]:
        """The source gives up: the model drives the rest of the task (sticky), unless the
        run may not climb to the agent (``ladder_cap``)."""

        detail = handoff.detail[: get_settings().replay.detail_max_chars]
        if handoff.kind != "answer":
            self._emit(
                "playback.diverged", {"step": handoff.step, "kind": handoff.kind, "detail": detail}
            )
        diverged = {} if handoff.kind == "answer" else {"playback_diverged": handoff.kind}
        max_stage = self._profile.max_stage
        if ladder_rank(max_stage) < ladder_rank("agent"):
            reason = (
                f"Blocked: playback stopped ({handoff.kind}) and this run may not go above "
                f"the {max_stage} stage."
            )
            return {**blocked_response(state, reason, stop_reason="ladder_cap"), **diverged}
        self._emit(
            "ladder.escalated",
            {"from": state.ladder_stage, "to": "agent", "reason": handoff.kind, "step": handoff.step},
        )
        # With a cascade, what playback ran into is not the first tier's doing: the agent starts
        # with a fresh failure window and the cascade counts from here (as after a tier change).
        cascade: dict[str, Any] = {}
        if len(self._tiers.tiers) > 1:
            cascade = {
                "consecutive_failures": 0,
                "tier_marks": TierMarks(replans=state.replan_count, progress_blocks=state.progress_blocks),
            }
        handed = state.apply(
            {
                "ladder_stage": "agent",
                "usage": _reached(state.usage, "agent"),
                "messages": [*self._history(state), user_message(f"[harness] {handoff.reason}")],
                **cascade,
            }
        )
        update = await self._model_step(handed)
        return {
            "ladder_stage": "agent",
            "usage": handed.usage,
            "messages": handed.messages,
            **cascade,
            **diverged,
            **update,
        }

    def _observe_proposal(self, state: LoopState, proposal: StepProposal) -> None:
        """Tell the source how its step went: the tool result, or why it was blocked."""

        source = self._step_source
        if source is None:
            return
        blocked = ""
        result: dict[str, Any] = {}
        if state.policy_decision == "blocked":
            blocked = str((state.policy_event or {}).get("reason") or state.error or "blocked")
        else:
            result = dict(state.tool_result or {})
        source.observe(proposal, result, self._step_view(state), blocked=blocked)
        self._emit(
            "playback.step",
            {
                "step": proposal.step,
                "stage": proposal.stage,
                "binding": proposal.binding,
                "status": "blocked" if blocked else str(result.get("status") or ""),
            },
        )

    def _step_view(self, state: LoopState) -> StepView:
        return StepView(
            task=state.task,
            turn=self.turn,
            tool_result=dict(state.tool_result or {}),
            snapshots=dict(state.environment.snapshots),
            tool_schemas=self._catalog,
            usage=state.usage,
        )

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
                stop_reason="answered",
            )

        if kind == "tool_call":
            request = normalize_tool_request(action.get("tool_request"))
            notes: str | None = None
            if self._notes_max_chars > 0:
                # Working notes are not a tool argument either (see below).
                request, notes = split_notes(
                    request, self._notes_colliding, self._notes_max_chars
                )
            notes_update = {} if notes is None else {"working_notes": notes}
            if not self._approval.model_signal:
                return {**tool_request_update(state, messages, request), **notes_update}
            # The model's approval request is not a tool argument: strip it before the
            # history, the repeat tracking, the progress guard, hooks and the tool see it.
            request, approval_request = split_approval_request(
                request, self._approval_colliding, self._approval.false_words
            )
            return {
                **tool_request_update(state, messages, request),
                **notes_update,
                "approval_request": approval_request,
            }

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
                return done_response(state, message, messages=messages, stop_reason="model_stop")
            if status == "cancelled":
                return done_response(
                    state,
                    f"Cancelled: {message}" if message else "Cancelled.",
                    messages=messages,
                    status="cancelled",
                    stop_reason="model_stop",
                )
            # A failed or blocked stop is an honest "the task was not achieved".
            if status == "failed":
                return done_response(
                    state,
                    f"Failed: {message}" if message else "Failed.",
                    messages=messages,
                    status="blocked",
                    stop_reason="model_stop",
                )
            return done_response(
                state,
                f"Blocked: {message}" if message else "Blocked.",
                messages=messages,
                status="blocked",
                stop_reason="model_stop",
            )

        return replan_response("Unrecognized proposed action.")

    async def _run_tool_turn(
        self,
        state: LoopState,
        *,
        source: str = "model",
    ) -> tuple[LoopState, CompletionStatus | None]:
        """Run progress guard -> hooks -> permissions -> (execute -> observe) for a tool call.

        Returns the new state and a terminal status if the turn ended the run (a refused
        approval); ``None`` means continue looping. Every non-terminal block (progress guard,
        hook deny, permission deny) short-circuits before execute/observe so
        ``consecutive_failures`` grows exactly once and the model reads the reason as the
        tool output.

        Order: the progress guard checks the raw request (``policy.decided``); the broker
        normalizes and resolves it; ``pre_tool_use`` hooks may deny, ask or rewrite the
        arguments; the :class:`~src.harness.permissions.PermissionEngine` then decides on the
        **final** request (``permission.decided``) -- a hook ``allow`` cannot lift a rule, a
        hook ``ask`` escalates through the same mode logic. ``ask`` goes to
        ``permission_request`` hooks, then the human callback. An unknown tool skips hooks
        and permissions and observes its error result. ``post_tool_use(_failure)`` may
        rewrite the output; hook context becomes one separate ``[harness]`` message after the
        observation, never part of the tool output.
        """

        request = dict(state.tool_request or {})
        model_ask_reason = state.approval_request
        self._emit("action.proposed", {"tool_request": request, "source": source})

        progress_reason = progress_block_reason(state, request)
        if progress_reason is not None:
            state = state.apply(
                {
                    **tool_block_updates(state, progress_reason),
                    "progress_blocks": state.progress_blocks + 1,
                }
            )
            self._emit("policy.decided", dict(state.policy_event or {}))
            return state, None

        prepared = await self._broker.prepare(request, state.snapshot_mapping())
        # What tool.started/approval.requested report: the model's request, unless a hook
        # rewrote its arguments.
        started_request = request
        notes: list[str] = []
        hook_ask_reason = ""

        if prepared.tool is not None and self._hooks.has("pre_tool_use"):
            outcome, prepared = await self._pre_tool_use(state, prepared)
            notes.append(outcome.additional_context)
            if outcome.decision == "deny":
                blocked_reason = f"Blocked by hook: {outcome.reason or 'no reason given'}"
                state = state.apply(
                    tool_block_updates(state, blocked_reason, event={"source": "hook"})
                )
                return self._with_note(state, notes), None
            if outcome.updated_input is not None:
                started_request = {**request, "args": dict(outcome.updated_input)}
                state = state.apply({"tool_request": started_request})
            if outcome.decision == "ask":
                hook_ask_reason = outcome.reason or (
                    f"A hook requested approval for {prepared.request.get('name')}"
                )

        if prepared.tool is not None:
            state, terminal, blocked = await self._authorize(
                state, prepared, started_request, hook_ask_reason, model_ask_reason
            )
            if terminal is not None:
                return state, terminal
            if blocked:
                return self._with_note(state, notes), None

        read_only = prepared.tool is not None and tool_is_read_only(prepared.tool)
        # ``server``/``read_only`` let a recorder mirror the kept snapshots without names.
        self._emit(
            "tool.started",
            {"tool_request": started_request, "server": prepared.server, "read_only": read_only},
        )
        result: ToolResult = await self._broker.invoke(prepared)
        self._emit("tool.finished", {"tool_result": dict(result)})

        if prepared.tool is not None:
            result = await self._post_tool_use(state, prepared, result, notes)

        state = state.apply({"tool_result": result})
        observation_update = self._observation.compile(
            state,
            server=prepared.server,
            read_only=read_only,
            compress_tool_output=self._compress_tools,
        )
        state = state.apply(observation_update)
        self._emit(
            "observation.compiled",
            {"observation": state.observation, "has_snapshot": bool(state.environment.snapshots)},
        )

        if str(state.decision or "") == "done":
            return state, self._completion.status_from_state(state)
        return self._with_note(state, notes), None

    async def _authorize(
        self,
        state: LoopState,
        prepared: PreparedToolCall,
        started_request: ToolRequest,
        hook_ask_reason: str,
        model_ask_reason: str = "",
    ) -> tuple[LoopState, CompletionStatus | None, bool]:
        """Evaluate permissions on the final prepared call and resolve an ``ask``.

        Returns ``(state, terminal_status, blocked)``: ``terminal_status`` is ``"blocked"``
        when the approval was refused (by a ``permission_request`` hook or the human);
        ``blocked`` is a non-terminal permission deny whose reason the model reads.

        With the approval classifier on, a state-changing call that the engine allows only by
        default (no rule, no grant, no ask) is judged by the classifier first; its "needs
        approval" re-evaluates the call as an ``ask``.
        """

        snapshot = state.snapshot_mapping()
        check = PermissionCheck(
            tool=str(prepared.request.get("name", "") or ""),
            server=prepared.server,
            args=copy.deepcopy(dict(prepared.request.get("args") or {})),
            read_only=tool_is_read_only(prepared.tool),
            hook_ask_reason=hook_ask_reason,
            model_ask_reason=model_ask_reason,
            allowed_domains=tuple(self._profile.allowed_domains),
        )
        verdict = self._permissions.evaluate(check, snapshot)
        if (
            self._approval.classifies
            and verdict.decision == "allow"
            and verdict.source == "mode"
            and not verdict.rule_id
            and not check.read_only
        ):
            exhausted = budget_exhausted(state.usage, self._budget)
            if exhausted:
                # No room for the judge's model call: ask the human instead (fail closed).
                reason = f"Approval needed: {exhausted}, so the approval classifier did not run."
            else:
                reason = await self._classify(state, prepared, check, snapshot)
                # The judge's call counts toward the task; it reports no tokens, so its cost
                # is unknown.
                state = state.apply(
                    {
                        "usage": add_call(
                            state.usage, stage=self._call_stage(state), tokens={}, cost=None
                        )
                    }
                )
            if reason:
                check = dataclasses.replace(check, classifier_ask_reason=reason)
                verdict = self._permissions.evaluate(check, snapshot)
        self._emit(
            "permission.decided",
            _permission_payload(verdict, prepared, self._permissions.mode),
        )
        event = {"source": verdict.source, "rule_id": verdict.rule_id}

        if verdict.decision == "deny":
            state = state.apply(tool_block_updates(state, verdict.reason, event=event))
            return state, None, True

        if verdict.decision == "allow":
            if verdict.source == "grant":
                self._emit_approval_resolved("allow", by="grant", scope="session")
            state = state.apply(tool_gate_updates(state, "approved", verdict.reason, event=event))
            return state, None, False

        state = state.apply(tool_gate_updates(state, "needs_human", verdict.reason, event=event))
        self._emit(
            "approval.requested",
            {
                "tool_request": started_request,
                "reason": verdict.reason,
                "rule_id": verdict.rule_id,
            },
        )
        denied = str(started_request.get("name") or "the requested tool")
        hook_outcome = await self._permission_request(state, prepared, verdict.reason)
        if hook_outcome.decision == "deny":
            self._emit_approval_resolved("deny", by="hook", scope="once")
            reason = (
                f"Blocked: approval hook denied {denied}: "
                f"{hook_outcome.reason or 'no reason given'}"
            )
            update = blocked_response(state, reason, stop_reason="approval_hook_denied")
            return state.apply(update), "blocked", False
        if hook_outcome.decision == "allow":
            self._emit_approval_resolved("allow", by="hook", scope="once")
            return state, None, False

        answer = _approval_answer(
            await self._human_input(started_request, verdict.reason, verdict)
        )
        if answer == "deny":
            self._emit_approval_resolved("deny", by="human", scope="once")
            reason = f"Blocked: human approval was denied for {denied}."
            update = blocked_response(state, reason, stop_reason="approval_denied")
            return state.apply(update), "blocked", False
        scope = "once"
        if answer == "session" and verdict.grant_key is not None and not verdict.always_ask:
            self._permissions.grant(verdict.grant_key)
            scope = "session"
        self._emit_approval_resolved("allow", by="human", scope=scope)
        return state, None, False

    async def _classify(
        self,
        state: LoopState,
        prepared: PreparedToolCall,
        check: PermissionCheck,
        snapshot: Mapping[str, Any],
    ) -> str:
        """Ask the approval classifier about ``check``; the ask reason, ``""`` when safe."""

        classifier = self._approval.classifier
        if classifier is None:
            return ""
        try:
            resources = self._permissions.resources(check, snapshot)
        except Exception:  # noqa: BLE001 - the classifier then judges without page context
            resources = {}
        self._emit("model.requested", {"phase": "approval", "tool": check.tool})
        judgment = await classifier.judge(
            task=state.task,
            tool=check.tool,
            description=str(getattr(prepared.tool, "description", "") or ""),
            args=check.args,
            resources=resources,
        )
        self._emit(
            "model.responded",
            {
                "phase": "approval",
                "tool": check.tool,
                "needs_approval": judgment.needs_approval,
                "error": judgment.error,
            },
        )
        return judgment.reason if judgment.needs_approval else ""

    def _emit_approval_resolved(self, decision: str, *, by: str, scope: str) -> None:
        self._emit("approval.resolved", {"decision": decision, "by": by, "scope": scope})

    @staticmethod
    def _with_note(state: LoopState, notes: list[str]) -> LoopState:
        note = _harness_note(notes)
        return state if note is None else state.apply({"messages": [*state.messages, note]})

    async def _pre_tool_use(
        self,
        state: LoopState,
        prepared: PreparedToolCall,
    ) -> tuple[HookOutcome, PreparedToolCall]:
        """Run ``pre_tool_use``; re-prepare rewritten arguments, which may not change the tool."""

        outcome = await self._run_hooks(
            _hook_event(
                "pre_tool_use",
                state,
                self._event_ctx,
                tool=str(prepared.request.get("name", "") or ""),
                server=prepared.server,
                args=prepared.request.get("args") or {},
            )
        )
        if outcome.decision == "deny" or outcome.updated_input is None:
            return outcome, prepared

        reprepared = await self._broker.prepare(
            {**prepared.request, "args": dict(outcome.updated_input)},
            state.snapshot_mapping(),
        )
        if reprepared.tool is None or reprepared.request.get("name") != prepared.request.get("name"):
            modifier = next(
                (record.hook_id for record in reversed(outcome.records) if record.modified),
                "unknown",
            )
            denied = HookOutcome(
                decision="deny",
                reason=f"Hook {modifier} cannot change the tool name.",
                additional_context=outcome.additional_context,
                records=outcome.records,
            )
            return denied, prepared
        return outcome, reprepared

    async def _permission_request(
        self,
        state: LoopState,
        prepared: PreparedToolCall,
        reason: str,
    ) -> HookOutcome:
        """Let ``permission_request`` hooks stand in for the human on an ``ask`` verdict.

        ``allow`` approves, ``deny`` refuses exactly like a human would; anything else (no
        hooks, no opinion, ``ask``, a failed hook) leaves the decision to the human callback.
        """

        if prepared.tool is None or not self._hooks.has("permission_request"):
            return HookOutcome()
        return await self._run_hooks(
            _hook_event(
                "permission_request",
                state,
                self._event_ctx,
                tool=str(prepared.request.get("name", "") or ""),
                server=prepared.server,
                args=prepared.request.get("args") or {},
                reason=reason,
            )
        )

    async def _post_tool_use(
        self,
        state: LoopState,
        prepared: PreparedToolCall,
        result: ToolResult,
        notes: list[str],
    ) -> ToolResult:
        """Run ``post_tool_use`` (success) or ``post_tool_use_failure``; apply ``updated_output``."""

        name: HookEventName = (
            "post_tool_use" if result.get("status") == "success" else "post_tool_use_failure"
        )
        if not self._hooks.has(name):
            return result
        outcome = await self._run_hooks(
            _hook_event(
                name,
                state,
                self._event_ctx,
                tool=str(prepared.request.get("name", "") or ""),
                server=prepared.server,
                args=prepared.request.get("args") or {},
                result=result,
            )
        )
        notes.append(outcome.additional_context)
        if outcome.updated_output is None:
            return result
        field_name = "content" if name == "post_tool_use" else "error"
        return {**result, field_name: outcome.updated_output}  # type: ignore[return-value]

    async def _run_hooks(self, event: HookEvent) -> HookOutcome:
        """Run hooks for ``event``, emitting one ``hook.decided`` per handler as it finishes."""

        return await self._hooks.run(
            event,
            on_record=lambda record: self._emit(
                "hook.decided", _hook_record_payload(record, event)
            ),
        )

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
                "action_history": render_action_history(
                    state.action_history,
                    get_settings().observation.action_history_limit,
                ),
                "working_notes": state.working_notes,
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
    :class:`Agent`.
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
        self._budget = RunBudget()

    async def run(
        self,
        task: str,
        *,
        task_id: str = "",
        goal_id: str = "",
        session_id: str | None = None,
        state_overrides: Mapping[str, Any] | None = None,
        turn_cap: int | None = None,
        profile: RunProfile | None = None,
    ) -> AgentLoopResult:
        """Run the loop to a terminal state and return the :class:`AgentLoopResult`.

        ``profile.playback`` replays a recording through the session's step source
        (``EngineResources.steps``): its recorded plan replaces the planner call and its
        steps are proposed before the model is asked. Without it nothing changes.
        """

        self._event_ctx = {
            "session_id": session_id,
            "task_id": task_id,
            "goal_id": goal_id or task_id,
        }
        turn_cap = max(1, int(turn_cap or get_settings().loop.turn_cap))
        profile = profile or RunProfile()
        self._budget = effective_budget(profile.budget, get_settings().loop)

        tools = list(await self._resources.tool_registry.get_all())
        step_source = self._build_step_source(task, profile)
        turn_controller = TurnController(
            self._resources,
            tools=tools,
            event_ctx=self._event_ctx,
            completion=self._completion,
            compress_tools=self._compress_tools,
            human_input=self._human_input,
            source=self._source,
            step_source=step_source,
            profile=profile,
            budget=self._budget,
        )

        state = LoopState.from_state_overrides(state_overrides)
        state = state.apply({"task": task})
        if task_id and not state.task_id:
            state = state.apply({"task_id": task_id})
        if step_source is not None:
            state = state.apply(
                {
                    "ladder_stage": "playback",
                    "usage": dataclasses.replace(state.usage, stage_reached="playback"),
                }
            )

        state, denied = await self._goal_start(state)
        if denied:
            return await self._finish(state, "blocked", turns=0)

        recorded_plan = step_source.plan() if step_source is not None else None
        if step_source is not None and not recorded_plan and (
            ladder_rank(profile.max_stage) < ladder_rank("agent")
        ):
            # This run may not use the model at all: a one-step plan instead of call #0.
            recorded_plan = _normalize_steps(None, task or "Complete the task.")
        if recorded_plan:
            # The recording's plan: no planner call (model call #0) for a replay.
            state = self._seed_plan(state, recorded_plan)
        else:
            blocked = self._budget_block(state)
            if blocked is not None:
                return await self._finish(blocked, "blocked", turns=0)
            # Model call #0: build the initial plan (consumes the planner response).
            state = await self._run_plan(state)

        final_status: CompletionStatus | None = None
        turn = 0
        agent_turns = 0
        while True:
            state = self._escalate_tier(state)
            turn += 1
            # Replayed steps are bounded by the recording; the cap counts the agent's turns.
            source_turn = step_source is not None and state.ladder_stage in SOURCE_STAGES
            if not source_turn:
                agent_turns += 1
            if agent_turns > turn_cap:
                state = state.apply(
                    blocked_response(
                        state,
                        (
                            f"Blocked: reached the maximum of {turn_cap} agent turns "
                            "without a final answer."
                        ),
                        stop_reason="turn_cap",
                    )
                )
                final_status = "blocked"
                break

            self._emit("turn.started", {"turn": turn, "current_step": state.current_step})
            plan_before = state.plan
            step_before = state.current_step
            turn_controller.turn = turn
            result = await turn_controller.run_turn(state)
            state = result.state
            if source_turn and state.ladder_stage not in SOURCE_STAGES:
                # A handoff ran a model step inside this playback turn.
                agent_turns += 1
            if state.plan != plan_before or state.current_step != step_before:
                self._emit_plan(state)
            if result.status is not None:
                final_status = result.status
                break
            if result.replan:
                blocked = self._budget_block(state)
                if blocked is not None:
                    state, final_status = blocked, "blocked"
                    break
                state = await self._run_plan(state)
                continue

        return await self._finish(state, final_status or "blocked", turns=turn)

    def _escalate_tier(self, state: LoopState) -> LoopState:
        """Before every turn: move the agent up a model tier when it is stuck
        (``guards.tier_escalation``), with one ``ladder.escalated``; a no-op with one tier."""

        tiers = self._resources.tiers()
        if len(tiers.tiers) < 2:
            return state
        update, reason = tier_escalation(
            state, get_settings().cascade, tier_count=len(tiers.tiers)
        )
        if not update:
            return state
        if reason is not None:
            self._emit(
                "ladder.escalated",
                {
                    "from": "agent",
                    "to": "agent",
                    "reason": reason,
                    "from_tier": state.model_tier,
                    "tier": update["model_tier"],
                    "model": tiers.tier(update["model_tier"]).label,
                },
            )
        return state.apply(update)

    def _build_step_source(self, task: str, profile: RunProfile) -> StepSource | None:
        """The task's step source when it replays a recording; ``None`` otherwise.

        A playback request without a session step source is a configuration error (replay
        is off): it fails the task rather than silently running the agent.
        """

        request = profile.playback
        if request is None:
            return None
        factory = self._resources.steps
        if factory is None:
            raise ValueError("this task replays a recording, but replay is not enabled")
        source = factory.build(
            request,
            task=task,
            budget=self._budget,
            max_stage=profile.max_stage,
            emit=self._emit,
        )
        self._emit("playback.started", dict(source.info))
        return source

    def _seed_plan(self, state: LoopState, plan: list[PlanStep]) -> LoopState:
        """Install a recorded plan as if the planner had returned it (no model call)."""

        steps: list[PlanStep] = [
            {**step, "status": "pending"} for step in plan if isinstance(step, Mapping)  # type: ignore[typeddict-item]
        ]
        state = state.apply(
            {
                "plan": steps or _normalize_steps(None, state.task or "Complete the task."),
                "current_step": 0,
                "decision": "replan",
                "error": "",
                "messages": self._history(state),
            }
        )
        self._emit_plan(state)
        return state

    async def _goal_start(self, state: LoopState) -> tuple[LoopState, bool]:
        """Run ``goal_start`` hooks before the first model call; ``True`` means denied.

        A deny blocks the task without calling the model. ``additional_context`` lands after
        the ``User request`` message (so the planner sees it) as a ``[harness]`` message.
        """

        if not self._resources.hooks.has("goal_start"):
            return state, False
        outcome = await self._run_hooks(_hook_event("goal_start", state, self._event_ctx))
        if outcome.decision == "deny":
            reason = outcome.reason or "a goal_start hook denied the task."
            # Seed the real system prompt first, as the planner would have.
            state = state.apply({"messages": self._history(state)})
            update = blocked_response(state, f"Blocked: {reason}", stop_reason="goal_hook_denied")
            return state.apply(update), True
        note = _harness_note([outcome.additional_context])
        if note is not None:
            state = state.apply({"messages": [*self._history(state), note]})
        return state, False

    async def _finish(
        self,
        state: LoopState,
        status: CompletionStatus,
        *,
        turns: int,
    ) -> AgentLoopResult:
        """Build the terminal result, after the observational ``goal_end`` hooks.

        Only a normal terminal result reaches here: engine exceptions, cancellation and
        ``GoalRunner`` timeouts never run ``goal_end`` (``goal.failed``/``goal.cancelled``
        cover them). A failing ``goal_end`` hook cannot change the result.
        """

        if self._resources.hooks.has("goal_end"):
            await self._run_hooks(_hook_event("goal_end", state, self._event_ctx, status=status))
        return AgentLoopResult(
            status=status,
            final_answer=str(state.final_answer or ""),
            session_state=state.to_session_state(),
            state=state,
            turns=turns,
            stop_reason=state.stop_reason,
            usage=state.usage,
        )

    async def _run_hooks(self, event: HookEvent) -> HookOutcome:
        """Run hooks for ``event``, emitting one ``hook.decided`` per handler as it finishes."""

        return await self._resources.hooks.run(
            event,
            on_record=lambda record: self._emit(
                "hook.decided", _hook_record_payload(record, event)
            ),
        )

    async def _run_plan(self, state: LoopState) -> LoopState:
        """Build/replace the plan via a raw planner model call (ports ``plan_node``)."""

        messages = self._history(state)
        prior_replans = int(state.replan_count or 0)
        replan_count = prior_replans + 1 if state.plan else prior_replans

        mapping = self._plan_mapping(state)
        plan_prompt = self._resources.context.plan_prompt(
            mapping, memory=self._resources.memory.render(mapping)
        )
        # Call #0 runs on tier 0; a replan on the tier the agent is on.
        tiers = self._resources.tiers()
        fields = tier_fields(tiers, state.model_tier)
        self._emit("model.requested", {"phase": "plan", **fields})
        tier = tiers.tier(state.model_tier)
        response = await tier.model.complete([*messages, user_message(plan_prompt)])
        usage = response_usage(response)
        self._emit(
            "model.responded", {"phase": "plan", **({"usage": usage} if usage else {}), **fields}
        )
        cost = tier.price.cost(getattr(response, "usage", None)) if tier.price else None
        accounted = add_call(state.usage, stage="agent", model=tier.label, tokens=usage, cost=cost)

        data = _json_object(_message_content(response))
        plan = _normalize_steps(data.get("steps"), state.task or "Complete the task.")
        state = state.apply(
            {
                "plan": plan,
                "current_step": 0,
                "decision": "replan",
                "replan_count": replan_count,
                "error": "",
                "messages": messages,
                "usage": accounted,
            }
        )
        self._emit_plan(state)
        return state

    def _budget_block(self, state: LoopState) -> LoopState | None:
        """The blocked state when the run budget has no room for the next model call."""

        exhausted = budget_exhausted(state.usage, self._budget)
        if not exhausted:
            return None
        return state.apply(
            blocked_response(state, f"Blocked: {exhausted}.", stop_reason="budget_exhausted")
        )

    def _emit_plan(self, state: LoopState) -> None:
        """Publish the plan's content: every (re)plan and every change of step status."""

        self._emit(
            "plan.updated",
            {
                "steps": [dict(step) for step in state.plan],
                "current_step": state.current_step,
                "replan_count": int(state.replan_count or 0),
            },
        )

    def _history(self, state: LoopState) -> list[Message]:
        return _build_history(self._resources, state)

    def _plan_mapping(self, state: LoopState) -> dict[str, Any]:
        return {"task": state.task, "task_id": state.task_id, "observation": state.observation}

    def _emit(self, event_type: str, payload: Mapping[str, Any]) -> None:
        _emit_event(
            self._resources,
            source=self._source,
            event_ctx=self._event_ctx,
            event_type=event_type,
            payload=payload,
        )


class Agent:
    """The assembled agent: its :class:`EngineResources` and the human-approval channel.

    Built once by the composition root (``SessionContext.build_agent``; evals build their
    own over fake tools). Each :meth:`run` drives a fresh :class:`AgentLoopEngine` over one
    :class:`~src.contracts.TaskRequest`; lifecycle concerns (goal events, watchdog,
    timeouts) stay in the enclosing :class:`~src.agent_loop.goals.GoalRunner`, whose
    ``TaskRunner`` shape :meth:`run` matches.
    """

    def __init__(
        self,
        resources: EngineResources,
        *,
        human_input: HumanInputCallback | None = None,
    ) -> None:
        self.resources = resources
        self._human_input = human_input

    async def run(self, request: TaskRequest) -> AgentLoopResult:
        """Run one task to a terminal :class:`AgentLoopResult`."""

        engine = AgentLoopEngine(
            self.resources,
            compress_tools=request.compress_tools,
            human_input=self._human_input,
        )
        return await engine.run(
            request.task,
            task_id=request.task_id,
            goal_id=request.goal_id,
            session_id=request.session_id,
            state_overrides=request.state_overrides,
            turn_cap=request.turn_cap,
            profile=request.profile,
        )


__all__ = [
    "EVENT_SOURCE",
    "Agent",
    "AgentLoopEngine",
    "AgentLoopResult",
    "HumanInputCallback",
    "TurnController",
    "TurnResult",
]
