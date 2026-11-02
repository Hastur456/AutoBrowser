"""The recording format (``src/agent_loop/recordings.py``) and its file store."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agent_loop.recordings import (
    RECORDING_FORMAT,
    RecordedStep,
    Recording,
    RecordingFormatError,
    RecordingNameError,
    recording_from_json,
    recording_to_json,
    schema_digest,
    validate_recording_name,
)
from src.config import ReplaySettings
from src.contracts import Binding
from src.harness import recording_store as store_module
from src.harness.recording_store import (
    PREVIOUS_FILE,
    RECORDING_FILE,
    STATS_FILE,
    RecordingNotFoundError,
    RecordingStore,
)


def make_recording(name: str = "price-watch", *, task: str = "Read the price") -> Recording:
    return Recording(
        name=name,
        task=task,
        answer="output",
        plan=({"id": 1, "description": "Open the page", "status": "pending"},),
        catalog={"open": {"server": "web", "read_only": False, "schema_digest": schema_digest({})}},
        created_at="2026-11-02T10:00:00+00:00",
        source={"task_id": "task-1", "model": "m"},
        steps=(
            RecordedStep(index=1, tool="open", args={"url": "https://shop.example/1"}, server="web"),
            RecordedStep(
                index=2,
                tool="click",
                args={"element": "Specs tab", "target": "e42"},
                server="web",
                plan_step=0,
                bindings=(
                    Binding(path=("target",), value="e42", descriptor={"role": "tab", "name": "Specs"}),
                ),
                checkpoint={"url": "shop.example/1"},
            ),
        ),
    )


@pytest.fixture
def store(tmp_path: Path) -> RecordingStore:
    return RecordingStore(tmp_path / "recordings", ReplaySettings())


def test_a_recording_round_trips_through_json() -> None:
    recording = make_recording()

    document = recording_to_json(recording)

    assert document["format"] == RECORDING_FORMAT
    assert recording_from_json(json.loads(json.dumps(document))) == recording


@pytest.mark.parametrize(
    "name", ["", "Price", "price_watch", "../etc", "a/b", "-lead", "trail-", "a--b", "x" * 65]
)
def test_invalid_names_are_refused(name: str) -> None:
    with pytest.raises(RecordingNameError):
        validate_recording_name(name)


@pytest.mark.parametrize("name", ["a", "price-watch", "v2", "x" * 64])
def test_valid_names_pass(name: str) -> None:
    assert validate_recording_name(name) == name


def test_the_schema_digest_ignores_key_order() -> None:
    assert schema_digest({"a": 1, "b": {"c": 2}}) == schema_digest({"b": {"c": 2}, "a": 1})
    assert schema_digest({"a": 1}) != schema_digest({"a": 2})
    assert schema_digest(None) == schema_digest({})


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d.update(format="autobrowser.recording/2"), "unsupported recording format"),
        (lambda d: d.update(name="../x"), "invalid recording name"),
        (lambda d: d.update(answer="maybe"), "unknown answer mode"),
        (lambda d: d.update(steps={}), "steps must be a list"),
        (lambda d: d["steps"][0].pop("tool"), "has no tool"),
        (lambda d: d["steps"][0].update(index=5), "out of order"),
        (lambda d: d["steps"][1]["bindings"][0].update(path=[]), "binding path"),
        (lambda d: d["steps"][1]["bindings"][0].update(value=""), "binding value"),
        (lambda d: d.update(catalog=[]), "catalog"),
    ],
)
def test_malformed_documents_are_refused(mutate, message: str) -> None:  # noqa: ANN001
    document = recording_to_json(make_recording())
    mutate(document)

    with pytest.raises(RecordingFormatError, match=message):
        recording_from_json(document)


def test_the_store_saves_and_loads(store: RecordingStore) -> None:
    recording = make_recording()

    assert store.save(recording) is False
    assert store.exists("price-watch")
    assert store.load("price-watch") == recording
    assert (store.root / "price-watch" / RECORDING_FILE).is_file()


def test_a_missing_recording_is_not_found(store: RecordingStore) -> None:
    with pytest.raises(RecordingNotFoundError):
        store.load("nothing-here")


def test_a_corrupt_file_is_a_format_error(store: RecordingStore) -> None:
    directory = store.root / "broken"
    directory.mkdir(parents=True)
    (directory / RECORDING_FILE).write_text("{not json", encoding="utf-8")

    with pytest.raises(RecordingFormatError):
        store.load("broken")
    (info,) = store.entries()
    assert info.name == "broken" and info.error


def test_a_file_naming_another_recording_is_refused(store: RecordingStore) -> None:
    store.save(make_recording("first"))
    (store.root / "second").mkdir()
    (store.root / "first" / RECORDING_FILE).replace(store.root / "second" / RECORDING_FILE)

    with pytest.raises(RecordingFormatError, match="names 'first'"):
        store.load("second")


def test_a_path_escaping_name_never_touches_the_disk(store: RecordingStore) -> None:
    with pytest.raises(RecordingNameError):
        store.load("../outside")
    with pytest.raises(RecordingNameError):
        store.delete("..")


def test_replacing_keeps_the_previous_recording(store: RecordingStore) -> None:
    store.save(make_recording(task="first"))

    assert store.save(make_recording(task="second")) is True
    assert store.load("price-watch").task == "second"
    previous = json.loads((store.root / "price-watch" / PREVIOUS_FILE).read_text(encoding="utf-8"))
    assert previous["task"] == "first"


def test_without_keep_previous_nothing_is_kept(tmp_path: Path) -> None:
    store = RecordingStore(tmp_path, ReplaySettings(keep_previous=False))
    store.save(make_recording(task="first"))
    store.save(make_recording(task="second"))

    assert not (tmp_path / "price-watch" / PREVIOUS_FILE).exists()


def test_a_failed_write_leaves_the_old_recording_intact(
    store: RecordingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.save(make_recording(task="first"))

    def broken_dumps(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise OSError("disk full")

    monkeypatch.setattr(store_module.json, "dumps", broken_dumps)
    with pytest.raises(OSError):
        store.save(make_recording(task="second"))
    monkeypatch.undo()

    assert store.load("price-watch").task == "first"
    assert not [path for path in (store.root / "price-watch").iterdir() if path.suffix == ".tmp"]


def test_playback_stats_never_rewrite_the_recording(store: RecordingStore) -> None:
    store.save(make_recording())
    before = (store.root / "price-watch" / RECORDING_FILE).read_bytes()

    store.record_playback("price-watch", stage="playback")
    stats = store.record_playback("price-watch", stage="agent", diverged="checkpoint")

    assert (store.root / "price-watch" / RECORDING_FILE).read_bytes() == before
    assert stats["playbacks"] == 2
    assert stats["by_stage"] == {"playback": 1, "agent": 1}
    assert stats["diverged"] == {"checkpoint": 1}
    assert stats["last_stage"] == "agent"
    assert json.loads((store.root / "price-watch" / STATS_FILE).read_text(encoding="utf-8")) == stats


def test_entries_and_delete(store: RecordingStore) -> None:
    store.save(make_recording("b-run", task="second"))
    store.save(make_recording("a-run", task="first"))

    assert [(info.name, info.steps) for info in store.entries()] == [("a-run", 2), ("b-run", 2)]
    assert store.delete("a-run") is True
    assert store.delete("a-run") is False
    assert [info.name for info in store.entries()] == ["b-run"]


def test_an_absent_root_lists_nothing(tmp_path: Path) -> None:
    assert RecordingStore(tmp_path / "none", ReplaySettings()).entries() == []
