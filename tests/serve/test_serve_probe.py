"""Stage 0: a model-free probe decides whether the job's main task runs at all."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.serve.dispatcher import TriggerRef
from src.serve.state import JobStateStore
from tests.serve.serve_fakes import FakeWorker, Rig, job, outcome

HOURLY = TriggerRef("interval", "hourly")
PROBE = {
    "steps": [{"tool": "browser_navigate", "args": {"url": "https://shop.example/1"}}, {"tool": "browser_snapshot"}],
    "select": "(?s)Price.*?USD",
    "ignore": [r"\d+ reviews"],
}


def probed(answer: str):
    return outcome("completed", answer=answer, stage="playback", calls=0, tokens=0, cost=0.0,
                   stop_reason="playback_completed")


def rig(tmp_path: Path, *results, **fields) -> Rig:
    return Rig(tmp_path, FakeWorker(*results), jobs={"price-watch": job(probe=PROBE, domains=["shop.example"], **fields)},
               notify_max_chars=200)


@pytest.mark.asyncio
async def test_the_probe_runs_model_free_and_isolated(tmp_path: Path) -> None:
    stand = rig(tmp_path, probed("Price 10 USD"), outcome())

    await stand.dispatcher.fire("price-watch", HOURLY)
    await stand.drain()

    (_, probe), _main = stand.worker.calls
    assert probe.max_stage == "playback"
    assert probe.carry_context is False
    assert probe.allowed_domains == ("shop.example",)
    assert probe.playback.answer == "output"
    assert probe.playback.select == "(?s)Price.*?USD" and probe.playback.ignore == (r"\d+ reviews",)
    assert [step.tool for step in probe.playback.steps] == ["browser_navigate", "browser_snapshot"]
    assert probe.playback.steps[0].args == {"url": "https://shop.example/1"}
    assert probe.labels["kind"] == "probe"


@pytest.mark.asyncio
async def test_the_first_probe_is_a_change_and_runs_the_main_task(tmp_path: Path) -> None:
    stand = rig(tmp_path, probed("Price 10 USD"), outcome(answer="It costs 10 USD."))

    fired = await stand.dispatcher.fire("price-watch", HOURLY)
    await stand.drain()

    report = stand.ledger.find(fired.run_id)
    assert (report.status, report.changed) == ("completed", True)
    assert [task.kind for task in report.tasks] == ["probe", "main"]
    change, = [note for note in stand.channel.sent if note.kind == "change"]
    assert "Price 10 USD" in change.body
    assert JobStateStore(tmp_path).load("price-watch").last_region == "Price 10 USD"


@pytest.mark.asyncio
async def test_an_unchanged_probe_costs_nothing_and_says_nothing(tmp_path: Path) -> None:
    stand = rig(tmp_path, probed("Price 10 USD"), outcome(), probed("Price 10 USD"))
    await stand.dispatcher.fire("price-watch", HOURLY)
    await stand.drain()
    stand.channel.sent.clear()

    fired = await stand.dispatcher.fire("price-watch", HOURLY)
    await stand.drain()

    report = stand.ledger.find(fired.run_id)
    assert (report.status, report.changed, report.stage_reached) == ("unchanged", False, "probe")
    assert [task.kind for task in report.tasks] == ["probe"]
    assert report.usage["model_calls"] == 0
    assert len(stand.worker.calls) == 3  # probe, main, probe: no second main task
    assert stand.channel.sent == []


@pytest.mark.asyncio
async def test_a_changed_region_notifies_what_was_and_what_is(tmp_path: Path) -> None:
    stand = rig(tmp_path, probed("Price 10 USD"), outcome(), probed("Price 12 USD"), outcome(answer="Now 12."))
    await stand.dispatcher.fire("price-watch", HOURLY)
    await stand.drain()
    stand.channel.sent.clear()

    fired = await stand.dispatcher.fire("price-watch", HOURLY)
    await stand.drain()

    assert stand.ledger.find(fired.run_id).changed is True
    change, = [note for note in stand.channel.sent if note.kind == "change"]
    assert "Price 10 USD" in change.body and "Price 12 USD" in change.body


@pytest.mark.asyncio
async def test_a_failed_probe_fails_the_run_without_a_main_task(tmp_path: Path) -> None:
    stand = rig(tmp_path, outcome("blocked", stop_reason="ladder_cap", calls=0))

    fired = await stand.dispatcher.fire("price-watch", HOURLY)
    await stand.drain()

    report = stand.ledger.find(fired.run_id)
    assert (report.status, report.stop_reason) == ("failed", "ladder_cap")
    assert len(stand.worker.calls) == 1
    assert [note.kind for note in stand.channel.sent] == ["failure"]


@pytest.mark.asyncio
async def test_a_failed_main_task_keeps_the_old_fingerprint_so_the_next_tick_retries(tmp_path: Path) -> None:
    stand = rig(tmp_path, probed("Price 10 USD"), outcome("failed"), probed("Price 10 USD"), outcome())

    await stand.dispatcher.fire("price-watch", HOURLY)
    await stand.drain()
    second = await stand.dispatcher.fire("price-watch", HOURLY)
    await stand.drain()

    assert stand.ledger.find(second.run_id).status == "completed"
    assert len(stand.worker.calls) == 4


@pytest.mark.asyncio
async def test_the_report_adds_up_the_probe_and_the_main_task(tmp_path: Path) -> None:
    stand = rig(tmp_path, probed("Price 10 USD"), outcome(calls=2, tokens=300, cost=0.03))

    fired = await stand.dispatcher.fire("price-watch", HOURLY)
    await stand.drain()

    usage = stand.ledger.find(fired.run_id).usage
    assert (usage["model_calls"], usage["input_tokens"] + usage["output_tokens"]) == (2, 300)
    assert usage["cost"] == pytest.approx(0.03)
