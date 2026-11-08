"""The run ledger: one RunReport per line, day totals in the stand's time zone."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

from src.serve.ledger import DayTotals, Ledger
from src.serve.reports import RunReport, TaskSummary

#: A fixed zone three hours ahead of UTC: 22:30 UTC is already the next day there.
PLUS3 = timezone(timedelta(hours=3))


def report(run_id: str, finished: datetime, *, status: str = "completed", tokens: int = 100,
           cost: float | None = 0.01, calls: int = 1) -> RunReport:
    return RunReport(
        run_id=run_id,
        job_id="price-watch",
        trigger={"type": "interval", "id": "hourly"},
        started_at=(finished - timedelta(seconds=5)).isoformat(),
        finished_at=finished.isoformat(),
        duration_ms=5000,
        status=status,  # type: ignore[arg-type]
        stop_reason="answered",
        stage_reached="agent",
        tasks=(TaskSummary(task_id=f"task-{run_id}", kind="main", status=status),),
        changed=None,
        usage={"model_calls": calls, "input_tokens": tokens // 2, "output_tokens": tokens - tokens // 2,
               "cost": cost},
    )


def test_a_report_survives_the_round_trip(tmp_path: Path) -> None:
    original = report("run-1", datetime(2026, 11, 4, 10, tzinfo=UTC))

    assert RunReport.from_json(original.to_json()) == original


def test_appended_reports_are_found_again_after_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    Ledger(path, UTC).append(report("run-1", datetime(2026, 11, 4, 10, tzinfo=UTC)))

    found = Ledger(path, UTC).find("run-1")

    assert found is not None and found.run_id == "run-1"
    assert Ledger(path, UTC).find("run-2") is None


def test_day_totals_count_runs_tokens_and_known_cost(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", UTC)
    day = datetime(2026, 11, 4, 10, tzinfo=UTC)
    ledger.append(report("a", day, tokens=100, cost=0.01))
    ledger.append(report("b", day, tokens=50, cost=None))
    ledger.append(report("c", day + timedelta(days=1), tokens=7, cost=0.5))

    assert ledger.day_totals(date(2026, 11, 4)) == DayTotals(runs=2, tokens=150, cost=0.01)
    assert ledger.day_totals(date(2026, 11, 5)) == DayTotals(runs=1, tokens=7, cost=0.5)
    assert ledger.day_totals(date(2026, 11, 6)) == DayTotals()


def test_an_unchanged_probe_run_costs_nothing_and_is_not_a_run(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", UTC)
    ledger.append(report("a", datetime(2026, 11, 4, 10, tzinfo=UTC), status="unchanged", tokens=0, cost=0.0, calls=0))

    assert ledger.day_totals(date(2026, 11, 4)).runs == 0


def test_the_day_is_the_day_in_the_stand_time_zone(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", PLUS3)
    ledger.append(report("late", datetime(2026, 11, 4, 22, 30, tzinfo=UTC)))

    assert ledger.day_totals(date(2026, 11, 5)).runs == 1
    assert ledger.day_totals(date(2026, 11, 4)).runs == 0


def test_totals_are_restored_from_the_file(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    Ledger(path, UTC).append(report("a", datetime(2026, 11, 4, 10, tzinfo=UTC), tokens=40, cost=0.2))

    assert Ledger(path, UTC).day_totals(date(2026, 11, 4)) == DayTotals(runs=1, tokens=40, cost=0.2)


def test_a_damaged_line_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    Ledger(path, UTC).append(report("a", datetime(2026, 11, 4, 10, tzinfo=UTC)))
    with path.open("a", encoding="utf-8") as file:
        file.write("{not json\n")
        file.write('{"run_id": "x"}\n')
    Ledger(path, UTC).append(report("b", datetime(2026, 11, 4, 11, tzinfo=UTC)))

    ledger = Ledger(path, UTC)

    assert ledger.day_totals(date(2026, 11, 4)).runs == 2
    assert ledger.find("b") is not None


def test_a_run_is_found_without_reading_the_whole_ledger(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path, UTC)
    for index in range(3):
        ledger.append(report(f"run-{index}", datetime(2026, 11, 4, 10, index, tzinfo=UTC)))
    restarted = Ledger(path, UTC)  # the index is rebuilt on start
    ledger.append(report("run-3", datetime(2026, 11, 4, 10, 3, tzinfo=UTC)))

    def no_full_read(_self):
        raise AssertionError("find() read the whole ledger")

    monkeypatch.setattr(Ledger, "_read", no_full_read)

    assert ledger.find("run-3").run_id == "run-3"
    assert ledger.find("run-0").run_id == "run-0"
    assert restarted.find("run-1").run_id == "run-1"
    assert ledger.find("run-9") is None
