"""Recording files under ``<storage.root_dir>/<replay.dir>/<name>/``.

``recording.json`` is the current recording, replaced atomically; ``previous.json`` keeps the
one it replaced (``replay.keep_previous``); ``stats.json`` counts playbacks so a replay never
rewrites the recording itself. A name is validated before any path is built, so it cannot
leave the recordings directory.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.agent_loop.recordings import (
    Recording,
    RecordingFormatError,
    recording_from_json,
    recording_to_json,
    validate_recording_name,
)
from src.config import ReplaySettings, StorageSettings

RECORDING_FILE = "recording.json"
PREVIOUS_FILE = "previous.json"
STATS_FILE = "stats.json"


class RecordingNotFoundError(FileNotFoundError):
    """No recording of that name exists."""


@dataclass(frozen=True)
class RecordingInfo:
    """One stored recording as ``autobrowser recordings list`` shows it."""

    name: str
    task: str
    steps: int
    created_at: str
    #: Set when the file cannot be read; the other fields are then empty.
    error: str = ""


class RecordingStore:
    """Reads and writes the recordings of the AutoBrowser home."""

    def __init__(self, root: Path, settings: ReplaySettings) -> None:
        self._root = Path(root)
        self._settings = settings

    @classmethod
    def from_settings(cls, replay: ReplaySettings, storage: StorageSettings) -> RecordingStore:
        """The store at ``storage.root_dir / replay.dir``; nothing is created until a write."""

        return cls(Path(storage.root_dir) / replay.dir, replay)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def settings(self) -> ReplaySettings:
        return self._settings

    def directory(self, name: str) -> Path:
        return self._root / validate_recording_name(name)

    def exists(self, name: str) -> bool:
        return (self.directory(name) / RECORDING_FILE).is_file()

    def load(self, name: str) -> Recording:
        """The recording ``name``; :class:`RecordingNotFoundError` or
        :class:`~src.agent_loop.recordings.RecordingFormatError` when it cannot be used."""

        path = self.directory(name) / RECORDING_FILE
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise RecordingNotFoundError(f"no recording named {name!r}") from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise RecordingFormatError(f"{path}: not JSON ({exc})") from exc
        recording = recording_from_json(data)
        if recording.name != name:
            raise RecordingFormatError(f"{path}: names {recording.name!r}, not {name!r}")
        return recording

    def save(self, recording: Recording) -> bool:
        """Write ``recording``; ``True`` when it replaced an existing one."""

        directory = self.directory(recording.name)
        directory.mkdir(parents=True, exist_ok=True)
        current = directory / RECORDING_FILE
        replaced = current.is_file()
        if replaced and self._settings.keep_previous:
            shutil.copyfile(current, directory / PREVIOUS_FILE)
        _atomic_write_json(current, recording_to_json(recording))
        return replaced

    def entries(self) -> list[RecordingInfo]:
        """Every recording directory, sorted by name; unreadable ones carry ``error``."""

        if not self._root.is_dir():
            return []
        infos: list[RecordingInfo] = []
        for directory in sorted(self._root.iterdir()):
            if not directory.is_dir() or not (directory / RECORDING_FILE).is_file():
                continue
            try:
                recording = self.load(directory.name)
            except (OSError, ValueError) as exc:
                infos.append(RecordingInfo(directory.name, "", 0, "", error=str(exc)))
                continue
            infos.append(
                RecordingInfo(
                    recording.name, recording.task, len(recording.steps), recording.created_at
                )
            )
        return infos

    def delete(self, name: str) -> bool:
        """Remove the recording directory; ``False`` when there was none."""

        directory = self.directory(name)
        if not directory.is_dir():
            return False
        shutil.rmtree(directory)
        return True

    def stats(self, name: str) -> dict[str, Any]:
        """The playback counters of ``name``; ``{}`` when it was never replayed."""

        try:
            data = json.loads((self.directory(name) / STATS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def record_playback(self, name: str, *, stage: str, diverged: str = "") -> dict[str, Any]:
        """Count one playback of ``name`` that reached ``stage`` (and diverged as ``diverged``).

        Only ``stats.json`` changes; the recording itself is never rewritten by a replay.
        """

        stats = self.stats(name)
        stats["playbacks"] = int(stats.get("playbacks", 0) or 0) + 1
        by_stage = dict(stats.get("by_stage") or {})
        by_stage[stage] = int(by_stage.get(stage, 0) or 0) + 1
        stats["by_stage"] = by_stage
        if diverged:
            by_kind = dict(stats.get("diverged") or {})
            by_kind[diverged] = int(by_kind.get(diverged, 0) or 0) + 1
            stats["diverged"] = by_kind
        stats["last_played_at"] = datetime.now(UTC).isoformat()
        stats["last_stage"] = stage
        directory = self.directory(name)
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(directory / STATS_FILE, stats)
        return stats


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write ``payload`` next to ``path`` and replace it in one step: a failed write never
    leaves a half-written file behind."""

    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


__all__ = [
    "PREVIOUS_FILE",
    "RECORDING_FILE",
    "STATS_FILE",
    "RecordingInfo",
    "RecordingNotFoundError",
    "RecordingStore",
]
