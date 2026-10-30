"""The human-readable report of a batch run (``report.md`` next to ``run_index.jsonl``).

A pure renderer over ``batch.json`` and the ``run_index.jsonl`` rows: the run's provenance
(commit, model, settings fingerprint), agent against infra failures, one table row per scenario
(grade, failure kind, tasks done, time, model calls, tokens, model and tool time, tool errors,
permission blocks), a total row, then every failed scenario grouped by failure kind (infra kinds
last) with its failed checks and final answer.
:func:`render_trace_text` renders the readable trace kept beside the copied ``events.jsonl``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from src.agent_loop.benchmarks import split_by_task
from src.agent_loop.failures import INFRA_KINDS
from src.agent_loop.replay import print_action_sequence

if TYPE_CHECKING:
    from src.agent_loop.events import EventRecord
    from src.agent_loop.failures import TaskOutcome

REPORT_FILE_NAME = "report.md"

#: Summed metrics of a run, in table order.
SUMMED_METRICS = (
    "duration_ms",
    "tasks",
    "model_calls",
    "input_tokens",
    "output_tokens",
    "model_ms",
    "tool_calls",
    "tool_ms",
    "tool_errors",
    "policy_blocks",
)
_NONE = "—"
#: Kinds that are not a failure: a pass, or a scenario without checks.
_NOT_FAILED = frozenset({"passed", "ungraded"})
#: Order of the failure groups: the agent's own failures first, infra kinds last.
_KIND_ORDER = (
    "permission_denied",
    "guard_limit",
    "agent_stopped",
    "grade_failed",
    "error",
    "provider_error",
    "timeout",
)
#: Group of a failed row from before failure kinds were recorded.
_LEGACY_FAILED = "failed"
_SHORT_HASH = 12


def total_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, int | None]:
    """The metrics of every row added up; a metric no row reported stays ``None``."""

    totals: dict[str, int | None] = {}
    for key in SUMMED_METRICS:
        values = [
            value
            for row in rows
            if isinstance(value := (row.get("metrics") or {}).get(key), int)
            and not isinstance(value, bool)
        ]
        totals[key] = sum(values) if values else None
    return totals


def render_batch_report(
    metadata: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    *,
    graded: bool,
) -> str:
    """Markdown report of a batch run from its metadata and ``run_index.jsonl`` rows."""

    config = metadata.get("config") or {}
    provenance = metadata.get("provenance") or {}
    lines = [f"# Batch report: {metadata.get('batch_id', '')}", ""]
    commit = (provenance.get("git") or {}).get("commit")
    if commit:
        dirty = " (dirty)" if (provenance.get("git") or {}).get("dirty") else ""
        lines.append(f"- Commit: {str(commit)[:_SHORT_HASH]}{dirty}")
    lines.append(f"- Model: {provenance.get('model') or config.get('model') or _NONE}")
    if provenance.get("settings_sha256"):
        sections = ", ".join(provenance.get("non_default_sections") or []) or "none"
        lines.append(
            f"- Settings: {str(provenance['settings_sha256'])[:_SHORT_HASH]},"
            f" non-default: {sections}"
        )
    lines += [
        f"- Started: {metadata.get('started_at') or _NONE}",
        f"- Finished: {metadata.get('finished_at') or _NONE}",
        f"- Status: {metadata.get('status') or _NONE}",
        f"- Scenarios: {len(rows)}",
    ]
    if graded:
        counts = {status: 0 for status in ("passed", "failed", "ungraded")}
        for row in rows:
            status = (row.get("grade") or {}).get("status")
            if status in counts:
                counts[status] += 1
        lines.append(
            f"- Grades: {counts['passed']} passed, {counts['failed']} failed,"
            f" {counts['ungraded']} ungraded"
        )
    kinds = [row.get("failure_kind") for row in rows if row.get("failure_kind")]
    if kinds:
        infra = sum(1 for kind in kinds if kind in INFRA_KINDS)
        agent = sum(1 for kind in kinds if kind not in INFRA_KINDS | _NOT_FAILED)
        passed_kinds = sum(1 for kind in kinds if kind == "passed")
        lines.append(
            f"- Agent failures: {agent} · Infra failures: {infra} · Passed: {passed_kinds}"
        )
    lines += [
        "",
        "| Scenario | Grade | Kind | Tasks done | Time, s | Model calls | Tokens in / out"
        " | Model, s | Tool calls | Tools, s | Tool errors | Blocks |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        statuses = list(row.get("task_statuses") or [])
        done = sum(1 for status in statuses if status == "done")
        tasks = len(row.get("tasks") or statuses)
        grade = (row.get("grade") or {}).get("status") if graded else None
        lines.append(
            _table_row(
                str(row.get("scenario_id", "")),
                grade or _NONE,
                str(row.get("failure_kind") or _NONE),
                f"{done}/{tasks}",
                row.get("metrics") or {},
            )
        )
    totals = total_metrics(rows)
    done_total = sum(
        1 for row in rows for status in (row.get("task_statuses") or []) if status == "done"
    )
    task_total = sum(len(row.get("tasks") or []) for row in rows)
    passed = sum(1 for row in rows if (row.get("grade") or {}).get("status") == "passed")
    lines.append(
        _table_row(
            "**Total**",
            f"{passed}/{len(rows)} passed" if graded else _NONE,
            _NONE,
            f"{done_total}/{task_total}",
            totals,
        )
    )
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        group = _failure_group(row)
        if group is not None:
            groups.setdefault(group, []).append(row)
    if groups:
        lines += ["", "## Failed scenarios"]
        for group in sorted(groups, key=_group_rank):
            lines += ["", f"### {group} ({len(groups[group])})"]
            if group in INFRA_KINDS:
                lines += [
                    "",
                    "Not the agent's fault (the model provider or the time budget):"
                    " rerun these scenarios.",
                ]
            for row in groups[group]:
                lines += _failed_scenario(row)
    traced = [row for row in rows if row.get("trace_text_path") or row.get("session_dir")]
    if traced:
        lines += [
            "",
            "## Traces",
            "",
            "Each scenario's readable trace in `traces/` (beside a copy of its `events.jsonl`,"
            " the full trace) and its session.",
            "",
        ]
        lines += [f"- {row.get('scenario_id', '')}: {_trace_links(row)}" for row in traced]
    return "\n".join(lines) + "\n"


def render_trace_text(
    scenario_id: str,
    events: Sequence[EventRecord],
    outcomes: Sequence[TaskOutcome],
) -> str:
    """A readable trace: per task its text, outcome and action sequence."""

    lines = [f"# Scenario: {scenario_id}"]
    for index, task_events in enumerate(split_by_task(events)):
        outcome = outcomes[index] if index < len(outcomes) else None
        task = next(
            (str(e.payload.get("task") or "") for e in task_events if e.type == "goal.started"),
            "",
        )
        lines += ["", f"## Task {index + 1}: {task or _NONE}"]
        if outcome is not None:
            lines.append(
                f"status: {outcome.status} · stop_reason: {outcome.stop_reason or _NONE}"
                f" · failure_kind: {outcome.failure_kind}"
            )
        lines += ["", print_action_sequence(list(task_events))]
    return "\n".join(lines) + "\n"


def _trace_links(row: Mapping[str, Any]) -> str:
    links: list[str] = []
    if row.get("trace_text_path"):
        links.append(f"[{row['trace_text_path']}]({row['trace_text_path']})")
    if row.get("session_dir"):
        links.append(f"session `{row['session_dir']}`")
    return " · ".join(links)


def _failure_group(row: Mapping[str, Any]) -> str | None:
    """The failure group of a row, ``None`` when it did not fail."""

    kind = row.get("failure_kind")
    if kind:
        return None if kind in _NOT_FAILED else str(kind)
    # A row from before failure kinds were recorded fails by its grade alone.
    return _LEGACY_FAILED if (row.get("grade") or {}).get("status") == "failed" else None


def _group_rank(group: str) -> tuple[int, int, str]:
    infra = group in INFRA_KINDS
    position = _KIND_ORDER.index(group) if group in _KIND_ORDER else len(_KIND_ORDER)
    return (int(infra), position, group)


def _failed_scenario(row: Mapping[str, Any]) -> list[str]:
    lines = ["", f"#### {row.get('scenario_id', '')}", ""]
    for check in (row.get("grade") or {}).get("checks") or []:
        if check.get("status") == "failed":
            lines.append(f"- `{check.get('key', '')}`: {check.get('message', '')}")
    if row.get("error"):
        error = row["error"]
        lines.append(f"- error: {error.get('type', '')}: {error.get('message', '')}")
    answer = row.get("final_answer")
    lines += ["", "Final answer:", ""]
    lines += [f"> {line}" for line in str(answer).splitlines()] if answer else [f"> {_NONE}"]
    if row.get("trace_text_path") or row.get("session_dir"):
        lines += ["", f"Trace: {_trace_links(row)}"]
    return lines


def _table_row(name: str, grade: str, kind: str, done: str, metrics: Mapping[str, Any]) -> str:
    tokens = f"{_count(metrics.get('input_tokens'))} / {_count(metrics.get('output_tokens'))}"
    cells = [
        name,
        grade,
        kind,
        done,
        _seconds(metrics.get("duration_ms")),
        _count(metrics.get("model_calls")),
        tokens,
        _seconds(metrics.get("model_ms")),
        _count(metrics.get("tool_calls")),
        _seconds(metrics.get("tool_ms")),
        _count(metrics.get("tool_errors")),
        _count(metrics.get("policy_blocks")),
    ]
    return "| " + " | ".join(cells) + " |"


def _seconds(value: Any) -> str:
    return f"{value / 1000:.1f}" if _is_int(value) else _NONE


def _count(value: Any) -> str:
    return f"{value:,}".replace(",", " ") if _is_int(value) else _NONE


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


__all__ = ["REPORT_FILE_NAME", "render_batch_report", "total_metrics"]
