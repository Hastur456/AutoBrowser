"""The stand's scheduler: ``interval`` and ``cron`` triggers fire their jobs on time.

Cron slots come from ``croniter`` in ``serve.timezone``; all arithmetic and comparisons run
in UTC (aware datetimes of one ``ZoneInfo`` compare by wall time and ignore ``fold``, and
adding a ``timedelta`` to them is wall-time arithmetic). Each wall-clock slot fires once: the
repeated hour of an autumn DST change does not fire a daily cron twice. When each
trigger last fired is kept in the job state, so a restart neither repeats a slot nor, with
``catch_up: none``, catches up the slots it missed while down (``one``: a single catch-up
firing). A trigger never fires sooner than ``serve.min_interval_seconds`` after its previous
firing: a faster cron is thinned (``serve.run.skipped{reason: min_interval}``).
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

from croniter import croniter

from src.config import ServeSettings, TriggerSpec
from src.serve.clock import Clock
from src.serve.dispatcher import FireResult, TriggerRef
from src.serve.state import JobStateStore

#: Trigger types the scheduler fires (webhooks are fired by the HTTP API).
SCHEDULED_TYPES = frozenset({"interval", "cron"})

FireCallback = Callable[[str, TriggerRef], Awaitable[FireResult]]
SkipCallback = Callable[[str, TriggerRef, str], Awaitable[None]]


@dataclass
class _Entry:
    job_id: str
    trigger: TriggerSpec
    due: datetime

    @property
    def ref(self) -> TriggerRef:
        return TriggerRef(type=self.trigger.type, id=self.trigger.id)


class Scheduler:
    def __init__(
        self,
        settings: ServeSettings,
        fire: FireCallback,
        *,
        skip: SkipCallback,
        clock: Clock,
        states: JobStateStore,
        tz: tzinfo,
    ) -> None:
        self._settings = settings
        self._fire = fire
        self._skip = skip
        self._clock = clock
        self._states = states
        self._tz = tz
        self._stopped = False

    def stop(self) -> None:
        self._stopped = True

    async def run(self) -> None:
        """Fire due triggers until :meth:`stop` (returns at once with nothing to schedule)."""

        entries = self._initial_entries()
        while entries and not self._stopped:
            entry = min(entries, key=lambda item: item.due)
            now = _utc(self._clock.now())
            if entry.due > now:
                await self._clock.sleep((entry.due - now).total_seconds())
                continue
            await self._fire_entry(entry, now)
            entry.due = self._after(entry.trigger, now)

    def next_fire(self, trigger: TriggerSpec, after: datetime) -> datetime:
        """The first slot of ``trigger`` strictly after ``after``."""

        return self._after(trigger, _utc(after))

    # ------------------------------------------------------------------ internals

    def _initial_entries(self) -> list[_Entry]:
        now = _utc(self._clock.now())
        entries: list[_Entry] = []
        for job_id, job in self._settings.jobs.items():
            if not job.enabled:
                continue
            state = self._states.load(job_id)
            for trigger in job.triggers:
                if trigger.type not in SCHEDULED_TYPES:
                    continue
                last = state.last_fired.get(trigger.id)
                due = self._first_due(trigger, _utc(last) if last is not None else None, now)
                entries.append(_Entry(job_id, trigger, due))
        return entries

    def _first_due(self, trigger: TriggerSpec, last: datetime | None, now: datetime) -> datetime:
        if trigger.type == "interval":
            if last is None:
                return now  # never fired: the first run starts with the stand
            period = timedelta(seconds=float(trigger.every_seconds or 0))
            due = last + period
            if due > now:
                return due
            if self._settings.catch_up == "one":
                return now
            # Skip the missed periods, keep the phase.
            missed = math.ceil((now - due) / period)
            due += period * missed
            return due if due > now else due + period
        if last is not None and self._settings.catch_up == "one":
            previous = _utc(croniter(trigger.schedule, now.astimezone(self._tz)).get_prev(datetime))
            if previous > last:
                return now
        return self._after(trigger, now)

    def _after(self, trigger: TriggerSpec, moment: datetime) -> datetime:
        if trigger.type == "interval":
            return moment + timedelta(seconds=float(trigger.every_seconds or 0))
        local = moment.astimezone(self._tz)
        slots = croniter(trigger.schedule, local)
        following: datetime = slots.get_next(datetime)
        if following.fold == 1 and following.replace(fold=0).astimezone(UTC) <= moment:
            # The repeated hour of a DST change: this wall-clock slot already came round.
            following = slots.get_next(datetime)
        return _utc(following)

    async def _fire_entry(self, entry: _Entry, now: datetime) -> None:
        state = self._states.load(entry.job_id)
        last = state.last_fired.get(entry.trigger.id)
        if last is not None and (now - _utc(last)).total_seconds() < self._settings.min_interval_seconds:
            await self._skip(entry.job_id, entry.ref, "min_interval")
            return
        state.last_fired[entry.trigger.id] = now
        self._states.save(entry.job_id, state)
        await self._fire(entry.job_id, entry.ref)


def _utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)


__all__ = ["SCHEDULED_TYPES", "Scheduler"]
