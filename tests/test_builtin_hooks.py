"""Unit tests for the generic hook handlers in :mod:`src.harness.builtin_hooks`."""

from __future__ import annotations

import pytest

from src.contracts import HookEvent, HookResult
from src.harness.builtin_hooks import grounded_final_answer

NBSP = "\u00a0"
NARROW_NBSP = "\u202f"
THIN = "\u2009"


def stop_event(answer: str, *evidence: str, task: str = "Find a jacket.") -> HookEvent:
    return HookEvent(
        name="stop",
        session_id="session-1",
        goal_id="task-1",
        task_id="task-1",
        task=task,
        final_answer=answer,
        evidence=evidence,
    )


async def check(answer: str, *evidence: str, task: str = "Find a jacket.", **options: int) -> HookResult | None:
    return await grounded_final_answer(**options)(stop_event(answer, *evidence, task=task))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "evidence"),
    [
        ("Jacket A costs 1299 ₽.", "- link \"Jacket A\" ref=e10\n- text \"1 299 ₽\""),
        ("Jacket A costs 1 299 ₽.", "price: 1299"),
        (f"Jacket A costs 1{NBSP}299 ₽.", "price: 1 299"),
        (f"Jacket A costs 1{NARROW_NBSP}299 ₽.", f"price: 1{THIN}299"),
        ("Rating 4,5 of 5.", "rating 4.5 / 5"),
        ("Rating 4.50.", "rating 4,5; 5"),
        ("It costs 1299.00.", "1 299 ₽"),
        ("Jacket A is the first result.", "- link \"Jacket A\" ref=e10"),
    ],
)
async def test_numbers_found_in_the_evidence_are_grounded(answer: str, evidence: str) -> None:
    assert await check(answer, evidence) is None


@pytest.mark.asyncio
async def test_a_number_missing_from_the_evidence_is_rejected_and_named() -> None:
    result = await check("Jacket A costs 1 499 ₽, Jacket B 2 000 ₽.", "Jacket A 1 499 ₽ | Jacket B 1 999 ₽")

    assert result is not None
    assert result.decision == "deny"
    assert "2 000" in result.reason
    assert "1 499" not in result.reason


@pytest.mark.asyncio
async def test_numbers_from_the_task_are_not_checked() -> None:
    result = await check(
        "Here are 3 jackets: A, B, C.",
        "A B C",
        task="Find 3 jackets under 5000 rubles.",
    )

    assert result is None


@pytest.mark.asyncio
async def test_list_markers_and_element_refs_are_not_claimed_values() -> None:
    answer = "1. Jacket A (ref e10)\n2) Jacket B\n3. Jacket C"

    assert await check(answer, "Jacket A Jacket B Jacket C") is None


@pytest.mark.asyncio
async def test_evidence_spread_over_observation_and_snapshot_counts() -> None:
    assert await check("Total 12 items for 3 400 ₽.", "12 items", "sum: 3400") is None


@pytest.mark.asyncio
async def test_without_evidence_any_number_is_ungrounded() -> None:
    result = await check("It costs 999.")

    assert result is not None and result.decision == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["", "   "])
async def test_an_empty_answer_is_rejected(answer: str) -> None:
    result = await check(answer, "anything")

    assert result is not None and result.decision == "deny"
    assert "empty or too short" in result.reason


@pytest.mark.asyncio
async def test_min_chars_rejects_a_too_short_answer() -> None:
    assert (await check("OK", "OK", min_chars=5)) is not None
    assert (await check("Found it.", "Found it.", min_chars=5)) is None


def test_min_chars_must_not_be_negative() -> None:
    with pytest.raises(ValueError):
        grounded_final_answer(min_chars=-1)
