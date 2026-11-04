"""``<serve.dir>/ledger.jsonl``: one :class:`RunReport` per line, and the day totals the
stand's day budgets are checked against (restored from the file on start)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from pathlib import Path

from src.agent_loop.events import read_jsonl_lines
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
        for report in self._read():
            self._count(report)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, report: RunReport) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(report.to_json(), ensure_ascii=False))
            file.write("\n")
        self._count(report)

    def day_totals(self, day: date) -> DayTotals:
        return self._days.get(day, DayTotals())

    def day_of(self, moment: datetime) -> date:
        return moment.astimezone(self._tz).date()

    def find(self, run_id: str) -> RunReport | None:
        """The report of ``run_id``, newest first; ``None`` when the ledger has none."""

        for report in reversed(list(self._read())):
            if report.run_id == run_id:
                return report
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

    def _read(self) -> list[RunReport]:
        if not self._path.is_file():
            return []
        reports: list[RunReport] = []
        for number, line in enumerate(read_jsonl_lines(self._path), start=1):
            try:
                reports.append(RunReport.from_json(json.loads(line)))
            except (ValueError, KeyError, TypeError):
                _LOG.warning("ledger %s: line %d is not a run report, skipped", self._path, number)
        return reports


__all__ = ["DayTotals", "Ledger"]
