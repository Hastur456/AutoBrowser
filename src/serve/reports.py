"""The report of one stand run (``RunReport``), as the ledger stores it and the API returns it
(``docs/development/2026-11-02-serve-mode-design.md`` section 6.5)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

#: How a run ended. ``unchanged``: the probe saw nothing new, no main task ran.
RunStatus = Literal["completed", "unchanged", "failed", "blocked", "cancelled"]
#: What a task of a run was for.
TaskKind = Literal["probe", "main", "free"]
#: What happened to a trigger as it fired (``POST /v1/jobs/{id}/runs`` answers it).
RunState = Literal["queued", "coalesced", "skipped"]


@dataclass(frozen=True)
class TaskSummary:
    """One task of a run."""

    task_id: str
    kind: TaskKind
    status: str
    stop_reason: str = ""
    stage: str = ""


@dataclass(frozen=True)
class RunReport:
    """One run of a job: what triggered it, how it ended, what it cost."""

    run_id: str
    job_id: str
    #: ``{"type": ..., "id": ...}`` of the trigger (``api``/``manual`` for an API call).
    trigger: Mapping[str, str]
    started_at: str
    finished_at: str
    duration_ms: int
    status: RunStatus
    stop_reason: str = ""
    stage_reached: str = ""
    tasks: tuple[TaskSummary, ...] = ()
    #: The probe's verdict; ``None`` for a job without a probe.
    changed: bool | None = None
    #: ``{"recording", "replayed", "retargeted", "diverged_at", "diverged"}``; empty without playback.
    playback: Mapping[str, Any] = field(default_factory=dict)
    #: ``{"model_calls", "input_tokens", "output_tokens", "cost", "by_stage", "by_model"}``.
    usage: Mapping[str, Any] = field(default_factory=dict)
    #: ``{"run": <RunBudget fields>, "day_spent_cost": ...}``.
    budget: Mapping[str, Any] = field(default_factory=dict)
    recording_updated: bool = False
    notified: tuple[str, ...] = ()
    #: ``Type: message`` of a failure the run could not report otherwise.
    error: str = ""

    @property
    def tokens(self) -> int:
        return int(self.usage.get("input_tokens", 0) or 0) + int(self.usage.get("output_tokens", 0) or 0)

    @property
    def cost(self) -> float | None:
        value = self.usage.get("cost")
        return None if value is None else float(value)

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "job_id": self.job_id,
            "trigger": dict(self.trigger),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "stage_reached": self.stage_reached,
            "tasks": [
                {
                    "task_id": task.task_id,
                    "kind": task.kind,
                    "status": task.status,
                    "stop_reason": task.stop_reason,
                    "stage": task.stage,
                }
                for task in self.tasks
            ],
            "changed": self.changed,
            "playback": dict(self.playback),
            "usage": dict(self.usage),
            "budget": dict(self.budget),
            "recording_updated": self.recording_updated,
            "notified": list(self.notified),
            "error": self.error,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RunReport:
        """The report of a ledger line; ``KeyError``/``TypeError``/``ValueError`` when it is
        not one."""

        return cls(
            run_id=str(data["run_id"]),
            job_id=str(data["job_id"]),
            trigger={str(k): str(v) for k, v in dict(data["trigger"]).items()},
            started_at=str(data["started_at"]),
            finished_at=str(data["finished_at"]),
            duration_ms=int(data["duration_ms"]),
            status=str(data["status"]),  # type: ignore[arg-type]
            stop_reason=str(data.get("stop_reason") or ""),
            stage_reached=str(data.get("stage_reached") or ""),
            tasks=tuple(
                TaskSummary(
                    task_id=str(task["task_id"]),
                    kind=str(task["kind"]),  # type: ignore[arg-type]
                    status=str(task["status"]),
                    stop_reason=str(task.get("stop_reason") or ""),
                    stage=str(task.get("stage") or ""),
                )
                for task in data.get("tasks") or ()
            ),
            changed=data.get("changed"),
            playback=dict(data.get("playback") or {}),
            usage=dict(data.get("usage") or {}),
            budget=dict(data.get("budget") or {}),
            recording_updated=bool(data.get("recording_updated", False)),
            notified=tuple(str(item) for item in data.get("notified") or ()),
            error=str(data.get("error") or ""),
        )


__all__ = ["RunReport", "RunState", "RunStatus", "TaskKind", "TaskSummary"]
