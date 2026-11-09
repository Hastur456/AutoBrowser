"""The dispatcher over a pool of workers (design V2): parallel runs, exclusion of a job and a
recording, pinned workers, the day budget shared by running runs, and a single worker that
behaves exactly as before."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.contracts import RunProfile
from src.serve.approvals import ApprovalBridge
from src.serve.dispatcher import TriggerRef
from src.serve.pool import WorkerPool
from tests.serve.serve_fakes import FakeClock, FakeWorker, Rig, job, outcome, task_event

API = TriggerRef("api", "manual")
SNAPSHOT = Path(__file__).parent / "fixtures" / "dispatcher_v1_snapshot.json"
RUN = re.compile(r"run-[0-9a-f]{32}")


def gated(*results: Any) -> FakeWorker:
    worker = FakeWorker(*results)
    worker.gate = asyncio.Event()
    return worker


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def running(rig: Rig) -> asyncio.Task[None]:
    task = asyncio.create_task(rig.dispatcher.run())
    await settle()
    return task


async def finish(rig: Rig, loop: asyncio.Task[None]) -> None:
    for worker in rig.workers:
        if worker.gate is not None:
            worker.gate.set()
    await settle()
    await rig.dispatcher.stop(1.0)
    await loop


# -- one worker: exactly as before -----------------------------------------------------------


def _normalize(value: Any) -> Any:
    text = json.dumps(value, sort_keys=True, default=str)
    ids: dict[str, str] = {}
    text = RUN.sub(lambda match: ids.setdefault(match.group(0), f"run-{len(ids)}"), text)
    return json.loads(text)


async def test_one_worker_keeps_the_ledger_status_and_events_of_v1(tmp_path: Path) -> None:
    worker = FakeWorker(
        outcome(answer="10 USD", task_id="t1"),
        outcome("blocked", stop_reason="budget_exhausted", task_id="t2"),
        outcome("failed", task_id="t3"),
    )
    rig = Rig(
        tmp_path, worker,
        jobs={"a": job(), "b": job(coalesce="queue_one"), "c": job(notify_on=["success"])},
        daily_max_runs=10,
    )
    fired = [
        await rig.dispatcher.fire("a", API),
        await rig.dispatcher.fire("b", API),
        await rig.dispatcher.fire("b", API),
        await rig.dispatcher.fire("c", API),
    ]
    status_before = rig.dispatcher.status()
    await rig.drain()
    snapshot = _normalize(
        {
            "fired": [(item.state, item.reason) for item in fired],
            "status_before": status_before,
            "status_after": rig.dispatcher.status(),
            "ledger": [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()],
            "events": [(record.type, dict(record.payload)) for record in rig.sink.records],
            "calls": [text for text, _ in worker.calls],
        }
    )

    assert snapshot == json.loads(SNAPSHOT.read_text(encoding="utf-8"))


# -- parallel runs ----------------------------------------------------------------------------


async def test_two_jobs_run_at_once_on_two_workers_and_a_third_waits(tmp_path: Path) -> None:
    first, second = gated(), gated()
    rig = Rig(tmp_path, workers=[first, second], jobs={"a": job(), "b": job(), "c": job()})
    loop = await running(rig)

    for name in ("a", "b", "c"):
        await rig.dispatcher.fire(name, API)
    await settle()

    assert len(first.calls) == 1 and len(second.calls) == 1
    assert [item["job_id"] for item in rig.dispatcher.status()["queue"]] == ["c"]
    first.gate.set()  # type: ignore[union-attr]
    await settle()
    assert len(first.calls) == 2  # the freed worker takes the waiting job
    await finish(rig, loop)
    reports = [json.loads(line) for line in (tmp_path / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sorted(report["worker"] for report in reports) == [0, 0, 1]


async def test_a_second_run_of_a_job_waits_for_the_first(tmp_path: Path) -> None:
    first, second = gated(), gated()
    rig = Rig(tmp_path, workers=[first, second], jobs={"a": job(coalesce="queue_one")})
    loop = await running(rig)

    await rig.dispatcher.fire("a", API)
    await settle()
    assert (await rig.dispatcher.fire("a", API)).state == "queued"
    await settle()

    assert len(first.calls) + len(second.calls) == 1
    await finish(rig, loop)
    assert len(first.calls) + len(second.calls) == 2


async def test_two_jobs_of_one_recording_never_run_at_once(tmp_path: Path) -> None:
    first, second = gated(), gated()
    for worker in (first, second):
        worker.recordings = {"shared"}
    rig = Rig(
        tmp_path, workers=[first, second],
        jobs={"a": job(recording="shared"), "b": job(recording="shared")},
    )
    loop = await running(rig)

    await rig.dispatcher.fire("a", API)
    await rig.dispatcher.fire("b", API)
    await settle()

    assert len(first.calls) + len(second.calls) == 1
    await finish(rig, loop)
    assert len(first.calls) + len(second.calls) == 2


# -- pinned workers ---------------------------------------------------------------------------


async def test_a_pinned_job_waits_for_its_worker_and_others_never_use_it(tmp_path: Path) -> None:
    first, second, third = gated(), gated(), gated()
    rig = Rig(
        tmp_path, workers=[first, second, third],
        jobs={"logged-in": job(worker=2), "x": job(), "y": job(), "z": job()},
    )
    loop = await running(rig)

    for name in ("x", "y", "z"):
        await rig.dispatcher.fire(name, API)
    await settle()
    assert len(third.calls) == 0  # w2 is reserved for its pinned job
    assert [item["job_id"] for item in rig.dispatcher.status()["queue"]] == ["z"]

    await rig.dispatcher.fire("logged-in", API)
    await settle()
    assert len(third.calls) == 1
    await finish(rig, loop)


async def test_a_pinned_job_does_not_take_a_free_unreserved_worker(tmp_path: Path) -> None:
    first, second = gated(), gated()
    rig = Rig(tmp_path, workers=[first, second], jobs={"p": job(worker=1, coalesce="queue_one"), "q": job()})
    loop = await running(rig)

    await rig.dispatcher.fire("p", API)
    await settle()
    await rig.dispatcher.fire("p", API)
    await settle()

    assert (len(first.calls), len(second.calls)) == (0, 1)
    await finish(rig, loop)
    assert (len(first.calls), len(second.calls)) == (0, 2)


async def test_the_pool_reserves_the_pinned_workers() -> None:
    from src.config import ServeSettings

    settings = ServeSettings(workers=3, jobs={"p": job(worker=1), "q": job()})
    pool = WorkerPool.from_settings([FakeWorker(), FakeWorker(), FakeWorker()], settings)

    assert [slot.pinned for slot in pool.slots()] == [frozenset(), frozenset({"p"}), frozenset()]
    assert pool.slot_for("q", None).index == 0  # type: ignore[union-attr]
    assert pool.slot_for("p", 1).index == 1  # type: ignore[union-attr]


# -- the day budget ---------------------------------------------------------------------------


async def test_parallel_runs_share_what_is_left_of_the_day(tmp_path: Path) -> None:
    workers = [gated(), gated(), gated()]
    rig = Rig(
        tmp_path, workers=workers, daily_max_cost=1.0,
        jobs={name: job(budget={"max_model_calls": 5, "max_cost": 0.6}) for name in ("a", "b", "c")},
    )
    loop = await running(rig)

    for name in ("a", "b", "c"):
        await rig.dispatcher.fire(name, API)
    await settle()

    profiles: list[RunProfile] = [profile for worker in workers for _, profile in worker.calls]
    assert sorted(profile.budget.max_cost for profile in profiles) == pytest.approx([0.4, 0.6])
    assert [item["job_id"] for item in rig.dispatcher.status()["queue"]] == ["c"]
    await finish(rig, loop)


async def test_the_day_run_cap_counts_running_runs(tmp_path: Path) -> None:
    workers = [gated(), gated(), gated()]
    rig = Rig(tmp_path, workers=workers, daily_max_runs=2, jobs={"a": job(), "b": job(), "c": job()})
    loop = await running(rig)

    for name in ("a", "b", "c"):
        await rig.dispatcher.fire(name, API)
    await settle()

    assert sum(len(worker.calls) for worker in workers) == 2
    await finish(rig, loop)
    assert sum(len(worker.calls) for worker in workers) == 2
    assert [item["reason"] for item in rig.payloads("serve.run.skipped")][-1:] in (["budget"], ["shutdown"])


# -- sessions, cancel, stop -------------------------------------------------------------------


async def test_session_max_runs_recreates_only_the_worker_that_ran(tmp_path: Path) -> None:
    first, second = FakeWorker(), FakeWorker()
    rig = Rig(tmp_path, workers=[first, second], session_max_runs=1, jobs={"a": job(worker=1), "b": job()})

    await rig.dispatcher.fire("a", API)
    await rig.drain()

    assert (first.resets, second.resets) == (0, 1)


async def test_cancel_reaches_the_worker_running_the_run(tmp_path: Path) -> None:
    first, second = gated(), gated()
    rig = Rig(tmp_path, workers=[first, second], jobs={"a": job(), "b": job()})
    loop = await running(rig)
    await rig.dispatcher.fire("a", API)
    target = await rig.dispatcher.fire("b", API)
    await settle()

    result = await rig.dispatcher.cancel(target.run_id)

    assert result.stopping == (target.run_id,)
    assert (first.cancelled, second.cancelled) == (0, 1)
    await finish(rig, loop)


async def test_stop_cancels_every_busy_worker_after_the_grace(tmp_path: Path) -> None:
    first, second = gated(), gated()
    rig = Rig(tmp_path, workers=[first, second], jobs={"a": job(), "b": job()})
    loop = asyncio.create_task(rig.dispatcher.run())
    await rig.dispatcher.fire("a", API)
    await rig.dispatcher.fire("b", API)
    await settle()

    await rig.dispatcher.stop(0.01)
    await loop

    assert (first.cancelled, second.cancelled) == (1, 1)


# -- views ------------------------------------------------------------------------------------


async def test_status_lists_the_workers_of_a_pool(tmp_path: Path) -> None:
    first, second = gated(), gated()
    rig = Rig(tmp_path, workers=[first, second], jobs={"a": job(worker=1), "b": job()})
    loop = await running(rig)
    run = await rig.dispatcher.fire("a", API)
    await settle()

    status = rig.dispatcher.status()

    assert status["running"]["run_id"] == run.run_id
    assert [(item["index"], item["pinned"]) for item in status["workers"]] == [(0, []), (1, ["a"])]
    assert status["workers"][1]["running"]["run_id"] == run.run_id
    assert rig.dispatcher.running_on(1) == ("a", run.run_id) and rig.dispatcher.running_on(0) is None
    await finish(rig, loop)


async def test_one_workers_status_has_no_worker_list(tmp_path: Path) -> None:
    assert "workers" not in Rig(tmp_path).dispatcher.status()


async def test_an_approval_names_the_run_of_its_own_worker(tmp_path: Path) -> None:
    from tests.serve.test_serve_approvals import ticket

    first, second = gated(), gated()
    rig = Rig(tmp_path, workers=[first, second], api_approvals=True, jobs={"a": job(), "b": job(worker=1)})
    bridge = ApprovalBridge(second, rig.dispatcher.notifier, rig.dispatcher, index=1)
    bridge.start()
    loop = await running(rig)
    await rig.dispatcher.fire("a", API)
    run = await rig.dispatcher.fire("b", API)
    await settle()

    second.approval_listeners[0](ticket())
    await settle()

    sent = [item for item in rig.channel.sent if item.kind == "approval"]
    assert [(item.job_id, item.run_id) for item in sent] == [("b", run.run_id)]
    await finish(rig, loop)


async def test_the_report_names_the_highest_tier_a_task_reached(tmp_path: Path) -> None:
    worker = FakeWorker(outcome(task_id="t-tier"))
    rig = Rig(tmp_path, worker)
    worker.on_run = lambda _text, _profile, result: rig.tally.emit(
        task_event("ladder.escalated", result.task_id, tier=1, reason="replans")
    )

    await rig.dispatcher.fire("price-watch", API)
    await rig.drain()

    report, = rig.payloads("serve.run.finished")
    assert report["tier_reached"] == 1
    assert "worker" not in report


async def test_a_report_without_an_escalation_has_no_tier(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    await rig.dispatcher.fire("price-watch", API)
    await rig.drain()

    assert "tier_reached" not in rig.payloads("serve.run.finished")[0]



async def test_a_report_keeps_its_worker_and_tier_through_the_ledger() -> None:
    from src.serve.reports import RunReport

    report = RunReport(
        run_id="run-1", job_id="a", trigger={"type": "api", "id": "manual"}, started_at="s",
        finished_at="f", duration_ms=0, status="completed", worker=2, tier_reached=1,
    )
    plain = RunReport(
        run_id="run-2", job_id="a", trigger={"type": "api", "id": "manual"}, started_at="s",
        finished_at="f", duration_ms=0, status="completed",
    )

    assert RunReport.from_json(report.to_json()) == report
    assert {"worker", "tier_reached"}.isdisjoint(plain.to_json())


async def test_cancel_without_a_target_names_the_runs_when_several_run(tmp_path: Path) -> None:
    from src.serve.commands import ChatCommand, ServeCommands

    first, second = gated(), gated()
    rig = Rig(tmp_path, workers=[first, second], jobs={"a": job(), "b": job()})
    loop = await running(rig)
    await rig.dispatcher.fire("a", API)
    await rig.dispatcher.fire("b", API)
    await settle()
    port = ServeCommands(rig.settings, rig.dispatcher, approvals=None, events=rig.events, clock=rig.clock)

    reply = await port.handle(ChatCommand("cancel"), channel="telegram", sender="s")

    assert "Several runs are running" in reply.text
    assert (first.cancelled, second.cancelled) == (0, 0)
    await finish(rig, loop)



class StuckClock(FakeClock):
    """A clock whose sleeps never end on their own (a cooldown that has to be cancelled)."""

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.Event().wait()


async def test_cancel_reaches_a_run_waiting_out_its_cooldown(tmp_path: Path) -> None:
    worker = gated()
    rig = Rig(tmp_path, worker, clock=StuckClock(), jobs={"a": job(cooldown_seconds=600, coalesce="queue_one")})
    loop = await running(rig)
    await rig.dispatcher.fire("a", API)
    await settle()
    second = await rig.dispatcher.fire("a", API)  # queued behind the running one
    worker.gate.set()  # type: ignore[union-attr]
    await settle()  # the first ends; the second now waits out the cooldown

    result = await rig.dispatcher.cancel("a")
    await settle()

    assert result.cancelled == (second.run_id,) and result.stopping == ()
    assert [item["reason"] for item in rig.payloads("serve.run.skipped")] == ["cancelled"]
    assert len(worker.calls) == 1
    await finish(rig, loop)


class SlowReconnect(FakeWorker):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.reconnecting = asyncio.Event()

    async def reconnect(self, server: str) -> Any:
        self.reconnecting.set()
        await self.release.wait()
        return await super().reconnect(server)


async def test_a_run_cancelled_before_its_task_starts_runs_none(tmp_path: Path) -> None:
    from src.ui.views import ServerView

    worker = SlowReconnect()
    worker.servers = (ServerView(name="playwright", state="failed"),)
    rig = Rig(tmp_path, worker)
    loop = await running(rig)
    run = await rig.dispatcher.fire("price-watch", API)
    await worker.reconnecting.wait()

    result = await rig.dispatcher.cancel()
    worker.release.set()
    await settle()

    assert result.stopping == (run.run_id,)
    assert worker.calls == []
    report, = rig.payloads("serve.run.finished")
    assert (report["status"], report["stop_reason"]) == ("cancelled", "cancelled")
    # No task ran: the job's cooldown does not start.
    assert rig.states.load("price-watch").last_finished_at is None
    await finish(rig, loop)


async def test_a_probe_cancelled_by_the_user_is_a_cancelled_run(tmp_path: Path) -> None:
    probe = {"steps": [{"tool": "snapshot"}]}
    worker = gated(outcome("cancelled", stop_reason="cancelled", stage="playback", calls=0))
    rig = Rig(tmp_path, worker, jobs={"p": job(probe=probe, notify_on=["failure", "blocked"])})
    loop = await running(rig)
    await rig.dispatcher.fire("p", API)
    await settle()

    await rig.dispatcher.cancel("p")
    worker.gate.set()  # type: ignore[union-attr]
    await settle()

    report, = rig.payloads("serve.run.finished")
    assert (report["status"], report["stop_reason"]) == ("cancelled", "cancelled")
    assert len(worker.calls) == 1  # the probe only
    assert rig.channel.sent == []  # a cancel is not a failure
    await finish(rig, loop)



async def test_a_free_task_cancelled_before_it_starts_is_skipped(tmp_path: Path) -> None:
    rig = Rig(tmp_path, free_tasks=True)
    run = await rig.dispatcher.submit_task("buy milk", TriggerRef("telegram", "task"))
    await rig.dispatcher._start_eligible()  # noqa: SLF001 - taken off the queue, not started yet

    result = await rig.dispatcher.cancel()
    await settle()

    assert result.cancelled == (run.run_id,)
    assert [item["reason"] for item in rig.payloads("serve.run.skipped")] == ["cancelled"]
    assert rig.worker.calls == [] and rig.payloads("serve.run.finished") == []


async def test_a_cancel_after_an_unchanged_probe_keeps_it_unchanged(tmp_path: Path) -> None:
    import hashlib

    from src.serve.state import JobState

    probe = {"steps": [{"tool": "snapshot"}]}
    worker = gated(outcome(answer="Price 10", stage="playback", calls=0))
    rig = Rig(tmp_path, worker, jobs={"p": job(probe=probe)})
    rig.states.save("p", JobState(last_digest="sha256:" + hashlib.sha256(b"Price 10").hexdigest()))
    loop = await running(rig)
    await rig.dispatcher.fire("p", API)
    await settle()

    await rig.dispatcher.cancel("p")
    worker.gate.set()  # type: ignore[union-attr]
    await settle()

    report, = rig.payloads("serve.run.finished")
    assert report["status"] == "unchanged"
    await finish(rig, loop)


pytestmark = pytest.mark.asyncio
