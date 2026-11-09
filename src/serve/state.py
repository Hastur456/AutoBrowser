"""What the stand remembers between runs and restarts.

``<serve.dir>/jobs/<id>/state.json`` holds a job's last run, the last time each trigger fired,
the probe's last fingerprint and region, and skip counters; ``<serve.dir>/state.json`` holds
the stand's own state (the day the budget notification went out). Files are replaced
atomically; a damaged file starts fresh with a warning and never stops the stand.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from src.agent_loop.recordings import validate_recording_name

_LOG = logging.getLogger(__name__)

JOBS_DIR = "jobs"
STATE_FILE = "state.json"
#: ``<serve.dir>/channels/<name>.json``: where a chat channel stopped reading (its offset).
CHANNELS_DIR = "channels"


@dataclass
class JobState:
    """A job's memory across runs."""

    last_finished_at: datetime | None = None
    #: Trigger id -> when it last fired.
    last_fired: dict[str, datetime] = field(default_factory=dict)
    #: ``sha256:<hex>`` of the probe's last region; ``""`` before the first probe.
    last_digest: str = ""
    #: The probe's last region (for the "was / now" of a change notification).
    last_region: str = ""
    last_run_id: str = ""
    #: Skip reason -> how often (``coalesced``, ``cooldown``, ``budget``, ...).
    counters: dict[str, int] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "last_finished_at": _iso(self.last_finished_at),
            "last_fired": {key: value.isoformat() for key, value in self.last_fired.items()},
            "last_digest": self.last_digest,
            "last_region": self.last_region,
            "last_run_id": self.last_run_id,
            "counters": dict(self.counters),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> JobState:
        return cls(
            last_finished_at=_parse(data.get("last_finished_at")),
            last_fired={
                str(key): moment
                for key, value in dict(data.get("last_fired") or {}).items()
                if (moment := _parse(value)) is not None
            },
            last_digest=str(data.get("last_digest") or ""),
            last_region=str(data.get("last_region") or ""),
            last_run_id=str(data.get("last_run_id") or ""),
            counters={str(key): int(value) for key, value in dict(data.get("counters") or {}).items()},
        )


@dataclass
class ServeState:
    """The stand's own memory."""

    #: The day (ISO date) the one daily budget notification was sent.
    budget_notified_day: str = ""


class JobStateStore:
    """Job and stand state under ``root`` (``<storage.root_dir>/<serve.dir>``)."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def load(self, job_id: str) -> JobState:
        data = self._read(self._job_path(job_id))
        try:
            return JobState.from_json(data) if data else JobState()
        except (TypeError, ValueError):
            _LOG.warning("state of job %s is damaged; starting fresh", job_id)
            return JobState()

    def save(self, job_id: str, state: JobState) -> None:
        _atomic_write_json(self._job_path(job_id), state.to_json())

    def load_serve(self) -> ServeState:
        data = self._read(self._root / STATE_FILE)
        return ServeState(budget_notified_day=str(data.get("budget_notified_day") or ""))

    def save_serve(self, state: ServeState) -> None:
        _atomic_write_json(self._root / STATE_FILE, {"budget_notified_day": state.budget_notified_day})

    def _job_path(self, job_id: str) -> Path:
        return self._root / JOBS_DIR / validate_recording_name(job_id) / STATE_FILE

    @staticmethod
    def _read(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            _LOG.warning("%s is damaged; starting fresh", path)
            return {}
        return data if isinstance(data, dict) else {}


class ChannelStateStore:
    """Where each chat channel stopped reading, one file per channel under ``root``.

    Its own file, not the stand's ``state.json``: the dispatcher keeps that one in memory, and
    a second writer would lose its updates. A damaged file starts from 0 with a warning.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def load(self, channel: str) -> int:
        data = JobStateStore._read(self._path(channel))
        try:
            return max(0, int(data.get("offset") or 0))
        except (TypeError, ValueError):
            _LOG.warning("the offset of channel %s is damaged; starting from 0", channel)
            return 0

    def save(self, channel: str, offset: int) -> None:
        _atomic_write_json(self._path(channel), {"offset": int(offset)})

    def _path(self, channel: str) -> Path:
        return self._root / CHANNELS_DIR / f"{validate_recording_name(channel)}.json"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


__all__ = ["CHANNELS_DIR", "JOBS_DIR", "STATE_FILE", "ChannelStateStore", "JobState", "JobStateStore", "ServeState"]
