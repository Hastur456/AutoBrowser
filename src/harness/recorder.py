"""Record a completed run as a replayable recording.

``Recorder`` is a session-scoped event sink: it sees the raw events (before redaction, which
happens when an event is persisted) of the tasks it was armed for and keeps their successful
tool calls. A step is a ``tool.started``/``tool.finished`` pair; blocked calls never start,
failed ones are dropped, and the session's own service servers (memory, skills) are excluded
by a set the composition passes in, never by a name written here.

To describe a step's element references the recorder needs the page text the step was
chosen on, so it mirrors the engine's view: the latest tool output plus, per server, the
output of the latest successful read-only call, dropped by a successful state-changing call
of that server (the ``readOnlyHint`` rule of the observation compiler).

The session calls :meth:`Recorder.finish` after ``goal_end``, like the memory update; a
recording never fails a task (``recording.skipped`` says why none was written).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from src.agent_loop.events import REDACTED_VALUE, EventRecord, redact_json_safe
from src.agent_loop.recordings import RecordedStep, Recording, validate_recording_name
from src.config import ReplaySettings
from src.contracts import PlanStep, PlaybackAdapter
from src.harness.recording_store import RecordingStore

_LOG = logging.getLogger(__name__)

#: Ladder stages a run may reach without changing what a recording would hold: a replay
#: that never left playback keeps the stored recording (only its stats change).
UNCHANGED_STAGES = frozenset({"probe", "playback"})


@dataclass
class _Capture:
    """What one armed task has recorded so far."""

    name: str
    task: str
    steps: list[RecordedStep] = field(default_factory=list)
    plan: list[PlanStep] = field(default_factory=list)
    current_step: int | None = None
    pending: Mapping[str, Any] | None = None
    #: Mirror of ``BrowserState.snapshots`` and of the latest tool output.
    snapshots: dict[str, str] = field(default_factory=dict)
    latest: str = ""
    servers: set[str] = field(default_factory=set)
    tools: set[str] = field(default_factory=set)
    #: Set when the capture failed; ``finish`` reports it instead of saving.
    broken: str = ""


@dataclass(frozen=True)
class RecordingOutcome:
    """What :meth:`Recorder.finish` did: saved (``replaced`` or new) or skipped (``reason``)."""

    name: str
    saved: bool
    steps: int = 0
    replaced: bool = False
    reason: str = ""


class Recorder:
    """Session-scoped :class:`~src.agent_loop.events.EventSink` that records armed tasks."""

    def __init__(
        self,
        store: RecordingStore,
        settings: ReplaySettings,
        *,
        adapter: PlaybackAdapter | None = None,
        service_servers: frozenset[str] = frozenset(),
    ) -> None:
        self.store = store
        self.settings = settings
        #: Set once the session knows its tools (a browser server or plain-text tools).
        self.adapter = adapter
        self._service_servers = service_servers
        self._captures: dict[str, _Capture] = {}

    # -- arming -------------------------------------------------------------------------

    def arm(
        self,
        task_id: str,
        name: str,
        task: str,
        *,
        snapshots: Mapping[str, str] | None = None,
    ) -> None:
        """Record the task ``task_id`` as ``name``; raises for an invalid name.

        ``snapshots`` are the kept outputs the task starts from (the carried
        ``BrowserState.snapshots``): the engine's page view on the first step includes them,
        so the mirror must too.
        """

        self._captures[task_id] = _Capture(
            name=validate_recording_name(name),
            task=task,
            snapshots={str(key): str(value) for key, value in dict(snapshots or {}).items()},
        )

    def disarm(self, task_id: str) -> None:
        self._captures.pop(task_id, None)

    def armed(self, task_id: str) -> str | None:
        """The recording name ``task_id`` is armed for, else ``None``."""

        capture = self._captures.get(task_id)
        return capture.name if capture is not None else None

    # -- the event sink -----------------------------------------------------------------

    def emit(self, record: EventRecord) -> None:
        capture = self._captures.get(str(record.task_id or ""))
        if capture is None or capture.broken:
            return
        try:
            self._observe(capture, record)
        except Exception as exc:  # noqa: BLE001 - a recorder never breaks the task
            _LOG.warning("recording %s stopped", capture.name, exc_info=True)
            capture.broken = f"capture failed: {type(exc).__name__}"

    def _observe(self, capture: _Capture, record: EventRecord) -> None:
        payload = record.payload or {}
        if record.type == "plan.updated":
            steps = payload.get("steps")
            # The plan the steps were recorded against: the last one before the first step.
            if isinstance(steps, list) and not capture.steps:
                capture.plan = [
                    {**dict(step), "status": "pending"} for step in steps if isinstance(step, Mapping)
                ]
            current = payload.get("current_step")
            capture.current_step = current if isinstance(current, int) else None
        elif record.type == "tool.started":
            capture.pending = payload
        elif record.type == "tool.finished":
            started, capture.pending = capture.pending, None
            result = payload.get("tool_result")
            if isinstance(result, Mapping) and started is not None:
                self._finish_step(capture, started, result)

    def _finish_step(
        self, capture: _Capture, started: Mapping[str, Any], result: Mapping[str, Any]
    ) -> None:
        request = started.get("tool_request") or {}
        server = str(started.get("server") or "")
        read_only = bool(started.get("read_only", False))
        content = str(result.get("content") or "")
        success = result.get("status") == "success"
        # The page text the step was chosen on: the mirror *before* this call changes it.
        page_texts = [text for text in (capture.latest, capture.snapshots.get(server, "")) if text]
        if success and server not in self._service_servers and isinstance(request, Mapping):
            self._add_step(capture, request, server, read_only, content, page_texts)
        capture.latest = content
        if success:
            if read_only:
                capture.snapshots[server] = content
            else:
                capture.snapshots.pop(server, None)

    def _add_step(
        self,
        capture: _Capture,
        request: Mapping[str, Any],
        server: str,
        read_only: bool,
        content: str,
        page_texts: list[str],
    ) -> None:
        tool = str(request.get("name") or "")
        args = dict(request.get("args") or {})
        adapter = self.adapter
        if not tool or adapter is None:
            return
        bindings = tuple(adapter.describe(args, page_texts))
        redacted_args = redact_json_safe(args)
        redacted = REDACTED_VALUE in json.dumps(redacted_args, ensure_ascii=False) and (
            REDACTED_VALUE not in json.dumps(args, ensure_ascii=False, default=str)
        )
        if any(binding.descriptor.get("sensitive") for binding in bindings):
            # A secret field (by its page label, as the adapter marks it): keep the element
            # references, drop every other string value.
            redacted_args = _redact_values(redacted_args, {binding.path for binding in bindings})
            redacted = True
        capture.steps.append(
            RecordedStep(
                index=len(capture.steps) + 1,
                tool=tool,
                # A redacted step keeps the redacted arguments and is never replayed blindly.
                args=redacted_args if redacted else args,
                server=server,
                read_only=read_only,
                plan_step=capture.current_step,
                bindings=bindings,
                checkpoint=dict(adapter.checkpoint(content)),
                redacted=redacted,
            )
        )
        capture.servers.add(server)
        capture.tools.add(tool)

    # -- finishing ----------------------------------------------------------------------

    def finish(
        self,
        task_id: str,
        *,
        status: str,
        stage_reached: str,
        catalog: Mapping[str, Mapping[str, Any]],
        source: Mapping[str, Any],
    ) -> RecordingOutcome | None:
        """Save the capture of ``task_id`` when the run deserves it; ``None`` if not armed.

        Saved only for a ``completed`` task with 1..``replay.max_steps`` steps; a run that
        never left playback keeps the stored recording.
        """

        capture = self._captures.pop(task_id, None)
        if capture is None:
            return None
        reason = self._skip_reason(capture, status, stage_reached)
        if reason:
            return RecordingOutcome(capture.name, saved=False, steps=len(capture.steps), reason=reason)
        recording = Recording(
            name=capture.name,
            task=capture.task,
            steps=tuple(capture.steps),
            answer=self.settings.default_answer,
            plan=tuple(capture.plan),
            catalog={tool: dict(catalog[tool]) for tool in sorted(capture.tools) if tool in catalog},
            created_at=datetime.now(UTC).isoformat(),
            source={**dict(source), "stage_reached": stage_reached},
        )
        replaced = self.store.save(recording)
        return RecordingOutcome(capture.name, saved=True, steps=len(capture.steps), replaced=replaced)

    def _skip_reason(self, capture: _Capture, status: str, stage_reached: str) -> str:
        if capture.broken:
            return capture.broken
        if status != "completed":
            return f"the task ended {status}"
        if not capture.steps:
            return "no successful tool call to record"
        if len(capture.steps) > self.settings.max_steps:
            return f"{len(capture.steps)} steps exceed replay.max_steps ({self.settings.max_steps})"
        if stage_reached in UNCHANGED_STAGES and self.store.exists(capture.name):
            return "the replay never left playback; the recording stays"
        return ""


def _redact_values(value: Any, keep: set[tuple[str | int, ...]], path: tuple = ()) -> Any:
    """``value`` with every string not at a ``keep`` path replaced by the redaction marker."""

    if isinstance(value, Mapping):
        return {key: _redact_values(item, keep, (*path, str(key))) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_values(item, keep, (*path, index)) for index, item in enumerate(value)]
    if isinstance(value, str) and path not in keep:
        return REDACTED_VALUE
    return value


__all__ = ["UNCHANGED_STAGES", "Recorder", "RecordingOutcome"]
