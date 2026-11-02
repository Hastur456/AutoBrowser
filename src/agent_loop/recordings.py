"""The recording format: a completed run kept for playback without the model.

A pure module like :mod:`src.agent_loop.replay`: dataclasses, (de)serialization and the
format version. Files and directories are :mod:`src.harness.recording_store`'s business;
what a reference is and how it is rebound is the playback adapter's
(``docs/development/2026-11-02-serve-mode-design.md`` §5.1).

Tool names inside a recording are user data, never logic: the step source only compares a
recorded name with the session's catalog.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, get_args

from src.contracts import Binding, PlanStep, PlaybackAnswer

#: Version tag of the file format; a file with another tag is refused.
RECORDING_FORMAT = "autobrowser.recording/1"
#: Recording names: lowercase letters, digits and single hyphens, 1-64 characters (the
#: Agent Skills naming rule), so a name can never leave the recordings directory.
RECORDING_NAME = re.compile(r"^(?=.{1,64}$)[a-z0-9]+(?:-[a-z0-9]+)*$")
#: Prefix of a schema digest.
DIGEST_PREFIX = "sha256:"


class RecordingFormatError(ValueError):
    """A recording file is not a valid ``autobrowser.recording/1`` document."""


class RecordingNameError(ValueError):
    """A recording name breaks :data:`RECORDING_NAME`."""


def validate_recording_name(name: str) -> str:
    """``name`` unchanged when it is a valid recording name; :class:`RecordingNameError` else."""

    if not isinstance(name, str) or not RECORDING_NAME.match(name):
        raise RecordingNameError(
            f"invalid recording name {name!r}: use 1-64 lowercase letters, digits and hyphens"
        )
    return name


#: Digits in fingerprinted text: a changed price or count is not a changed page.
_DIGITS = re.compile(r"\d+")
_SPACES = re.compile(r"[ \t\r\f\v]+")
#: Characters of a fingerprint key: a short hash, so a recording keeps no page text.
FINGERPRINT_KEY_CHARS = 12


def fingerprint_key(text: str) -> str:
    """A short, number-blind hash of ``text`` for a checkpoint multiset."""

    masked = _DIGITS.sub("#", " ".join(str(text).split()).lower())
    return hashlib.sha256(masked.encode("utf-8")).hexdigest()[:FINGERPRINT_KEY_CHARS]


def multiset_similarity(recorded: Mapping[str, Any], current: Mapping[str, Any]) -> float:
    """Weighted Jaccard of two ``key -> count`` multisets; ``1.0`` when nothing was recorded."""

    if not recorded:
        return 1.0
    if not current:
        return 0.0
    keys = set(recorded) | set(current)
    shared = sum(min(int(recorded.get(key, 0)), int(current.get(key, 0))) for key in keys)
    total = sum(max(int(recorded.get(key, 0)), int(current.get(key, 0))) for key in keys)
    return shared / total if total else 1.0


def select_region(
    text: str,
    select: str | None,
    ignore: Sequence[str],
    *,
    drop: Sequence[re.Pattern[str]] = (),
) -> str:
    """The region of ``text`` an ``output`` answer returns, normalized for comparison.

    ``select`` (a regex) picks the first match, ``""`` when it does not match; every
    ``ignore`` match and every ``drop`` pattern (format markers an adapter strips) is
    removed; spaces collapse and blank lines go.
    """

    region = str(text or "")
    if select:
        match = re.search(select, region)
        region = match.group(0) if match else ""
    for pattern in ignore:
        region = re.sub(pattern, "", region)
    for pattern in drop:
        region = pattern.sub("", region)
    lines = (_SPACES.sub(" ", line).strip() for line in region.splitlines())
    return "\n".join(line for line in lines if line)


def schema_digest(schema: Mapping[str, Any] | None) -> str:
    """A stable digest of a tool's input schema (canonical JSON, sha256)."""

    canonical = json.dumps(
        dict(schema or {}), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return DIGEST_PREFIX + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RecordedStep:
    """One tool call of a recorded run."""

    #: 1-based position in the recording.
    index: int
    tool: str
    args: Mapping[str, Any]
    server: str = ""
    read_only: bool = False
    #: The plan step the call belonged to (moves ``current_step`` on playback).
    plan_step: int | None = None
    bindings: tuple[Binding, ...] = ()
    #: Fingerprint of the call's output (the playback adapter's ``checkpoint``).
    checkpoint: Mapping[str, Any] = field(default_factory=dict)
    #: A secret was redacted from the arguments: the step is never replayed blindly.
    redacted: bool = False


@dataclass(frozen=True)
class Recording:
    """A completed run: its plan, its steps and the catalog of the tools it used."""

    name: str
    task: str
    steps: tuple[RecordedStep, ...]
    answer: PlaybackAnswer = "agent"
    plan: tuple[PlanStep, ...] = ()
    #: Tool name -> ``{"server", "read_only", "schema_digest"}`` at recording time.
    catalog: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    created_at: str = ""
    #: Provenance: ``session_id``, ``task_id``, ``model``, ``stage_reached``.
    source: Mapping[str, Any] = field(default_factory=dict)


def recording_to_json(recording: Recording) -> dict[str, Any]:
    """The JSON document of ``recording``."""

    return {
        "format": RECORDING_FORMAT,
        "name": recording.name,
        "created_at": recording.created_at,
        "source": dict(recording.source),
        "task": recording.task,
        "answer": recording.answer,
        "plan": [dict(step) for step in recording.plan],
        "catalog": {name: dict(entry) for name, entry in recording.catalog.items()},
        "steps": [_step_to_json(step) for step in recording.steps],
    }


def recording_from_json(data: Any) -> Recording:
    """Parse a recording document; :class:`RecordingFormatError` for anything malformed."""

    if not isinstance(data, Mapping):
        raise RecordingFormatError("a recording must be a JSON object")
    if data.get("format") != RECORDING_FORMAT:
        raise RecordingFormatError(
            f"unsupported recording format {data.get('format')!r} (expected {RECORDING_FORMAT})"
        )
    try:
        name = validate_recording_name(data.get("name"))
    except RecordingNameError as exc:
        raise RecordingFormatError(str(exc)) from exc
    answer = data.get("answer", "agent")
    if answer not in get_args(PlaybackAnswer):
        raise RecordingFormatError(f"unknown answer mode {answer!r}")
    steps = data.get("steps")
    if not isinstance(steps, list):
        raise RecordingFormatError("steps must be a list")
    parsed = tuple(_step_from_json(raw, position) for position, raw in enumerate(steps, start=1))
    plan = data.get("plan", [])
    catalog = data.get("catalog", {})
    source = data.get("source", {})
    if not isinstance(plan, list) or not all(isinstance(step, Mapping) for step in plan):
        raise RecordingFormatError("plan must be a list of objects")
    if not isinstance(catalog, Mapping) or not all(
        isinstance(entry, Mapping) for entry in catalog.values()
    ):
        raise RecordingFormatError("catalog must map tool names to objects")
    if not isinstance(source, Mapping):
        raise RecordingFormatError("source must be an object")
    return Recording(
        name=name,
        task=str(data.get("task") or ""),
        steps=parsed,
        answer=answer,
        plan=tuple(dict(step) for step in plan),  # type: ignore[misc]
        catalog={str(key): dict(value) for key, value in catalog.items()},
        created_at=str(data.get("created_at") or ""),
        source=dict(source),
    )


def _step_to_json(step: RecordedStep) -> dict[str, Any]:
    return {
        "index": step.index,
        "server": step.server,
        "tool": step.tool,
        "read_only": step.read_only,
        "plan_step": step.plan_step,
        "args": dict(step.args),
        "bindings": [
            {"path": list(binding.path), "value": binding.value, "descriptor": dict(binding.descriptor)}
            for binding in step.bindings
        ],
        "checkpoint": dict(step.checkpoint),
        "redacted": step.redacted,
    }


def _step_from_json(raw: Any, position: int) -> RecordedStep:
    if not isinstance(raw, Mapping):
        raise RecordingFormatError(f"step {position} must be an object")
    tool = raw.get("tool")
    args = raw.get("args", {})
    if not isinstance(tool, str) or not tool:
        raise RecordingFormatError(f"step {position} has no tool")
    if not isinstance(args, Mapping):
        raise RecordingFormatError(f"step {position} args must be an object")
    if raw.get("index", position) != position:
        raise RecordingFormatError(f"step {position} is out of order")
    plan_step = raw.get("plan_step")
    if plan_step is not None and (not isinstance(plan_step, int) or isinstance(plan_step, bool)):
        raise RecordingFormatError(f"step {position} plan_step must be an integer")
    checkpoint = raw.get("checkpoint", {})
    if not isinstance(checkpoint, Mapping):
        raise RecordingFormatError(f"step {position} checkpoint must be an object")
    bindings = raw.get("bindings", [])
    if not isinstance(bindings, list):
        raise RecordingFormatError(f"step {position} bindings must be a list")
    return RecordedStep(
        index=position,
        tool=tool,
        args=dict(args),
        server=str(raw.get("server") or ""),
        read_only=bool(raw.get("read_only", False)),
        plan_step=plan_step,
        bindings=tuple(_binding_from_json(item, position) for item in bindings),
        checkpoint=dict(checkpoint),
        redacted=bool(raw.get("redacted", False)),
    )


def _binding_from_json(raw: Any, position: int) -> Binding:
    if not isinstance(raw, Mapping):
        raise RecordingFormatError(f"step {position}: a binding must be an object")
    path = raw.get("path")
    value = raw.get("value")
    descriptor = raw.get("descriptor") or {}
    if (
        not isinstance(path, Sequence)
        or isinstance(path, str)
        or not path
        or not all(isinstance(part, (str, int)) and not isinstance(part, bool) for part in path)
    ):
        raise RecordingFormatError(f"step {position}: a binding path must be a list of keys")
    if not isinstance(value, str) or not value:
        raise RecordingFormatError(f"step {position}: a binding value must be a string")
    if not isinstance(descriptor, Mapping):
        raise RecordingFormatError(f"step {position}: a binding descriptor must be an object")
    return Binding(path=tuple(path), value=value, descriptor=dict(descriptor))


__all__ = [
    "DIGEST_PREFIX",
    "FINGERPRINT_KEY_CHARS",
    "RECORDING_FORMAT",
    "RECORDING_NAME",
    "RecordedStep",
    "Recording",
    "RecordingFormatError",
    "RecordingNameError",
    "recording_from_json",
    "fingerprint_key",
    "multiset_similarity",
    "recording_to_json",
    "schema_digest",
    "select_region",
    "validate_recording_name",
]
