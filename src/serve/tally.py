"""``RunTally``: the stand's event sink on the worker session.

The run report needs how many recorded steps were replayed or retargeted, where playback
diverged and whether the recording was saved; ``TaskOutcome`` does not carry these, the
session's events do. The composition adds this sink to the worker's ``event_sinks``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.agent_loop.events import EventRecord

_COUNTED = frozenset(
    {"playback.started", "playback.step", "playback.diverged", "recording.saved", "ladder.escalated"}
)


@dataclass
class TaskTally:
    """What one task's events said about playback and its recording."""

    recording: str = ""
    replayed: int = 0
    retargeted: int = 0
    diverged_at: int | None = None
    diverged: str = ""
    recording_saved: bool = False
    #: The highest model tier the cascade moved the task to; ``None``: no tier change.
    tier_reached: int | None = None

    def playback(self) -> dict[str, Any]:
        """The run report's ``playback`` block; empty when the task replayed nothing."""

        if not self.recording:
            return {}
        return {
            "recording": self.recording,
            "replayed": self.replayed,
            "retargeted": self.retargeted,
            "diverged_at": self.diverged_at,
            "diverged": self.diverged,
        }


class RunTally:
    """:class:`~src.agent_loop.events.EventSink` counting playback and recording events by task."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskTally] = {}

    def emit(self, record: EventRecord) -> None:
        if record.type not in _COUNTED or not record.task_id:
            return
        tally = self._tasks.setdefault(record.task_id, TaskTally())
        payload = record.payload
        if record.type == "playback.started":
            tally.recording = str(payload.get("recording") or "")
        elif record.type == "playback.step":
            if payload.get("stage") == "retarget":
                tally.retargeted += 1
            else:
                tally.replayed += 1
        elif record.type == "playback.diverged":
            step = payload.get("step")
            tally.diverged_at = int(step) if isinstance(step, int) else None
            tally.diverged = str(payload.get("kind") or "")
        elif record.type == "ladder.escalated":
            tier = payload.get("tier")
            if isinstance(tier, int):
                tally.tier_reached = max(tally.tier_reached or 0, tier)
        else:
            tally.recording_saved = True

    def take(self, task_id: str) -> TaskTally:
        """The tally of ``task_id``, forgotten afterwards."""

        return self._tasks.pop(task_id, TaskTally())


__all__ = ["RunTally", "TaskTally"]
