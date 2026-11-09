"""The pure tier-escalation decision of the model cascade (``guards.tier_escalation``)."""

from __future__ import annotations

from typing import Any

import pytest

from src.agent_loop.execution.guards import escalation_possible, tier_escalation
from src.agent_loop.execution.state import LoopState, TierMarks
from src.config import CascadeSettings

CASCADE = CascadeSettings()


def state(**fields: Any) -> LoopState:
    return LoopState().apply(fields)


def escalate(current: LoopState, settings: CascadeSettings = CASCADE, tiers: int = 3) -> tuple[dict[str, Any], str | None]:
    return tier_escalation(current, settings, tier_count=tiers)


def test_nothing_happened_stays_on_the_tier() -> None:
    assert escalate(state()) == ({}, None)


@pytest.mark.parametrize(
    "fields, reason",
    [
        ({"tier_signal": "no_action"}, "no_action"),
        ({"progress_blocks": 1}, "progress_block"),
        ({"consecutive_failures": 2}, "failures"),
        ({"replan_count": 1}, "replans"),
    ],
)
def test_each_reason_goes_up_one_tier(fields: dict[str, Any], reason: str) -> None:
    update, why = escalate(state(**fields))

    assert why == reason
    assert update["model_tier"] == 1
    assert update["tier_signal"] == ""
    # The new tier gets a fresh failure window (U64).
    assert update["consecutive_failures"] == 0


def test_the_reasons_are_checked_in_order() -> None:
    update, why = escalate(
        state(tier_signal="no_action", progress_blocks=1, consecutive_failures=5, replan_count=3)
    )

    assert why == "no_action"
    assert update["model_tier"] == 1


def test_counts_are_taken_since_the_last_tier_change() -> None:
    update, _ = escalate(state(progress_blocks=1, replan_count=2))
    after = state(model_tier=1, progress_blocks=1, replan_count=2).apply(
        {"tier_marks": update["tier_marks"]}
    )

    assert update["tier_marks"] == TierMarks(replans=2, progress_blocks=1)
    assert escalate(after) == ({}, None)
    assert escalate(after.apply({"progress_blocks": 2}))[1] == "progress_block"
    assert escalate(after.apply({"replan_count": 3}))[1] == "replans"


def test_failures_below_the_threshold_do_not_go_up() -> None:
    assert escalate(state(consecutive_failures=1)) == ({}, None)


def test_thresholds_and_switches_come_from_the_settings() -> None:
    quiet = CascadeSettings(
        escalate_after_failures=4,
        escalate_after_replans=2,
        escalate_on_progress_block=False,
        escalate_on_no_action=False,
    )

    assert escalate(state(consecutive_failures=3, replan_count=1, progress_blocks=1), quiet) == ({}, None)
    assert escalate(state(tier_signal="no_action"), quiet)[1] is None
    assert escalate(state(consecutive_failures=4), quiet)[1] == "failures"
    assert escalate(state(replan_count=2), quiet)[1] == "replans"


def test_an_ignored_no_action_signal_is_still_cleared() -> None:
    quiet = CascadeSettings(escalate_on_no_action=False)

    assert escalate(state(tier_signal="no_action"), quiet) == ({"tier_signal": ""}, None)


def test_the_top_tier_never_goes_further() -> None:
    top = state(model_tier=2, progress_blocks=1, tier_signal="no_action")

    assert escalate(top, tiers=3) == ({"tier_signal": ""}, None)
    assert escalate(state(progress_blocks=1), tiers=1) == ({}, None)


def test_a_step_source_stage_never_escalates() -> None:
    assert escalate(state(ladder_stage="playback", progress_blocks=1)) == ({}, None)
    assert escalate(state(ladder_stage="retarget", replan_count=4)) == ({}, None)


def test_escalation_possible_needs_a_higher_tier_and_the_agent() -> None:
    assert escalation_possible(state(), tier_count=2)
    assert not escalation_possible(state(), tier_count=1)
    assert not escalation_possible(state(model_tier=1), tier_count=2)
    assert not escalation_possible(state(ladder_stage="playback"), tier_count=2)


def test_failures_before_the_mark_do_not_count() -> None:
    marked = state(tier_marks=TierMarks(failures=1))

    assert escalate(marked.apply({"consecutive_failures": 2})) == ({}, None)
    assert escalate(marked.apply({"consecutive_failures": 3}))[1] == "failures"


def test_a_success_after_the_mark_clears_it() -> None:
    marked = state(tier_marks=TierMarks(replans=1, failures=2), replan_count=1)

    update, why = escalate(marked.apply({"consecutive_failures": 0}))

    assert why is None
    assert update == {"tier_marks": TierMarks(replans=1, failures=0)}
