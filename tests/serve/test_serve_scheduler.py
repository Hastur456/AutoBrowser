"""Scheduler on a fake clock: interval and cron triggers, catch-up, the minimum period, DST."""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import pytest

from src.config import ServeSettings
from src.serve.dispatcher import FireResult, TriggerRef
from src.serve.scheduler import Scheduler
from src.serve.state import JobState, JobStateStore
from tests.serve.serve_fakes import FakeClock, job

START = datetime(2026, 11, 4, 9, 0, 30, tzinfo=UTC)


class Fires:
    """Records what fired and stops the scheduler after ``limit`` firings."""

    def __init__(self, clock: FakeClock, limit: int) -> None:
        self.clock = clock
        self.limit = limit
        self.fired: list[tuple[str, str, datetime]] = []
        self.skipped: list[tuple[str, str, str]] = []
        self.scheduler: Scheduler | None = None

    async def fire(self, job_id: str, trigger: TriggerRef) -> FireResult:
        self.fired.append((job_id, trigger.id, self.clock.now()))
        if len(self.fired) >= self.limit and self.scheduler is not None:
            self.scheduler.stop()
        return FireResult(run_id="run-x", state="queued")

    async def skip(self, job_id: str, trigger: TriggerRef, reason: str) -> None:
        self.skipped.append((job_id, trigger.id, reason))


def make(tmp_path: Path, triggers: list[dict[str, Any]], *, limit: int, clock: FakeClock | None = None,
         tz: tzinfo = UTC, **settings: Any) -> tuple[Scheduler, Fires, FakeClock]:
    clock = clock or FakeClock(START)
    fires = Fires(clock, limit)
    config = ServeSettings(jobs={"j": job(triggers=triggers)}, **settings)
    scheduler = Scheduler(
        config, fires.fire, skip=fires.skip, clock=clock, states=JobStateStore(tmp_path), tz=tz
    )
    fires.scheduler = scheduler
    return scheduler, fires, clock


def times(fires: Fires) -> list[datetime]:
    return [moment for _, _, moment in fires.fired]


@pytest.mark.asyncio
async def test_an_interval_fires_at_start_then_every_period(tmp_path: Path) -> None:
    scheduler, fires, _ = make(tmp_path, [{"id": "t", "type": "interval", "every_seconds": 600}], limit=3)

    await scheduler.run()

    assert times(fires) == [START, START + timedelta(minutes=10), START + timedelta(minutes=20)]
    assert JobStateStore(tmp_path).load("j").last_fired["t"] == START + timedelta(minutes=20)


@pytest.mark.asyncio
async def test_a_restarted_interval_continues_its_period_without_firing_twice(tmp_path: Path) -> None:
    JobStateStore(tmp_path).save("j", JobState(last_fired={"t": START - timedelta(minutes=4)}))
    scheduler, fires, _ = make(tmp_path, [{"id": "t", "type": "interval", "every_seconds": 600}], limit=1)

    await scheduler.run()

    assert times(fires) == [START + timedelta(minutes=6)]


@pytest.mark.asyncio
async def test_missed_interval_periods_are_not_caught_up_by_default(tmp_path: Path) -> None:
    JobStateStore(tmp_path).save("j", JobState(last_fired={"t": START - timedelta(minutes=25)}))
    scheduler, fires, _ = make(tmp_path, [{"id": "t", "type": "interval", "every_seconds": 600}], limit=1)

    await scheduler.run()

    # The slots at -15 and -5 minutes were missed; the next one is at +5.
    assert times(fires) == [START + timedelta(minutes=5)]


@pytest.mark.asyncio
async def test_catch_up_one_fires_once_for_missed_interval_periods(tmp_path: Path) -> None:
    JobStateStore(tmp_path).save("j", JobState(last_fired={"t": START - timedelta(minutes=25)}))
    scheduler, fires, _ = make(
        tmp_path, [{"id": "t", "type": "interval", "every_seconds": 600}], limit=2, catch_up="one"
    )

    await scheduler.run()

    # One catch-up firing now; the period then runs from it.
    assert times(fires) == [START, START + timedelta(minutes=10)]


@pytest.mark.asyncio
async def test_a_cron_trigger_fires_on_its_slots(tmp_path: Path) -> None:
    scheduler, fires, _ = make(tmp_path, [{"id": "c", "type": "cron", "schedule": "0 * * * *"}], limit=2)

    await scheduler.run()

    assert times(fires) == [datetime(2026, 11, 4, 10, tzinfo=UTC), datetime(2026, 11, 4, 11, tzinfo=UTC)]


@pytest.mark.asyncio
async def test_a_missed_cron_slot_is_caught_up_once_only_when_asked(tmp_path: Path) -> None:
    JobStateStore(tmp_path).save("j", JobState(last_fired={"c": datetime(2026, 11, 4, 6, tzinfo=UTC)}))
    cron = [{"id": "c", "type": "cron", "schedule": "0 * * * *"}]

    scheduler, fires, _ = make(tmp_path, cron, limit=1)
    await scheduler.run()
    assert times(fires) == [datetime(2026, 11, 4, 10, tzinfo=UTC)]

    JobStateStore(tmp_path).save("j", JobState(last_fired={"c": datetime(2026, 11, 4, 6, tzinfo=UTC)}))
    scheduler, fires, _ = make(tmp_path, cron, limit=2, catch_up="one")
    await scheduler.run()
    assert times(fires) == [START, datetime(2026, 11, 4, 10, tzinfo=UTC)]


@pytest.mark.asyncio
async def test_a_cron_faster_than_the_minimum_period_is_thinned(tmp_path: Path) -> None:
    scheduler, fires, _ = make(
        tmp_path, [{"id": "c", "type": "cron", "schedule": "* * * * *"}], limit=2, min_interval_seconds=120
    )

    await scheduler.run()

    assert times(fires) == [datetime(2026, 11, 4, 9, 1, tzinfo=UTC), datetime(2026, 11, 4, 9, 3, tzinfo=UTC)]
    assert fires.skipped == [("j", "c", "min_interval")]


@pytest.mark.asyncio
async def test_a_disabled_job_is_not_scheduled(tmp_path: Path) -> None:
    clock = FakeClock(START)
    fires = Fires(clock, 1)
    config = ServeSettings(jobs={
        "off": job(enabled=False, triggers=[{"id": "t", "type": "interval", "every_seconds": 60}]),
        "on": job(triggers=[{"id": "t", "type": "interval", "every_seconds": 600}]),
    })
    scheduler = Scheduler(config, fires.fire, skip=fires.skip, clock=clock, states=JobStateStore(tmp_path), tz=UTC)
    fires.scheduler = scheduler

    await scheduler.run()

    assert [job_id for job_id, _, _ in fires.fired] == ["on"]


@pytest.mark.asyncio
async def test_webhook_triggers_are_not_scheduled(tmp_path: Path) -> None:
    clock = FakeClock(START)
    fires = Fires(clock, 1)
    config = ServeSettings(jobs={"j": job(triggers=[{"id": "w", "type": "webhook", "secret": "s"}])})
    scheduler = Scheduler(config, fires.fire, skip=fires.skip, clock=clock, states=JobStateStore(tmp_path), tz=UTC)

    await scheduler.run()  # nothing to schedule: returns at once

    assert fires.fired == []


@pytest.mark.skipif(not zoneinfo.available_timezones(), reason="no IANA time zone database")
@pytest.mark.asyncio
async def test_a_daily_cron_fires_once_a_day_across_the_autumn_dst_change(tmp_path: Path) -> None:
    berlin = zoneinfo.ZoneInfo("Europe/Berlin")
    clock = FakeClock(datetime(2026, 10, 23, 12, tzinfo=berlin))
    scheduler, fires, _ = make(
        tmp_path, [{"id": "c", "type": "cron", "schedule": "30 2 * * *"}], limit=4, clock=clock, tz=berlin,
        timezone="Europe/Berlin",
    )

    await scheduler.run()

    local_days = [moment.astimezone(berlin).date() for moment in times(fires)]
    assert len(local_days) == len(set(local_days)) == 4
    assert all(moment.astimezone(berlin).hour == 2 for moment in times(fires))


@pytest.mark.skipif(not zoneinfo.available_timezones(), reason="no IANA time zone database")
@pytest.mark.asyncio
async def test_a_daily_cron_is_not_lost_across_the_spring_dst_change(tmp_path: Path) -> None:
    berlin = zoneinfo.ZoneInfo("Europe/Berlin")
    clock = FakeClock(datetime(2026, 3, 27, 12, tzinfo=berlin))
    scheduler, fires, _ = make(
        tmp_path, [{"id": "c", "type": "cron", "schedule": "30 2 * * *"}], limit=4, clock=clock, tz=berlin,
        timezone="Europe/Berlin",
    )

    await scheduler.run()

    stamps = times(fires)
    assert stamps == sorted(stamps)
    days = [moment.astimezone(berlin).date() for moment in stamps]
    # 02:30 does not exist on 2026-03-29; whatever croniter picks, the day after still fires.
    assert days[0].isoformat() == "2026-03-28"
    assert "2026-03-30" in [day.isoformat() for day in days]
    assert len(set(days)) == len(days)


@pytest.mark.skipif(not zoneinfo.available_timezones(), reason="no IANA time zone database")
@pytest.mark.asyncio
async def test_a_quarter_hour_cron_fires_each_wall_slot_once_when_the_clocks_go_back(tmp_path: Path) -> None:
    berlin = zoneinfo.ZoneInfo("Europe/Berlin")
    clock = FakeClock(datetime(2026, 10, 25, 1, 50, tzinfo=berlin))
    scheduler, fires, _ = make(
        tmp_path, [{"id": "c", "type": "cron", "schedule": "*/15 * * * *"}], limit=9, clock=clock, tz=berlin,
        timezone="Europe/Berlin", min_interval_seconds=60,
    )

    await scheduler.run()

    walls = [moment.astimezone(berlin).strftime("%H:%M") for moment in times(fires)]
    assert len(walls) == len(set(walls)), walls
    assert walls[:6] == ["02:00", "02:15", "02:30", "02:45", "03:00", "03:15"]


@pytest.mark.asyncio
async def test_a_failing_firing_does_not_stop_the_schedule(tmp_path: Path) -> None:
    scheduler, fires, _ = make(tmp_path, [{"id": "t", "type": "interval", "every_seconds": 600}], limit=2)
    calls = {"n": 0}
    original = fires.fire

    async def flaky(job_id, trigger):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("[WinError 5] state.json is open elsewhere")
        return await original(job_id, trigger)

    scheduler._fire = flaky

    await scheduler.run()

    assert calls["n"] == 3
    assert len(fires.fired) == 2
