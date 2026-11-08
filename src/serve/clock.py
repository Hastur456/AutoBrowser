"""Time for the stand: an injectable clock, so schedules and day budgets are testable."""

from __future__ import annotations

import asyncio
import zoneinfo
from datetime import datetime, tzinfo
from typing import Protocol


class Clock(Protocol):
    """Aware wall-clock time in the stand's zone, and a way to wait."""

    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """:class:`Clock` over the real time."""

    def __init__(self, tz: tzinfo) -> None:
        self._tz = tz

    @property
    def tz(self) -> tzinfo:
        return self._tz

    def now(self) -> datetime:
        return datetime.now(self._tz)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


def resolve_timezone(name: str) -> tzinfo:
    """``serve.timezone``: an IANA zone, or the system's current zone when empty.

    The system zone is a fixed offset (the standard library reads no DST rules from
    Windows); an IANA name follows DST. ``ZoneInfoNotFoundError`` for an unknown name
    (``ServeSettings`` already refuses one).
    """

    if name:
        return zoneinfo.ZoneInfo(name)
    local = datetime.now().astimezone().tzinfo
    assert local is not None
    return local


__all__ = ["Clock", "SystemClock", "resolve_timezone"]
