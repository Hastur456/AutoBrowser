"""Dispatcher: queue, coalescing, cooldown, day budgets, run profiles, reports and shutdown."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from src.serve.dispatcher import TriggerRef
from src.serve.ledger import Ledger
from src.serve.state import JobStateStore
from src.ui.views import ServerView
from tests.serve.serve_fakes import (
    FakeClock,
    FakeWorker,
    RecordingChannel,
    Rig,
    job,
    outcome,
    task_event,
)

HOURLY = TriggerRef(type="interval", id="hourly")


# ----------------------------------------------------------------------------- firing


@pytest.mark.asyncio
async def test_a_fired_job_is_queued_then_run_and_reported(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    fired = await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()

    assert fired.state == "queued" and fired.run_id.startswith("run-")
    (text, profile), = rig.worker.calls
    assert text == "Open the page and report the price."
    report = rig.ledger.find(fired.run_id)
    assert report is not None
    assert (report.status, report.job_id, report.trigger) == (
        "completed", "price-watch", {"type": "interval", "id": "hourly"}
    )
    assert report.usage["model_calls"] == 1
    assert [t.kind for t in report.tasks] == ["main"]
    assert rig.types()[:2] == ["serve.trigger.fired", "serve.run.queued"]
    assert rig.payloads("serve.run.finished")[0]["run_id"] == fired.run_id


@pytest.mark.asyncio
async def test_an_unknown_job_is_a_key_error(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        await Rig(tmp_path).dispatcher.fire("nope", HOURLY)


@pytest.mark.asyncio
async def test_a_disabled_job_is_skipped(tmp_path: Path) -> None:
    rig = Rig(tmp_path, jobs={"price-watch": job(enabled=False)})

    fired = await rig.dispatcher.fire("price-watch", HOURLY)

    assert (fired.state, fired.reason) == ("skipped", "disabled")
    assert rig.payloads("serve.run.skipped")[0]["reason"] == "disabled"


@pytest.mark.asyncio
async def test_drop_coalesces_a_trigger_while_the_job_is_busy(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    first = await rig.dispatcher.fire("price-watch", HOURLY)
    second = await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()

    assert first.state == "queued"
    assert (second.state, second.reason) == ("coalesced", "coalesced")
    assert len(rig.worker.calls) == 1
    assert JobStateStore(tmp_path).load("price-watch").counters == {"coalesced": 1}


@pytest.mark.asyncio
async def test_queue_one_keeps_one_trigger_behind_a_running_one(tmp_path: Path) -> None:
    worker = FakeWorker()
    worker.gate = asyncio.Event()
    rig = Rig(tmp_path, worker, coalesce="queue_one")
    runner = asyncio.create_task(rig.dispatcher.run())

    await rig.dispatcher.fire("price-watch", HOURLY)
    await worker.started.wait()
    behind = await rig.dispatcher.fire("price-watch", HOURLY)
    third = await rig.dispatcher.fire("price-watch", HOURLY)
    worker.gate.set()
    await rig.dispatcher.stop(grace_seconds=5)
    await runner

    assert behind.state == "queued"
    assert third.state == "coalesced"
    # The queued run was dropped by the shutdown, never lost silently.
    assert [p["reason"] for p in rig.payloads("serve.run.skipped")] == ["coalesced", "shutdown"]


@pytest.mark.asyncio
async def test_the_job_cooldown_skips_a_trigger_too_soon_after_a_run(tmp_path: Path) -> None:
    rig = Rig(tmp_path, cooldown_seconds=600)

    await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()
    rig.clock.advance(60)
    early = await rig.dispatcher.fire("price-watch", HOURLY)
    rig.clock.advance(600)
    later = await rig.dispatcher.fire("price-watch", HOURLY)

    assert (early.state, early.reason) == ("skipped", "cooldown")
    assert later.state == "queued"


@pytest.mark.asyncio
async def test_a_full_queue_skips_the_trigger(tmp_path: Path) -> None:
    jobs = {name: job() for name in ("a", "b", "c")}
    rig = Rig(tmp_path, jobs=jobs, queue_size=2)

    results = [await rig.dispatcher.fire(name, HOURLY) for name in ("a", "b", "c")]

    assert [r.state for r in results] == ["queued", "queued", "skipped"]
    assert results[2].reason == "queue_full"


# ----------------------------------------------------------------------------- day budgets


@pytest.mark.asyncio
async def test_the_day_run_cap_skips_and_notifies_once_a_day(tmp_path: Path) -> None:
    rig = Rig(tmp_path, daily_max_runs=1)

    await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()
    over = await rig.dispatcher.fire("price-watch", HOURLY)
    again = await rig.dispatcher.fire("price-watch", HOURLY)

    assert (over.reason, again.reason) == ("budget", "budget")
    budget_notes = [n for n in rig.channel.sent if n.kind == "budget"]
    assert len(budget_notes) == 1

    # A restart the same day does not notify again; the next day runs again.
    restarted = Rig(tmp_path, clock=FakeClock(rig.clock.now()), daily_max_runs=1)
    assert (await restarted.dispatcher.fire("price-watch", HOURLY)).reason == "budget"
    assert restarted.channel.sent == []
    restarted.clock.advance(24 * 3600)
    assert (await restarted.dispatcher.fire("price-watch", HOURLY)).state == "queued"


@pytest.mark.asyncio
async def test_day_totals_are_restored_from_the_ledger(tmp_path: Path) -> None:
    rig = Rig(tmp_path, FakeWorker(outcome(tokens=900)), daily_max_tokens=1000)
    await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()

    restarted = Rig(tmp_path, FakeWorker(), clock=FakeClock(rig.clock.now()), daily_max_tokens=1000)
    await restarted.dispatcher.fire("price-watch", HOURLY)
    await restarted.drain()

    (_, profile), = restarted.worker.calls
    assert profile.budget.max_tokens == 100  # what is left of the day
    assert Ledger(tmp_path / "ledger.jsonl", restarted.clock.now().tzinfo).day_totals(
        date(2026, 11, 4)).tokens == 1000


@pytest.mark.asyncio
async def test_the_run_budget_is_the_smaller_of_the_job_and_the_day(tmp_path: Path) -> None:
    rig = Rig(
        tmp_path,
        jobs={"price-watch": job(budget={"max_model_calls": 5, "max_tokens": 50, "max_cost": 0.5})},
        daily_max_tokens=1000,
        daily_max_cost=0.1,
    )

    await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()

    (_, profile), = rig.worker.calls
    assert profile.budget.max_model_calls == 5
    assert profile.budget.max_tokens == 50
    assert profile.budget.max_cost == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_without_a_job_budget_the_default_run_budget_applies(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()

    (_, profile), = rig.worker.calls
    assert profile.budget.max_model_calls == 30
    assert profile.budget.max_tokens is None


# ----------------------------------------------------------------------------- run profiles


@pytest.mark.asyncio
async def test_the_run_profile_isolates_and_scopes_the_task(tmp_path: Path) -> None:
    rig = Rig(tmp_path, jobs={"price-watch": job(domains=["shop.example"], max_stage="human")})

    fired = await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()

    (_, profile), = rig.worker.calls
    assert profile.carry_context is False
    assert profile.allowed_domains == ("shop.example",)
    assert profile.max_stage == "human"
    assert profile.playback is None and profile.record_as is None
    assert profile.labels == {
        "mode": "serve", "job_id": "price-watch", "run_id": fired.run_id, "trigger": "interval:hourly",
    }


@pytest.mark.asyncio
async def test_an_existing_recording_is_replayed_and_resaved(tmp_path: Path) -> None:
    worker = FakeWorker(recordings=("price-watch",))
    rig = Rig(tmp_path, worker,
              jobs={"price-watch": job(recording="price-watch", record_on_success=True)})

    await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()

    (_, profile), = worker.calls
    assert profile.playback is not None and profile.playback.recording == "price-watch"
    assert profile.record_as == "price-watch"


@pytest.mark.asyncio
async def test_a_missing_recording_lets_the_agent_record_it(tmp_path: Path) -> None:
    rig = Rig(tmp_path, jobs={"price-watch": job(recording="price-watch", record_on_success=True)})

    await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()

    (_, profile), = rig.worker.calls
    assert profile.playback is None
    assert profile.record_as == "price-watch"


@pytest.mark.asyncio
async def test_a_run_capped_below_the_agent_without_a_recording_is_blocked_without_a_task(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path, jobs={"price-watch": job(recording="price-watch", max_stage="playback")})

    fired = await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()

    assert rig.worker.calls == []
    report = rig.ledger.find(fired.run_id)
    assert (report.status, report.stop_reason) == ("blocked", "ladder_cap")


# ----------------------------------------------------------------------------- outcomes


@pytest.mark.parametrize(
    "result, status, kind",
    [
        (outcome("completed"), "completed", None),
        (outcome("failed", stop_reason=""), "failed", "failure"),
        (outcome("blocked", stop_reason="turn_cap"), "blocked", "blocked"),
        (outcome("blocked", stop_reason="budget_exhausted"), "blocked", "budget"),
    ],
)
@pytest.mark.asyncio
async def test_outcomes_become_report_statuses_and_notifications(
    tmp_path: Path, result, status: str, kind: str | None
) -> None:
    rig = Rig(tmp_path, FakeWorker(result))

    fired = await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()

    report = rig.ledger.find(fired.run_id)
    assert report.status == status
    assert [n.kind for n in rig.channel.sent] == ([kind] if kind else [])
    if kind:
        assert report.notified == ("test",)


@pytest.mark.asyncio
async def test_success_is_notified_only_when_asked_for(tmp_path: Path) -> None:
    rig = Rig(tmp_path, jobs={"price-watch": job(notify_on=["success"])})

    await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()

    assert [n.kind for n in rig.channel.sent] == ["success"]


@pytest.mark.asyncio
async def test_a_session_that_cannot_start_fails_the_run_not_the_stand(tmp_path: Path) -> None:
    def broken(_text, _profile):
        raise RuntimeError("Chrome CDP port 9300 did not open in time.")

    rig = Rig(tmp_path, FakeWorker(broken))

    fired = await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()

    report = rig.ledger.find(fired.run_id)
    assert report.status == "failed"
    assert "did not open" in report.error
    assert [n.kind for n in rig.channel.sent] == ["failure"]


@pytest.mark.asyncio
async def test_playback_and_recording_events_reach_the_report(tmp_path: Path) -> None:
    worker = FakeWorker(outcome(task_id="task-7", stage="retarget"), recordings=("price-watch",))
    rig = Rig(tmp_path, worker, jobs={"price-watch": job(recording="price-watch", record_on_success=True)})

    def events(_text, _profile, _result):
        rig.tally.emit(task_event("playback.started", "task-7", recording="price-watch", steps=3))
        rig.tally.emit(task_event("playback.step", "task-7", step=1, stage="playback"))
        rig.tally.emit(task_event("playback.step", "task-7", step=2, stage="retarget"))
        rig.tally.emit(task_event("playback.diverged", "task-7", step=3, kind="checkpoint"))
        rig.tally.emit(task_event("recording.saved", "task-7", name="price-watch", steps=3, replaced=True))

    worker.on_run = events
    fired = await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()

    report = rig.ledger.find(fired.run_id)
    assert report.playback == {
        "recording": "price-watch", "replayed": 1, "retargeted": 1, "diverged_at": 3, "diverged": "checkpoint",
    }
    assert report.recording_updated is True


@pytest.mark.asyncio
async def test_a_failing_channel_never_fails_the_run(tmp_path: Path) -> None:
    rig = Rig(tmp_path, FakeWorker(outcome("failed")), channel=RecordingChannel(fail=True))

    fired = await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()

    assert rig.ledger.find(fired.run_id).status == "failed"
    failed, = rig.payloads("serve.notify.failed")
    assert failed["channel"] == "test"
    assert "hunter2" not in str(rig.sink.records)


# ----------------------------------------------------------------------------- the worker


@pytest.mark.asyncio
async def test_a_failed_server_is_reconnected_before_the_run(tmp_path: Path) -> None:
    worker = FakeWorker()
    worker.servers = (ServerView(name="playwright", state="failed"),)
    rig = Rig(tmp_path, worker)

    await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()

    assert worker.reconnected == ["playwright"]
    assert worker.resets == 0


@pytest.mark.asyncio
async def test_a_server_that_cannot_reconnect_resets_the_session(tmp_path: Path) -> None:
    worker = FakeWorker()
    worker.servers = (ServerView(name="playwright", state="failed"),)
    worker.reconnect_fails = True
    rig = Rig(tmp_path, worker)

    await rig.dispatcher.fire("price-watch", HOURLY)
    await rig.drain()

    assert worker.resets == 1
    assert len(worker.calls) == 1


@pytest.mark.asyncio
async def test_the_session_is_recreated_after_session_max_runs(tmp_path: Path) -> None:
    rig = Rig(tmp_path, session_max_runs=2)

    for _ in range(3):
        await rig.dispatcher.fire("price-watch", HOURLY)
        await rig.drain()

    assert rig.worker.resets == 1
    assert len(rig.worker.calls) == 3


# ----------------------------------------------------------------------------- shutdown


@pytest.mark.asyncio
async def test_shutdown_lets_the_running_run_finish_within_the_grace(tmp_path: Path) -> None:
    worker = FakeWorker()
    worker.gate = asyncio.Event()
    rig = Rig(tmp_path, worker)
    runner = asyncio.create_task(rig.dispatcher.run())
    fired = await rig.dispatcher.fire("price-watch", HOURLY)
    await worker.started.wait()

    stopping = asyncio.create_task(rig.dispatcher.stop(grace_seconds=5))
    await asyncio.sleep(0)
    late = await rig.dispatcher.fire("price-watch", HOURLY)
    worker.gate.set()
    await stopping
    await runner

    assert (late.state, late.reason) == ("skipped", "shutdown")
    assert worker.cancelled == 0
    assert rig.ledger.find(fired.run_id).status == "completed"


@pytest.mark.asyncio
async def test_shutdown_cancels_a_run_that_outlives_the_grace(tmp_path: Path) -> None:
    worker = FakeWorker()
    worker.gate = asyncio.Event()

    async def cancel() -> bool:
        worker.cancelled += 1
        worker.gate.set()
        return True

    worker.cancel = cancel  # type: ignore[method-assign]
    rig = Rig(tmp_path, worker)
    runner = asyncio.create_task(rig.dispatcher.run())
    await rig.dispatcher.fire("price-watch", HOURLY)
    await worker.started.wait()

    await rig.dispatcher.stop(grace_seconds=0.01)
    await runner

    assert worker.cancelled == 1


def test_status_shows_the_day_and_the_jobs_without_secrets(tmp_path: Path) -> None:
    rig = Rig(tmp_path, token="t0ken-value", daily_max_runs=5)

    status = rig.dispatcher.status()

    assert status["day"]["max_runs"] == 5
    assert status["queue"] == [] and status["running"] is None
    assert "price-watch" in status["jobs"]
    assert "t0ken-value" not in str(status)


@pytest.mark.asyncio
async def test_the_day_run_cap_holds_for_runs_already_queued(tmp_path: Path) -> None:
    jobs = {name: job() for name in ("a", "b", "c")}
    rig = Rig(tmp_path, jobs=jobs, daily_max_runs=2)

    for name in ("a", "b", "c"):
        assert (await rig.dispatcher.fire(name, HOURLY)).state == "queued"
    await rig.drain()

    assert len(rig.worker.calls) == 2
    assert rig.payloads("serve.run.skipped")[-1]["reason"] == "budget"


@pytest.mark.asyncio
async def test_a_run_queued_behind_a_running_one_waits_for_the_cooldown(tmp_path: Path) -> None:
    worker = FakeWorker()
    worker.gate = asyncio.Event()
    rig = Rig(tmp_path, worker, coalesce="queue_one", cooldown_seconds=600)
    runner = asyncio.create_task(rig.dispatcher.run())

    await rig.dispatcher.fire("price-watch", HOURLY)
    await worker.started.wait()
    behind = await rig.dispatcher.fire("price-watch", HOURLY)
    finished_at = rig.clock.now()
    worker.gate.set()
    for _ in range(50):
        await asyncio.sleep(0)
        if len(worker.calls) == 2:
            break
    await rig.dispatcher.stop(grace_seconds=5)
    await runner

    assert behind.state == "queued"
    assert len(worker.calls) == 2
    assert (rig.clock.now() - finished_at).total_seconds() >= 600


@pytest.mark.asyncio
async def test_a_stop_after_the_probe_never_starts_the_main_task(tmp_path: Path) -> None:
    probe = {"steps": [{"tool": "snapshot"}]}
    worker = FakeWorker(outcome(answer="Price 10", stage="playback", calls=0))
    rig = Rig(tmp_path, worker, jobs={"price-watch": job(probe=probe)})
    fired = await rig.dispatcher.fire("price-watch", HOURLY)

    def stop_now(_text, _profile, _result):
        rig.dispatcher._stopping = True  # the shutdown arrives while the probe ends

    worker.on_run = stop_now
    await rig.dispatcher.run_pending()

    assert len(worker.calls) == 1
    report = rig.ledger.find(fired.run_id)
    assert (report.status, report.stop_reason) == ("cancelled", "shutdown")


@pytest.mark.asyncio
async def test_shutdown_ends_even_when_cancelling_the_session_does_not_stop_the_run(tmp_path: Path) -> None:
    worker = FakeWorker()
    worker.gate = asyncio.Event()

    async def cancel_nothing() -> bool:  # e.g. the run sits in a session reset
        worker.cancelled += 1
        return False

    worker.cancel = cancel_nothing  # type: ignore[method-assign]
    rig = Rig(tmp_path, worker)
    runner = asyncio.create_task(rig.dispatcher.run())
    await rig.dispatcher.fire("price-watch", HOURLY)
    await worker.started.wait()

    await asyncio.wait_for(rig.dispatcher.stop(grace_seconds=0.01), 2)
    await asyncio.wait_for(runner, 2)

    assert worker.cancelled == 1
