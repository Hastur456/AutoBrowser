"""The pure block model of the terminal transcript (§5.5, §6.2 of the terminal UI design).

A :class:`Transcript` holds only what is still *live* (the plan, the current call, an
approval card) plus the progress of the running task. :func:`reduce` folds one
:data:`UiMessage` into a new transcript and returns the blocks that became final: the render
pump prints those into the scrollback once and never touches them again.

Every decision is made by the feed item ``kind``; tool names are carried as data only.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal, Protocol

from src.ui.views import FeedItem, PastTask

BlockKind = Literal[
    "banner",
    "user",
    "plan",
    "call",
    "notice",
    "approval",
    "outcome",
    "command_output",
    "picker",
]
BlockState = Literal["live", "frozen"]
Detail = Literal["compact", "verbose"]
NoticeLevel = Literal["info", "warn", "error"]
KeyActionName = Literal[
    "toggle_detail", "toggle_plan", "approval_up", "approval_down", "picker_up", "picker_down"
]

#: Feed kinds that belong to the current call block.
_CALL_KINDS = frozenset(
    {"gate", "approval_requested", "approval_resolved", "tool_started", "tool_finished"}
)

#: The three choices of an approval card, in display order.
APPROVAL_CHOICES: tuple[str, ...] = ("once", "session", "deny")


class ApprovalTicketView(Protocol):
    """What the card needs from a pending approval (``src.contracts.ApprovalTicket``)."""

    @property
    def ticket_id(self) -> str: ...
    @property
    def task_id(self) -> str: ...
    @property
    def tool(self) -> str: ...
    @property
    def server(self) -> str: ...
    @property
    def domain(self) -> str: ...
    @property
    def reason(self) -> str: ...
    @property
    def rule_id(self) -> str: ...
    @property
    def args_preview(self) -> str: ...
    @property
    def allow_session(self) -> bool: ...
    @property
    def created_at(self) -> datetime: ...
    @property
    def deadline(self) -> datetime: ...


class ApprovalResolutionView(Protocol):
    """How a pending approval ended (``src.contracts.ApprovalResolution``)."""

    @property
    def ticket_id(self) -> str: ...
    @property
    def answer(self) -> str: ...
    @property
    def via(self) -> str: ...
    @property
    def resolved_at(self) -> datetime: ...


@dataclass(frozen=True)
class UserSubmitted:
    text: str


@dataclass(frozen=True)
class Notice:
    text: str
    level: NoticeLevel = "info"


@dataclass(frozen=True)
class CommandOutput:
    """The result of a slash command: a title and preformatted rows."""

    title: str
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class StreamGap:
    """The subscriber fell behind and ``missed`` events were dropped."""

    missed: int


@dataclass(frozen=True)
class StreamReset:
    """The session was reset; event sequences start over."""


@dataclass(frozen=True)
class TaskSettled:
    """The session reports no running task, but no outcome reached the feed (a stream gap
    or a dead forwarder): freeze what is live and stop the spinner."""


@dataclass(frozen=True)
class ApprovalShown:
    ticket: ApprovalTicketView


@dataclass(frozen=True)
class ApprovalClosed:
    resolution: ApprovalResolutionView


@dataclass(frozen=True)
class PickerOption:
    """One choice of a picker: ``value`` is what choosing it acts on."""

    value: str
    label: str
    detail: str = ""


@dataclass(frozen=True)
class PickerShown:
    """Open a choice list in the live zone (``/resume`` without an id); ``picker_id`` says
    what Enter acts on."""

    picker_id: str
    title: str
    options: tuple[PickerOption, ...]


@dataclass(frozen=True)
class PickerClosed:
    picker_id: str


@dataclass(frozen=True)
class PastConversation:
    """Replay a resumed session's finished tasks into the scrollback: the task as the
    user's line, then its outcome."""

    tasks: tuple[PastTask, ...]


@dataclass(frozen=True)
class KeyAction:
    name: KeyActionName


UiMessage = (
    FeedItem
    | UserSubmitted
    | Notice
    | CommandOutput
    | StreamGap
    | StreamReset
    | TaskSettled
    | ApprovalShown
    | ApprovalClosed
    | PickerShown
    | PickerClosed
    | PastConversation
    | KeyAction
)


@dataclass(frozen=True)
class Block:
    """One transcript block; ``items`` are its feed items in arrival order."""

    block_id: str
    kind: BlockKind
    task_id: str = ""
    state: BlockState = "live"
    expanded: bool = False
    items: tuple[FeedItem, ...] = ()
    text: str = ""
    lines: tuple[str, ...] = ()
    level: NoticeLevel = "info"
    #: Notice blocks of a stream gap: how many events were dropped.
    missed: int = 0
    ticket: ApprovalTicketView | None = None
    resolution: ApprovalResolutionView | None = None
    #: Approval cards: index into :func:`approval_choices`; pickers: into ``options``.
    choice: int = 0
    #: Picker blocks: what the list is for and its choices.
    picker_id: str = ""
    options: tuple[PickerOption, ...] = ()
    #: Plan blocks: the steps and the index of the active one.
    steps: tuple[str, ...] = ()
    step_statuses: tuple[str, ...] = ()
    current_step: int | None = None
    #: Outcome blocks: tool calls of the task and its wall-clock time.
    calls: int = 0
    elapsed_seconds: float | None = None


@dataclass(frozen=True)
class TaskProgress:
    """The running task as the spinner and the status line show it."""

    task_id: str
    started_at: datetime | None = None
    turn: int | None = None
    phase: str = ""
    calls: int = 0
    steps: tuple[str, ...] = ()
    current_step: int | None = None


@dataclass(frozen=True)
class Transcript:
    live: tuple[Block, ...] = ()
    detail: Detail = "compact"
    show_plan: bool = True
    task: TaskProgress | None = None
    next_id: int = 1
    #: The highest event sequence folded in: session sequences only grow, so a replayed or
    #: re-subscribed stream never duplicates blocks.
    last_sequence: int = 0

    def block(self, kind: BlockKind) -> Block | None:
        """The live block of ``kind``, if any (there is at most one of each)."""

        for block in self.live:
            if block.kind == kind:
                return block
        return None


def reduce(transcript: Transcript, message: UiMessage) -> tuple[Transcript, list[Block]]:
    """The new transcript and the blocks to freeze into the scrollback, in print order."""

    if isinstance(message, FeedItem):
        return _reduce_feed(transcript, message)
    if isinstance(message, UserSubmitted):
        return _freeze_new(transcript, "user", text=message.text)
    if isinstance(message, Notice):
        return _freeze_new(transcript, "notice", text=message.text, level=message.level)
    if isinstance(message, CommandOutput):
        return _freeze_new(transcript, "command_output", text=message.title, lines=message.lines)
    if isinstance(message, StreamGap):
        return _freeze_new(transcript, "notice", level="warn", missed=message.missed)
    if isinstance(message, StreamReset):
        return replace(transcript, last_sequence=0), []
    if isinstance(message, TaskSettled):
        if transcript.task is None:
            return transcript, []
        settled, frozen = _freeze_kinds(transcript, [], ("call", "approval", "plan"))
        return replace(settled, task=None), frozen
    if isinstance(message, ApprovalShown):
        transcript, block = _new_block(
            transcript, "approval", task_id=message.ticket.task_id, ticket=message.ticket
        )
        return replace(transcript, live=(*transcript.live, block)), []
    if isinstance(message, ApprovalClosed):
        return _close_approval(transcript, message.resolution)
    if isinstance(message, PickerShown):
        kept = tuple(b for b in transcript.live if b.kind != "picker")
        transcript, block = _new_block(
            replace(transcript, live=kept),
            "picker",
            text=message.title,
            picker_id=message.picker_id,
            options=message.options,
        )
        return replace(transcript, live=(*transcript.live, block)), []
    if isinstance(message, PickerClosed):
        live = tuple(
            b for b in transcript.live if not (b.kind == "picker" and b.picker_id == message.picker_id)
        )
        return replace(transcript, live=live), []
    if isinstance(message, PastConversation):
        return _replay_past(transcript, message.tasks)
    return _reduce_key(transcript, message), []


def _replay_past(transcript: Transcript, tasks: tuple[PastTask, ...]) -> tuple[Transcript, list[Block]]:
    frozen: list[Block] = []
    for past in tasks:
        transcript, user = _new_block(transcript, "user", state="frozen", text=past.task)
        outcome_item = FeedItem(
            kind="outcome",
            event_type=f"goal.{past.status}",
            status=past.status,
            text=past.final_answer,
            turns=past.turns,
            task=past.task,
        )
        transcript, outcome = _new_block(transcript, "outcome", state="frozen", items=(outcome_item,))
        frozen.extend((user, outcome))
    return transcript, frozen


def _reduce_feed(transcript: Transcript, item: FeedItem) -> tuple[Transcript, list[Block]]:
    if item.sequence:
        if item.sequence <= transcript.last_sequence:
            return transcript, []
        transcript = replace(transcript, last_sequence=item.sequence)

    task = transcript.task
    if item.kind == "goal_started":
        frozen: list[Block] = []
        transcript, frozen = _freeze_kinds(transcript, frozen, ("call", "plan"))
        return replace(
            transcript,
            task=TaskProgress(task_id=item.task_id, started_at=item.timestamp),
        ), frozen
    if item.kind == "plan":
        task = task or TaskProgress(task_id=item.task_id)
        current = item.current_step
        if current is None:
            current = task.current_step if task.current_step is not None else 0
        task = replace(task, steps=item.steps, current_step=current)
        transcript = replace(transcript, task=task)
        plan = transcript.block("plan")
        if plan is None:
            transcript, plan = _new_block(transcript, "plan", task_id=item.task_id)
            transcript = replace(transcript, live=(plan, *transcript.live))
        updated = replace(
            plan,
            steps=item.steps,
            step_statuses=item.step_statuses,
            current_step=current,
            items=(item,),
        )
        return _swap(transcript, plan, updated), []
    if item.kind == "turn":
        task = task or TaskProgress(task_id=item.task_id)
        task = replace(task, turn=item.turn, current_step=item.current_step)
        transcript = replace(transcript, task=task)
        plan = transcript.block("plan")
        if plan is not None and item.current_step is not None:
            transcript = _swap(transcript, plan, replace(plan, current_step=item.current_step))
        return transcript, []
    if item.kind == "model":
        if task is not None:
            transcript = replace(transcript, task=replace(task, phase=item.phase))
        return transcript, []
    if item.kind == "call":
        frozen = []
        transcript, frozen = _freeze_kinds(transcript, frozen, ("call",))
        transcript, block = _new_block(
            transcript,
            "call",
            task_id=item.task_id,
            items=(item,),
            expanded=transcript.detail == "verbose",
        )
        if task is not None:
            transcript = replace(transcript, task=replace(task, phase="tool"))
        return replace(transcript, live=(*transcript.live, block)), frozen
    if item.kind in _CALL_KINDS:
        call = transcript.block("call")
        if call is None:
            transcript, call = _new_block(
                transcript, "call", task_id=item.task_id, expanded=transcript.detail == "verbose"
            )
            transcript = replace(transcript, live=(*transcript.live, call))
        transcript = _swap(transcript, call, replace(call, items=(*call.items, item)))
        if item.kind == "tool_finished" and task is not None:
            transcript = replace(transcript, task=replace(task, calls=task.calls + 1))
        return transcript, []
    if item.kind == "observation":
        call = transcript.block("call")
        if call is None:
            return transcript, []
        transcript = _swap(transcript, call, replace(call, items=(*call.items, item)))
        return _freeze_kinds(transcript, [], ("call",))
    if item.kind == "outcome":
        return _reduce_outcome(transcript, item)
    if item.kind == "memory":
        return _freeze_new(
            transcript, "notice", text=f"memory {item.status}: {item.text}".rstrip(": ")
        )
    # ``other``: unknown event types are only worth a line in verbose mode.
    if transcript.detail == "verbose":
        return _freeze_new(transcript, "notice", text=item.event_type)
    return transcript, []


def _reduce_outcome(transcript: Transcript, item: FeedItem) -> tuple[Transcript, list[Block]]:
    task = transcript.task
    frozen: list[Block] = []
    transcript, frozen = _freeze_kinds(transcript, frozen, ("call", "approval"))
    plan = transcript.block("plan")
    if plan is not None:
        transcript = replace(transcript, live=tuple(b for b in transcript.live if b is not plan))
        if plan.steps:
            if item.status == "completed":
                plan = replace(
                    plan,
                    current_step=len(plan.steps),
                    step_statuses=("completed",) * len(plan.steps),
                )
            frozen.append(replace(plan, state="frozen"))
    elapsed: float | None = None
    if task is not None and task.started_at is not None and item.timestamp is not None:
        elapsed = max((item.timestamp - task.started_at).total_seconds(), 0.0)
    transcript, block = _new_block(
        transcript,
        "outcome",
        task_id=item.task_id,
        items=(item,),
        state="frozen",
        calls=task.calls if task is not None else 0,
        elapsed_seconds=elapsed,
    )
    frozen.append(block)
    return replace(transcript, task=None), frozen


def _close_approval(
    transcript: Transcript, resolution: ApprovalResolutionView
) -> tuple[Transcript, list[Block]]:
    for block in transcript.live:
        if block.kind == "approval" and block.ticket is not None and (
            block.ticket.ticket_id == resolution.ticket_id
        ):
            remaining = tuple(b for b in transcript.live if b is not block)
            closed = replace(block, state="frozen", resolution=resolution)
            return replace(transcript, live=remaining), [closed]
    return transcript, []


def _reduce_key(transcript: Transcript, action: KeyAction) -> Transcript:
    if action.name == "toggle_detail":
        detail: Detail = "verbose" if transcript.detail == "compact" else "compact"
        live = tuple(
            replace(b, expanded=detail == "verbose") if b.kind == "call" else b
            for b in transcript.live
        )
        return replace(transcript, detail=detail, live=live)
    if action.name == "toggle_plan":
        return replace(transcript, show_plan=not transcript.show_plan)
    if action.name in ("picker_up", "picker_down"):
        picker = transcript.block("picker")
        if picker is None or not picker.options:
            return transcript
        step = -1 if action.name == "picker_up" else 1
        return _swap(transcript, picker, replace(picker, choice=(picker.choice + step) % len(picker.options)))
    card = transcript.block("approval")
    if card is None:
        return transcript
    step = -1 if action.name == "approval_up" else 1
    choice = (card.choice + step) % len(approval_choices(card))
    return _swap(transcript, card, replace(card, choice=choice))


def approval_choices(block: Block) -> tuple[str, ...]:
    """The answers a card offers; ``session`` only when the ticket allows a grant."""

    if block.ticket is not None and not block.ticket.allow_session:
        return ("once", "deny")
    return APPROVAL_CHOICES


def _freeze_kinds(
    transcript: Transcript, frozen: list[Block], kinds: tuple[BlockKind, ...]
) -> tuple[Transcript, list[Block]]:
    keep: list[Block] = []
    for block in transcript.live:
        if block.kind in kinds:
            frozen.append(replace(block, state="frozen"))
        else:
            keep.append(block)
    return replace(transcript, live=tuple(keep)), frozen


def _freeze_new(
    transcript: Transcript, kind: BlockKind, **values: object
) -> tuple[Transcript, list[Block]]:
    transcript, block = _new_block(transcript, kind, state="frozen", **values)
    return transcript, [block]


def _new_block(transcript: Transcript, kind: BlockKind, **values: object) -> tuple[Transcript, Block]:
    block = Block(block_id=f"b{transcript.next_id}", kind=kind, **values)  # type: ignore[arg-type]
    return replace(transcript, next_id=transcript.next_id + 1), block


def _swap(transcript: Transcript, old: Block, new: Block) -> Transcript:
    return replace(transcript, live=tuple(new if b is old else b for b in transcript.live))


__all__ = [
    "APPROVAL_CHOICES",
    "ApprovalClosed",
    "PastConversation",
    "PickerClosed",
    "PickerOption",
    "PickerShown",
    "ApprovalResolutionView",
    "ApprovalShown",
    "ApprovalTicketView",
    "Block",
    "BlockKind",
    "CommandOutput",
    "Detail",
    "KeyAction",
    "Notice",
    "StreamGap",
    "StreamReset",
    "TaskProgress",
    "TaskSettled",
    "Transcript",
    "UiMessage",
    "UserSubmitted",
    "approval_choices",
    "reduce",
]
