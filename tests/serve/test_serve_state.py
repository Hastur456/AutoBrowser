"""Per-job state and the stand's own state survive restarts; damage never stops the stand."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from src.serve.state import ChannelStateStore, JobState, JobStateStore, ServeState


def test_a_new_job_has_a_fresh_state(tmp_path: Path) -> None:
    assert JobStateStore(tmp_path).load("price-watch") == JobState()


def test_job_state_round_trips(tmp_path: Path) -> None:
    store = JobStateStore(tmp_path)
    when = datetime(2026, 11, 4, 10, tzinfo=UTC)
    state = JobState(
        last_finished_at=when,
        last_fired={"hourly": when},
        last_digest="sha256:abc",
        last_region="Price 10 USD",
        last_run_id="run-1",
        counters={"coalesced": 2},
    )

    store.save("price-watch", state)

    assert JobStateStore(tmp_path).load("price-watch") == state
    assert (tmp_path / "jobs" / "price-watch" / "state.json").is_file()


def test_a_damaged_job_state_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / "jobs" / "price-watch" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    assert JobStateStore(tmp_path).load("price-watch") == JobState()


def test_a_failed_write_keeps_the_previous_state(tmp_path: Path, monkeypatch) -> None:
    store = JobStateStore(tmp_path)
    store.save("j", JobState(last_digest="old"))

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("src.serve.state.os.replace", boom)
    try:
        store.save("j", JobState(last_digest="new"))
    except OSError:
        pass

    assert JobStateStore(tmp_path).load("j").last_digest == "old"


def test_the_serve_state_round_trips(tmp_path: Path) -> None:
    store = JobStateStore(tmp_path)
    assert store.load_serve() == ServeState()

    store.save_serve(ServeState(budget_notified_day="2026-11-04"))

    assert JobStateStore(tmp_path).load_serve().budget_notified_day == "2026-11-04"


def test_a_channel_offset_round_trips_in_its_own_file(tmp_path: Path) -> None:
    store = ChannelStateStore(tmp_path)

    assert store.load("telegram") == 0
    store.save("telegram", 42)

    assert ChannelStateStore(tmp_path).load("telegram") == 42
    assert (tmp_path / "channels" / "telegram.json").is_file()


def test_a_damaged_channel_offset_starts_from_zero(tmp_path: Path) -> None:
    (tmp_path / "channels").mkdir()
    (tmp_path / "channels" / "telegram.json").write_text("{nope", encoding="utf-8")

    assert ChannelStateStore(tmp_path).load("telegram") == 0
