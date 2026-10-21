"""Print mode (``autobrowser -p``): run one task headless and print only its result.

stdout carries the result alone — the final answer (``text``), one JSON object (``json``) or
JSON lines of the redacted feed items plus a closing result object (``stream-json``) — so the
command composes with pipes. Progress goes to stderr, and only with ``--verbose``. Nothing
asks a human: approvals are denied (``dont_ask``) unless ``--permission-mode`` says otherwise.

Exit codes (§7.6 of the terminal UI design): ``0`` completed, ``1`` failed, ``2`` usage
error, ``3`` blocked, ``130`` cancelled or interrupted.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict
from datetime import datetime
from typing import Any, Literal, Protocol

from src.config import UiSettings
from src.ui.feed import project_event
from src.ui.plain import PlainState, format_item
from src.ui.tui.ports import StreamItemView
from src.ui.views import FeedItem, SessionView, TaskOutcome

PrintFormat = Literal["text", "json", "stream-json"]

#: Exit code per task status.
EXIT_CODES: dict[str, int] = {"completed": 0, "failed": 1, "blocked": 3, "cancelled": 130}
USAGE_EXIT_CODE = 2
INTERRUPTED_EXIT_CODE = 130


class PrintController(Protocol):
    """What print mode needs from :class:`src.ui.controller.SessionController`."""

    async def run_outcome(self, text: str) -> TaskOutcome: ...
    def session_view(self) -> SessionView: ...
    def subscribe(self, after: Any = None) -> AsyncIterator[StreamItemView]: ...


async def run_print(
    controller: PrintController,
    task: str,
    *,
    output_format: PrintFormat,
    verbose: bool,
    settings: UiSettings,
    write_out: Callable[[str], None],
    write_err: Callable[[str], None],
) -> int:
    """Run ``task`` to its end, print the result, return the exit code."""

    outcome_seen = asyncio.Event()
    state = PlainState()

    def on_item(item: FeedItem) -> None:
        if output_format == "stream-json":
            write_out(json.dumps(_item_json(item), ensure_ascii=False))
        elif verbose:
            for line in format_item(item, state, settings):
                write_err(line)
        if item.kind == "outcome":
            outcome_seen.set()

    async def forward() -> None:
        async for stream_item in controller.subscribe():
            if stream_item.kind != "event" or stream_item.record is None:
                continue
            item = project_event(stream_item.record)
            if item is not None:
                on_item(item)

    forwarder = asyncio.create_task(forward())
    outcome: TaskOutcome | None = None
    error = ""
    cancelled = False
    try:
        outcome = await controller.run_outcome(task)
    except asyncio.CancelledError:
        cancelled = True
    except Exception as exc:  # noqa: BLE001 - the session could not start: a failed run
        error = f"{type(exc).__name__}: {exc}"
    finally:
        # stream-json/verbose: let the outcome event reach the stream before the forwarder
        # stops (only when a task of this run was tracked).
        if outcome is not None and (output_format == "stream-json" or verbose):
            try:
                await asyncio.wait_for(outcome_seen.wait(), settings.shutdown_timeout_seconds)
            except TimeoutError:
                pass
        forwarder.cancel()
        await asyncio.gather(forwarder, return_exceptions=True)

    # Only this run's task counts: a resumed session already has finished ones.
    if outcome is not None:
        status, task_id = outcome.status, outcome.task_id
        final_answer, turns, error = outcome.final_answer, outcome.turns, outcome.error
    else:
        status = "cancelled" if cancelled else "failed"
        task_id, final_answer, turns = "", "", None
    summary: dict[str, object] = {
        "status": status,
        "final_answer": final_answer,
        "turns": turns,
        "session_id": controller.session_view().session_id,
        "task_id": task_id,
    }
    if error:
        summary["error"] = error
    if output_format == "json":
        write_out(json.dumps(summary, ensure_ascii=False))
    elif output_format == "stream-json":
        write_out(json.dumps({"type": "result", **summary}, ensure_ascii=False))
    elif status == "completed":
        write_out(final_answer)
    else:
        write_err(f"[{status}] {error or final_answer}".rstrip())
    if cancelled:
        raise asyncio.CancelledError
    return EXIT_CODES.get(status, 1)


def _item_json(item: FeedItem) -> dict[str, object]:
    """A feed item without its empty fields; ``type`` is the source event type."""

    data: dict[str, object] = {"type": item.event_type}
    for key, value in asdict(item).items():
        if key in {"event_type"} or value in ("", None, (), [], {}):
            continue
        data[key] = value.isoformat() if isinstance(value, datetime) else value
    return data


__all__ = [
    "EXIT_CODES",
    "INTERRUPTED_EXIT_CODE",
    "PrintFormat",
    "USAGE_EXIT_CODE",
    "run_print",
]
