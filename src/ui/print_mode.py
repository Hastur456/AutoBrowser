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
from src.ui.views import FeedItem, SessionView, TaskView

PrintFormat = Literal["text", "json", "stream-json"]

#: Exit code per task status.
EXIT_CODES: dict[str, int] = {"completed": 0, "failed": 1, "blocked": 3, "cancelled": 130}
USAGE_EXIT_CODE = 2
INTERRUPTED_EXIT_CODE = 130


class PrintController(Protocol):
    """What print mode needs from :class:`src.ui.controller.SessionController`."""

    async def run(self, text: str) -> object: ...
    def session_view(self) -> SessionView: ...
    def task_views(self) -> list[TaskView]: ...
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
    outcomes: list[FeedItem] = []
    state = PlainState()

    def on_item(item: FeedItem) -> None:
        if output_format == "stream-json":
            write_out(json.dumps(_item_json(item), ensure_ascii=False))
        elif verbose:
            for line in format_item(item, state, settings):
                write_err(line)
        if item.kind == "outcome":
            outcomes.append(item)
            outcome_seen.set()

    async def forward() -> None:
        async for stream_item in controller.subscribe():
            if stream_item.kind != "event" or stream_item.record is None:
                continue
            item = project_event(stream_item.record)
            if item is not None:
                on_item(item)

    forwarder = asyncio.create_task(forward())
    tasks_before = len(controller.task_views())
    error = ""
    try:
        await controller.run(task)
    except asyncio.CancelledError:
        error = "cancelled"
    except Exception as exc:  # noqa: BLE001 - the failure becomes the printed result
        error = f"{type(exc).__name__}: {exc}"
    finally:
        # Let the outcome event reach the stream before the forwarder stops (only when the
        # task was tracked: a failed session start has no outcome to wait for).
        if len(controller.task_views()) > tasks_before:
            try:
                await asyncio.wait_for(outcome_seen.wait(), settings.shutdown_timeout_seconds)
            except TimeoutError:
                pass
        forwarder.cancel()
        await asyncio.gather(forwarder, return_exceptions=True)

    # The answer (or the block reason) is what the goal's terminal event carried.
    final_answer = outcomes[-1].text if outcomes else ""
    view = controller.session_view()
    tasks = controller.task_views()
    last = tasks[-1] if tasks else None
    status = last.status if last is not None and last.status != "running" else "failed"
    if error == "cancelled":
        status = "cancelled"
    summary = {
        "status": status,
        "final_answer": final_answer,
        "turns": last.turns if last is not None else None,
        "session_id": view.session_id,
        "task_id": last.task_id if last is not None else "",
    }
    if error and error != "cancelled":
        summary["error"] = error
    if output_format == "json":
        write_out(json.dumps(summary, ensure_ascii=False))
    elif output_format == "stream-json":
        write_out(json.dumps({"type": "result", **summary}, ensure_ascii=False))
    else:
        if status == "completed":
            write_out(final_answer)
        else:
            write_err(f"[{status}] {error or final_answer}".rstrip())
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
