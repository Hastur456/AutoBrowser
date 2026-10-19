from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from src.ui.tui.transcript import (
    ApprovalClosed,
    ApprovalShown,
    Block,
    CommandOutput,
    KeyAction,
    Notice,
    StreamGap,
    StreamReset,
    Transcript,
    UserSubmitted,
    approval_choices,
    reduce,
)
from src.ui.views import FeedItem

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


class Feed:
    """Feed items with growing sequences, like one session's event stream."""

    def __init__(self) -> None:
        self.sequence = 0

    def __call__(self, kind: str, *, at: float = 0.0, **fields: object) -> FeedItem:
        self.sequence += 1
        return FeedItem(
            kind=kind,  # type: ignore[arg-type]
            event_type=str(fields.pop("event_type", kind)),
            sequence=self.sequence,
            task_id="t1",
            timestamp=T0 + timedelta(seconds=at),
            **fields,  # type: ignore[arg-type]
        )


@dataclass(frozen=True)
class Ticket:
    ticket_id: str = "k1"
    task_id: str = "t1"
    tool: str = "click"
    server: str = "srv"
    domain: str = "shop.test"
    reason: str = "checkout"
    rule_id: str = "ask-checkout"
    args_preview: str = "{}"
    allow_session: bool = True
    created_at: datetime = T0
    deadline: datetime = T0 + timedelta(seconds=60)


@dataclass(frozen=True)
class Resolution:
    ticket_id: str = "k1"
    answer: str = "once"
    via: str = "tui"
    resolved_at: datetime = T0 + timedelta(seconds=3)


def run(messages: list[object], transcript: Transcript | None = None) -> tuple[Transcript, list[Block]]:
    transcript = transcript or Transcript()
    frozen: list[Block] = []
    for message in messages:
        transcript, newly = reduce(transcript, message)  # type: ignore[arg-type]
        frozen.extend(newly)
    return transcript, frozen


def test_user_input_notices_and_command_output_freeze_at_once() -> None:
    transcript, frozen = run(
        [UserSubmitted("hello"), Notice("note", "warn"), CommandOutput("MCP", ("a", "b")), StreamGap(5)]
    )

    assert [b.kind for b in frozen] == ["user", "notice", "command_output", "notice"]
    assert all(b.state == "frozen" for b in frozen)
    assert frozen[1].level == "warn" and frozen[3].missed == 5
    assert transcript.live == ()
    assert len({b.block_id for b in frozen}) == 4


def test_a_call_lives_until_its_observation_then_freezes_with_all_items() -> None:
    feed = Feed()
    transcript, frozen = run(
        [
            feed("goal_started"),
            feed("call", tool="nav"),
            feed("gate", decision="ask", origin="permission"),
            feed("tool_started"),
            feed("tool_finished", status="success", at=1.5),
        ]
    )

    assert frozen == []
    call = transcript.block("call")
    assert call is not None and [i.kind for i in call.items] == [
        "call",
        "gate",
        "tool_started",
        "tool_finished",
    ]
    assert transcript.task is not None and transcript.task.calls == 1

    transcript, frozen = run([feed("observation", text="obs")], transcript)

    assert [b.kind for b in frozen] == ["call"]
    assert frozen[0].state == "frozen" and frozen[0].items[-1].kind == "observation"
    assert transcript.block("call") is None


def test_a_new_call_freezes_the_previous_one() -> None:
    feed = Feed()
    transcript, frozen = run([feed("goal_started"), feed("call", tool="a"), feed("call", tool="b")])

    assert [b.items[0].tool for b in frozen] == ["a"]
    live = transcript.block("call")
    assert live is not None and live.items[0].tool == "b"


def test_plan_follows_turns_and_freezes_before_the_outcome() -> None:
    feed = Feed()
    transcript, _ = run(
        [
            feed("goal_started"),
            feed("plan", steps=("one", "two", "three")),
            feed("turn", turn=2, current_step=1),
            feed("model", phase="agent"),
        ]
    )

    plan = transcript.block("plan")
    assert plan is not None and (plan.steps, plan.current_step) == (("one", "two", "three"), 1)
    assert transcript.task is not None
    assert (transcript.task.turn, transcript.task.phase) == (2, "agent")

    transcript, frozen = run(
        [feed("call", tool="x"), feed("outcome", status="completed", text="ok", turns=2, at=30)],
        transcript,
    )

    assert [b.kind for b in frozen] == ["call", "plan", "outcome"]
    assert frozen[1].current_step == 3  # completed: every step done
    assert frozen[2].elapsed_seconds == 30.0
    assert transcript.task is None and transcript.live == ()


def test_replayed_events_are_not_folded_twice_until_a_reset() -> None:
    first = FeedItem(kind="call", event_type="action.proposed", sequence=7, task_id="t1", tool="a")
    transcript, _ = run([first])
    again, frozen = run([first], transcript)

    assert again == transcript and frozen == []

    # After a session reset sequences start over, so the same number is new again.
    reset, frozen = run([StreamReset(), first], transcript)
    assert reset.last_sequence == 7
    assert [b.items[0].tool for b in frozen] == ["a"]
    assert reset.block("call") is not None


def test_detail_toggle_expands_live_and_future_calls() -> None:
    feed = Feed()
    transcript, _ = run([feed("call", tool="a"), KeyAction("toggle_detail")])

    assert transcript.detail == "verbose"
    assert transcript.block("call").expanded  # type: ignore[union-attr]
    transcript, _ = run([feed("call", tool="b")], transcript)
    assert transcript.block("call").expanded  # type: ignore[union-attr]
    transcript, _ = run([KeyAction("toggle_plan")], transcript)
    assert transcript.show_plan is False


def test_approval_card_choices_and_resolution() -> None:
    transcript, _ = run([ApprovalShown(Ticket())])
    card = transcript.block("approval")
    assert card is not None and approval_choices(card) == ("once", "session", "deny")

    transcript, _ = run([KeyAction("approval_down"), KeyAction("approval_down")], transcript)
    assert transcript.block("approval").choice == 2  # type: ignore[union-attr]
    transcript, _ = run([KeyAction("approval_down")], transcript)
    assert transcript.block("approval").choice == 0  # type: ignore[union-attr]
    transcript, _ = run([KeyAction("approval_up")], transcript)
    assert transcript.block("approval").choice == 2  # type: ignore[union-attr]

    other, frozen = run([ApprovalClosed(Resolution(ticket_id="nope"))], transcript)
    assert frozen == [] and other.block("approval") is not None

    transcript, frozen = run([ApprovalClosed(Resolution())], transcript)
    assert [b.kind for b in frozen] == ["approval"]
    assert frozen[0].resolution is not None and transcript.block("approval") is None


def test_a_ticket_without_a_session_grant_offers_two_choices() -> None:
    transcript, _ = run([ApprovalShown(Ticket(allow_session=False)), KeyAction("approval_down")])
    card = transcript.block("approval")

    assert card is not None and approval_choices(card) == ("once", "deny")
    assert card.choice == 1


def test_an_outcome_closes_a_pending_card_and_unknown_events_are_quiet() -> None:
    feed = Feed()
    transcript, frozen = run(
        [feed("goal_started"), ApprovalShown(Ticket()), feed("other", event_type="x.y")]
    )
    assert frozen == []

    transcript, frozen = run([feed("outcome", status="cancelled")], transcript)
    assert [b.kind for b in frozen] == ["approval", "outcome"]
    assert transcript.live == ()
