"""``<serve.dir>/ledger.jsonl``: one :class:`RunReport` per line, and the day totals the
stand's day budgets are checked against (restored from the file on start)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from pathlib import Path

from src.serve.reports import RunReport

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class DayTotals:
    """What one day's runs spent. ``cost`` sums the known costs only."""

    runs: int = 0
    tokens: int = 0
    cost: float = 0.0


class Ledger:
    """Append-only run reports with per-day totals in the stand's time zone.

    A run whose probe saw no change (``unchanged``) cost nothing and is not counted as a run
    toward ``serve.daily_max_runs``.
    """

    def __init__(self, path: Path, tz: tzinfo) -> None:
        self._path = Path(path)
        self._tz = tz
        self._days: dict[date, DayTotals] = {}
        #: run_id -> byte offset of its line, so a lookup reads one line, not the ledger.
        self._offsets: dict[str, int] = {}
        for offset, report in self._read():
            self._count(report)
            self._offsets[report.run_id] = offset

    @property
    def path(self) -> Path:
        return self._path

    def append(self, report: RunReport) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(report.to_json(), ensure_ascii=False) + "\n").encode("utf-8")
        with self._path.open("ab") as file:
            offset = file.tell()
            file.write(line)
        self._offsets[report.run_id] = offset
        self._count(report)

    def day_totals(self, day: date) -> DayTotals:
        return self._days.get(day, DayTotals())

    def day_of(self, moment: datetime) -> date:
        return moment.astimezone(self._tz).date()

    def find(self, run_id: str) -> RunReport | None:
        """The report of ``run_id``; ``None`` when the ledger has none."""

        offset = self._offsets.get(run_id)
        if offset is None:
            return None
        try:
            with self._path.open("rb") as file:
                file.seek(offset)
                return RunReport.from_json(json.loads(file.readline().decode("utf-8")))
        except (OSError, ValueError, KeyError, TypeError):
            _LOG.warning("ledger %s: the line of %s is unreadable", self._path, run_id)
            return None

    def _count(self, report: RunReport) -> None:
        try:
            day = self.day_of(datetime.fromisoformat(report.finished_at))
        except ValueError:
            return
        totals = self.day_totals(day)
        self._days[day] = DayTotals(
            runs=totals.runs + (0 if report.status == "unchanged" else 1),
            tokens=totals.tokens + report.tokens,
            cost=totals.cost + (report.cost or 0.0),
        )

    def _read(self) -> list[tuple[int, RunReport]]:
        """``(byte offset, report)`` of every readable line; a record ends only at ``\n``."""

        if not self._path.is_file():
            return []
        reports: list[tuple[int, RunReport]] = []
        offset = 0
        with self._path.open("rb") as file:
            for number, raw in enumerate(file, start=1):
                start, offset = offset, offset + len(raw)
                if not raw.strip():
                    continue
                try:
                    reports.append((start, RunReport.from_json(json.loads(raw.decode("utf-8")))))
                except (ValueError, KeyError, TypeError):
                    _LOG.warning("ledger %s: line %d is not a run report, skipped", self._path, number)
        return reports


__all__ = ["DayTotals", "Ledger"]
