"""Playback of recordings: the step source, its factory and the plain-text adapter.

``PlaybackSource`` is the engine's :class:`~src.contracts.StepSource` over one stored
recording (``docs/development/2026-11-02-serve-mode-design.md`` §5.2). Before each recorded
step it checks the tool catalog, refuses a step with a redacted secret and rebinds element
references to the current page through the session's :class:`~src.contracts.PlaybackAdapter`
— all **before** the call, so a divergence has no side effect. After the step it compares the
output with the recorded checkpoint. A divergence it cannot repair hands the task to the
agent; a binding or tool error may first go to a one-step retarget (stage 2) when a
retargeter is configured.

``TextPlaybackAdapter`` serves tool servers without a browser snapshot format: arguments
replay literally (no references to rebind) and a checkpoint is a number-blind multiset of
word shingles. Browser snapshots are :mod:`src.browser.playback`'s business.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from src.agent_loop.execution.usage import budget_exhausted
from src.agent_loop.model import response_usage
from src.agent_loop.prompts import PLAYBACK_RETARGET_SYSTEM_PROMPT, PLAYBACK_RETARGET_USER_PROMPT
from src.agent_loop.recordings import (
    RecordedStep,
    Recording,
    fingerprint_key,
    multiset_similarity,
    select_region,
)
from src.config import ReplaySettings
from src.contracts import (
    Binding,
    BindResult,
    EventCallback,
    LadderStage,
    ModelCallRecord,
    PlanStep,
    PlaybackAdapter,
    PlaybackAnswer,
    PlaybackRequest,
    RunBudget,
    StepFinal,
    StepHandoff,
    StepHandoffKind,
    StepOutcome,
    StepProposal,
    StepView,
    ToolDef,
    ladder_rank,
)
from src.harness.recording_store import RecordingStore
from src.llm import ChatModel, ModelPrice, ModelProviderError
from src.messages import system_message, user_message

_LOG = logging.getLogger(__name__)

#: The model-facing note of a handoff: why playback stopped (fixed wording, no page text).
_HANDOFF_REASONS: dict[str, str] = {
    "catalog": "the tool of recorded step {step} ({tool}) is gone or changed",
    "binding": "the page element of recorded step {step} ({tool}) was not found on the current page",
    "redacted": "recorded step {step} ({tool}) needs a secret that recordings never store",
    "tool_error": "recorded step {step} ({tool}) failed",
    "checkpoint": "after recorded step {step} ({tool}) the page does not look like the recording",
    "denied": "recorded step {step} ({tool}) was blocked: {detail}",
}
_CONTINUE = "Continue the task yourself from the current state."
_ANSWER = "The recorded steps of this task ran. Answer the task from the current page."


class StepRetargeter(Protocol):
    """Stage 2: one model call that re-chooses the arguments of a diverged recorded step."""

    async def retarget(
        self,
        *,
        task: str,
        step: RecordedStep,
        reason: str,
        view: StepView,
        page_texts: Sequence[str],
        emit: EventCallback,
    ) -> tuple[dict[str, Any] | None, ModelCallRecord | None]:
        """The new arguments (``None``: no acceptable call) and the model call it made."""


@dataclass
class _Divergence:
    kind: str
    detail: str = ""


class PlaybackSource:
    """:class:`~src.contracts.StepSource` replaying one recording."""

    def __init__(
        self,
        recording: Recording,
        request: PlaybackRequest,
        adapter: PlaybackAdapter,
        settings: ReplaySettings,
        *,
        task: str = "",
        budget: RunBudget | None = None,
        max_stage: LadderStage = "human",
        emit: EventCallback | None = None,
        retargeter: StepRetargeter | None = None,
    ) -> None:
        self._recording = recording
        self._request = request
        self._adapter = adapter
        self._settings = settings
        self._task = task or recording.task
        self._budget = budget or RunBudget()
        self._max_stage = max_stage
        self._emit = emit or (lambda _type, _payload: None)
        self._retargeter = retargeter
        self._index = 0
        self._divergence: _Divergence | None = None
        self._usage: list[ModelCallRecord] = []
        self._retargets = 0
        self._retargeted_step: int | None = None
        self._stage: LadderStage = "playback"

    # -- the StepSource protocol ----------------------------------------------------------

    @property
    def info(self) -> Mapping[str, Any]:
        return {
            "recording": self._recording.name,
            "steps": len(self._recording.steps),
            "answer": self._answer_mode,
        }

    @property
    def usage(self) -> Sequence[ModelCallRecord]:
        return tuple(self._usage)

    def plan(self) -> list[PlanStep] | None:
        return [dict(step) for step in self._recording.plan] or None  # type: ignore[misc]

    async def next(self, view: StepView) -> StepOutcome:
        divergence, self._divergence = self._divergence, None
        steps = self._recording.steps
        if divergence is not None:
            step = steps[self._index]
            if divergence.kind == "tool_error" and self._retargeted_step != step.index:
                proposal = await self._retarget(step, view, divergence)
                if proposal is not None:
                    return proposal
            return self._handoff(divergence.kind, step, divergence.detail)
        if self._index >= len(steps):
            return self._answer(view)

        step = steps[self._index]
        problem = self._catalog_problem(step, view)
        if problem:
            return self._handoff("catalog", step, problem)
        if step.redacted:
            return self._handoff("redacted", step, "redacted arguments")
        bound = self._adapter.rebind(step.args, step.bindings, self._page_texts(view, step.server))
        if bound.args is None:
            proposal = await self._retarget(step, view, _Divergence("binding", bound.detail))
            return proposal or self._handoff("binding", step, bound.detail)
        return StepProposal(
            request={"name": step.tool, "args": dict(bound.args)},
            stage="playback",
            step=step.index,
            plan_step=step.plan_step,
            binding="rebound" if step.bindings else "none",
        )

    def observe(
        self,
        proposal: StepProposal,
        result: Mapping[str, Any],
        view: StepView,
        *,
        blocked: str = "",
    ) -> None:
        step = self._recording.steps[self._index]
        if blocked:
            self._divergence = _Divergence("denied", blocked)
            return
        if result.get("status") != "success":
            self._divergence = _Divergence(
                "tool_error", str(result.get("error") or "")[: self._settings.detail_max_chars]
            )
            return
        if step.checkpoint:
            current = self._adapter.checkpoint(str(result.get("content") or ""))
            similarity = self._adapter.similarity(step.checkpoint, current)
            if similarity < self._settings.checkpoint_min_similarity:
                # The action already ran and cannot be undone: the agent takes over (§5.5),
                # never a retarget of the next step on an unexpected page.
                self._divergence = _Divergence("checkpoint", f"similarity {similarity:.2f}")
                return
        self._index += 1

    # -- internals ------------------------------------------------------------------------

    @property
    def _answer_mode(self) -> PlaybackAnswer:
        return self._request.answer or self._recording.answer

    def _answer(self, view: StepView) -> StepOutcome:
        mode = self._answer_mode
        if mode == "agent":
            return StepHandoff(reason=_ANSWER, kind="answer")
        if mode == "output":
            text = self._adapter.region(
                str(view.tool_result.get("content") or ""), self._request.select, self._request.ignore
            )
            if not text and self._recording.steps:
                # The answer region is gone: the page changed in a way the checkpoints missed.
                return self._handoff(
                    "checkpoint", self._recording.steps[-1], "the answer region is empty"
                )
        else:
            count = len(self._recording.steps)
            text = f"Replayed {count} recorded step{'s' if count != 1 else ''} of {self._recording.name}."
        return StepFinal(final_answer=text, stage=self._stage)  # type: ignore[arg-type]

    def _handoff(self, kind: str, step: RecordedStep, detail: str) -> StepHandoff:
        template = _HANDOFF_REASONS.get(kind, "playback stopped at recorded step {step}")
        reason = template.format(step=step.index, tool=step.tool, detail=detail or "no reason given")
        handoff_kind: StepHandoffKind = "binding" if kind == "redacted" else kind  # type: ignore[assignment]
        return StepHandoff(
            reason=f"Playback stopped: {reason}. {_CONTINUE}",
            kind=handoff_kind,
            step=step.index,
            detail=detail,
        )

    def _catalog_problem(self, step: RecordedStep, view: StepView) -> str:
        current = view.tool_schemas.get(step.tool)
        if current is None:
            return "tool not available"
        recorded = self._recording.catalog.get(step.tool)
        if recorded is None:
            return ""
        if recorded.get("schema_digest") != current.get("schema_digest"):
            return "input schema changed"
        if bool(recorded.get("read_only")) != bool(current.get("read_only")):
            return "readOnlyHint changed"
        return ""

    @staticmethod
    def _page_texts(view: StepView, server: str) -> list[str]:
        """The page as the step sees it: the latest output, then the server's kept output."""

        texts = [str(view.tool_result.get("content") or ""), str(view.snapshots.get(server) or "")]
        return [text for text in texts if text]

    async def _retarget(
        self, step: RecordedStep, view: StepView, divergence: _Divergence
    ) -> StepProposal | None:
        """Stage 2: ask the retargeter for this one step, when the run allows it."""

        if not self._may_retarget(view):
            return None
        assert self._retargeter is not None
        self._retargets += 1
        self._retargeted_step = step.index
        self._emit("ladder.escalated", {"from": "playback", "to": "retarget", "reason": divergence.kind, "step": step.index})
        try:
            args, call = await self._retargeter.retarget(
                task=self._task,
                step=step,
                reason=divergence.detail or divergence.kind,
                view=view,
                page_texts=self._page_texts(view, step.server),
                emit=self._emit,
            )
        except Exception:  # noqa: BLE001 - a failed retarget is a handoff, never a crash
            _LOG.warning("retargeting step %s failed", step.index, exc_info=True)
            # The retargeter answers every failed model call itself (with its record); an
            # error here came before any call, so nothing is counted.
            args, call = None, None
        if call is not None:
            self._usage.append(call)
        if args is None:
            return None
        self._stage = "retarget"
        return StepProposal(
            request={"name": step.tool, "args": args},
            stage="retarget",
            step=step.index,
            plan_step=step.plan_step,
            binding="retargeted",
        )

    def _may_retarget(self, view: StepView) -> bool:
        if self._retargeter is None or not self._settings.retarget_enabled:
            return False
        if ladder_rank(self._max_stage) < ladder_rank("retarget"):
            return False
        if self._retargets >= self._settings.max_retargets_per_run:
            return False
        return not budget_exhausted(view.usage, self._budget)



#: Resolves a recorded tool name to its current model-visible schema (``None``: gone).
ToolDefResolver = Callable[[str], Awaitable[ToolDef | None]]


class Retargeter:
    """Stage 2: one model call re-chooses the arguments of one diverged recorded step.

    The model sees the task, the recorded step (its element references replaced by their
    recorded descriptions), why it diverged and the current page, and is offered only the
    recorded tool. Its answer is accepted only as exactly one call of that tool whose
    reference arguments are live references of the current page (``adapter.rebind`` in
    verify mode); anything else — text, another tool, two calls, an invented reference,
    a timeout or a provider error — means no retarget, and the agent takes over.
    """

    def __init__(
        self,
        llm: ChatModel,
        adapter: PlaybackAdapter,
        settings: ReplaySettings,
        *,
        tool_def: ToolDefResolver,
        model_label: str = "",
        price: ModelPrice | None = None,
        tier: int = 0,
    ) -> None:
        self._llm = llm
        self._adapter = adapter
        self._settings = settings
        self._tool_def = tool_def
        self._label = model_label
        self._price = price
        self._tier = tier

    async def retarget(
        self,
        *,
        task: str,
        step: RecordedStep,
        reason: str,
        view: StepView,
        page_texts: Sequence[str],
        emit: EventCallback,
    ) -> tuple[dict[str, Any] | None, ModelCallRecord | None]:
        tool = await self._tool_def(step.tool)
        if tool is None:
            return None, None
        page = "\n\n".join(page_texts)[: self._settings.retarget_context_chars]
        messages = [
            system_message(PLAYBACK_RETARGET_SYSTEM_PROMPT),
            user_message(
                PLAYBACK_RETARGET_USER_PROMPT.format(
                    task=task,
                    tool=step.tool,
                    args=_described_args(step),
                    reason=reason,
                    page=page,
                )
            ),
        ]
        emit("model.requested", {"phase": "retarget", "tier": self._tier, "tool_count": 1})
        try:
            response = await asyncio.wait_for(
                self._llm.complete(messages, tools=[tool]),
                timeout=self._settings.retarget_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - any failed call is a rejection, still counted
            if isinstance(exc, TimeoutError):
                error = "timeout"
            elif isinstance(exc, ModelProviderError):
                error = str(exc)
            else:
                error = type(exc).__name__
            emit("model.responded", {"phase": "retarget", "tier": self._tier, "error": error})
            return None, ModelCallRecord(phase="retarget", model=self._label, tier=self._tier)
        usage = response_usage(response) or {}
        emit(
            "model.responded",
            {"phase": "retarget", "tier": self._tier, **({"usage": usage} if usage else {})},
        )
        record = ModelCallRecord(
            phase="retarget",
            model=self._label,
            usage=usage,
            cost=self._price.cost(response.usage) if self._price is not None else None,
            tier=self._tier,
        )
        calls = list(response.tool_calls or ())
        if len(calls) != 1 or calls[0].name != step.tool:
            return None, record
        args = dict(calls[0].arguments or {})
        if step.bindings:
            verified = self._adapter.rebind(args, step.bindings, page_texts, verify=True)
            if verified.args is None:
                return None, record
        return args, record


def _described_args(step: RecordedStep) -> str:
    """The recorded arguments as JSON, each reference replaced by its recorded description:
    ``<element role="tab" name="Specs" context=["tablist"], was e21>``."""

    args: Any = json.loads(json.dumps(dict(step.args), default=str))
    descriptions: dict[str, str] = {}
    for index, binding in enumerate(step.bindings):
        target: Any = args
        for part in binding.path[:-1]:
            target = target[part] if isinstance(target, (dict, list)) else None
        last = binding.path[-1]
        placeholder = f"__element_{index}__"
        fields = " ".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}"
            for key, value in binding.descriptor.items()
            if value not in ("", [], None)
        )
        descriptions[json.dumps(placeholder)] = f"<element {fields}, was {binding.value}>"
        if isinstance(target, dict) and isinstance(last, str) and last in target:
            target[last] = placeholder
        elif isinstance(target, list) and isinstance(last, int) and 0 <= last < len(target):
            target[last] = placeholder
    text = json.dumps(args, ensure_ascii=False, indent=2)
    for placeholder, description in descriptions.items():
        text = text.replace(placeholder, description)
    return text


class PlaybackFactory:
    """:class:`~src.contracts.StepSourceFactory` over the session's recording store."""

    def __init__(
        self,
        store: RecordingStore,
        adapter: PlaybackAdapter,
        settings: ReplaySettings,
        *,
        retargeter: StepRetargeter | None = None,
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._settings = settings
        self._retargeter = retargeter

    def build(
        self,
        request: PlaybackRequest,
        *,
        task: str,
        budget: RunBudget,
        max_stage: LadderStage,
        emit: EventCallback,
    ) -> PlaybackSource:
        for pattern in (request.select, *request.ignore):
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"invalid playback regex {pattern!r}: {exc}") from exc
        return PlaybackSource(
            self._store.load(request.recording),
            request,
            self._adapter,
            self._settings,
            task=task,
            budget=budget,
            max_stage=max_stage,
            emit=emit,
            retargeter=self._retargeter,
        )


class TextPlaybackAdapter:
    """:class:`~src.contracts.PlaybackAdapter` for tools whose output is plain text."""

    def __init__(self, settings: ReplaySettings | None = None) -> None:
        self._settings = settings or ReplaySettings()

    def describe(self, args: Mapping[str, Any], page_texts: Sequence[str]) -> list[Binding]:
        return []

    def rebind(
        self,
        args: Mapping[str, Any],
        bindings: Sequence[Binding],
        page_texts: Sequence[str],
        *,
        verify: bool = False,
    ) -> BindResult:
        if bindings:
            return BindResult(None, "plain-text tools have no element references")
        return BindResult(dict(args))

    def checkpoint(self, text: str) -> Mapping[str, Any]:
        words = str(text or "").split()
        size = self._settings.text_shingle_words
        shingles: dict[str, int] = {}
        for start in range(max(1, len(words) - size + 1)):
            chunk = " ".join(words[start : start + size])
            if chunk:
                key = fingerprint_key(chunk)
                shingles[key] = shingles.get(key, 0) + 1
        return {"shingles": shingles}

    def similarity(self, recorded: Mapping[str, Any], current: Mapping[str, Any]) -> float:
        return multiset_similarity(
            dict(recorded.get("shingles") or {}), dict(current.get("shingles") or {})
        )

    def region(self, text: str, select: str | None, ignore: Sequence[str]) -> str:
        return select_region(text, select, ignore)


__all__ = [
    "PlaybackFactory",
    "PlaybackSource",
    "Retargeter",
    "StepRetargeter",
    "TextPlaybackAdapter",
    "ToolDefResolver",
]
