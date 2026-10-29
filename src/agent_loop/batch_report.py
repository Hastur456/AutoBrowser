"""The human-readable report of a batch run (``report.md`` next to ``run_index.jsonl``).

A pure renderer over the ``run_index.jsonl`` rows: one table row per scenario (grade, tasks
done, time, model calls, tokens, model and tool time, tool errors, permission blocks), a total
row, then every scenario whose grade failed with its failed checks and final answer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

REPORT_FILE_NAME = "report.md"

#: Summed metrics of a run, in table order.
_SUMMED = (
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


def total_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, int | None]:
    """The metrics of every row added up; a metric no row reported stays ``None``."""

    totals: dict[str, int | None] = {}
    for key in _SUMMED:
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
    lines = [
        f"# Batch report: {metadata.get('batch_id', '')}",
        "",
        f"- Model: {config.get('model') or _NONE}",
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
    lines += [
        "",
        "| Scenario | Grade | Tasks done | Time, s | Model calls | Tokens in / out | Model, s"
        " | Tool calls | Tools, s | Tool errors | Blocks |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        statuses = list(row.get("task_statuses") or [])
        done = sum(1 for status in statuses if status == "done")
        tasks = len(row.get("tasks") or statuses)
        grade = (row.get("grade") or {}).get("status") if graded else None
        lines.append(_table_row(str(row.get("scenario_id", "")), grade or _NONE, f"{done}/{tasks}", row.get("metrics") or {}))
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
            f"{done_total}/{task_total}",
            totals,
        )
    )
    failures = [row for row in rows if (row.get("grade") or {}).get("status") == "failed"]
    if failures:
        lines += ["", "## Failed scenarios"]
        for row in failures:
            lines += ["", f"### {row.get('scenario_id', '')}", ""]
            for check in (row.get("grade") or {}).get("checks") or []:
                if check.get("status") == "failed":
                    lines.append(f"- `{check.get('key', '')}`: {check.get('message', '')}")
            if row.get("error"):
                error = row["error"]
                lines.append(f"- error: {error.get('type', '')}: {error.get('message', '')}")
            answer = row.get("final_answer")
            lines += ["", "Final answer:", ""]
            lines += [f"> {line}" for line in str(answer).splitlines()] if answer else [f"> {_NONE}"]
            if row.get("session_dir"):
                lines += ["", f"Trace: `{row['session_dir']}`"]
    traced = [row for row in rows if row.get("session_dir")]
    if traced:
        lines += ["", "## Traces", "", "Each scenario's session; its `events.jsonl` is the full trace.", ""]
        lines += [f"- {row.get('scenario_id', '')}: `{row['session_dir']}`" for row in traced]
    return "\n".join(lines) + "\n"


def _table_row(name: str, grade: str, done: str, metrics: Mapping[str, Any]) -> str:
    tokens = f"{_count(metrics.get('input_tokens'))} / {_count(metrics.get('output_tokens'))}"
    cells = [
        name,
        grade,
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
