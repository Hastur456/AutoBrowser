"""A probe over a real session and a real MCP server: an unchanged page costs no model call.

The page server renders new element refs on every load; as the browser server its output is
read by the browser playback adapter, whose region drops them, so the fingerprint of an
unchanged page stays the same (``tests/mcp_fixtures/fake_page_server.py``).
"""

from __future__ import annotations

import json
import sys
from datetime import UTC
from pathlib import Path

import pytest

from src.agent_loop.events import EventEmitter, InMemoryEventSink
from src.config import ServeSettings, get_settings
from src.harness.session import SessionRuntime, default_mcp_runtime_factory
from src.serve.dispatcher import Dispatcher, TriggerRef
from src.serve.ledger import Ledger
from src.serve.notify import Notifier
from src.serve.state import JobStateStore
from src.serve.tally import RunTally
from src.ui.controller import SessionController
from tests.serve.serve_fakes import FakeClock, RecordingChannel
from tests.test_harness_session import make_config, no_start, noop_wait
from tests.test_playback_integration import DONE, PAGE_SERVER, PLAN, CountingModel

JOB = {
    "task": "Report the kettle price.",
    "probe": {
        "steps": [
            {"tool": "navigate", "args": {"url": "https://shop.example/item/1"}},
            {"tool": "snapshot"},
        ],
        "select": "(?s)heading.*",
    },
}


@pytest.mark.asyncio
async def test_an_unchanged_page_runs_no_main_task_and_calls_no_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AUTOBROWSER_REPLAY__ENABLED", "true")
    monkeypatch.setenv("AUTOBROWSER_BROWSER__LAUNCH", "external")
    monkeypatch.setenv(
        "AUTOBROWSER_MCP_SERVERS",
        json.dumps({"page": {"transport": "stdio", "command": sys.executable, "args": [PAGE_SERVER]}}),
    )
    # The browser server: its snapshot format (refs) is read by the browser playback adapter.
    monkeypatch.setenv("AUTOBROWSER_BROWSER_MCP_SERVER", "page")
    get_settings.cache_clear()
    model = CountingModel([PLAN, DONE])
    tally = RunTally()
    session = SessionRuntime(
        make_config(no_mcp=False),
        llm_factory=lambda **_kwargs: model,
        start_chrome_cdp=no_start,
        wait_for_port=noop_wait,
        mcp_runtime_factory=default_mcp_runtime_factory,
        event_sinks=[tally],
    )
    controller = SessionController(session)
    settings = ServeSettings(jobs={"kettle": JOB})
    sink = InMemoryEventSink()
    events = EventEmitter(sink)
    channel = RecordingChannel()
    ledger = Ledger(tmp_path / "ledger.jsonl", UTC)
    dispatcher = Dispatcher(
        settings,
        controller,
        ledger=ledger,
        states=JobStateStore(tmp_path),
        notifier=Notifier([channel], events, max_chars=settings.notify_max_chars),
        events=events,
        tally=tally,
        clock=FakeClock(),
    )
    try:
        first = await dispatcher.fire("kettle", TriggerRef("interval", "t"))
        await dispatcher.run_pending()
        calls_after_first = model.calls
        second = await dispatcher.fire("kettle", TriggerRef("interval", "t"))
        await dispatcher.run_pending()
    finally:
        await controller.shutdown(5)
        await session.close()

    one, two = ledger.find(first.run_id), ledger.find(second.run_id)
    assert (one.status, one.changed) == ("completed", True)
    assert [task.kind for task in one.tasks] == ["probe", "main"]
    assert one.tasks[0].stage == "playback"
    assert calls_after_first >= 1  # the main task used the model, the probe did not
    assert (two.status, two.changed) == ("unchanged", False)
    assert [task.kind for task in two.tasks] == ["probe"]
    assert model.calls == calls_after_first
    assert [note.kind for note in channel.sent] == ["change"]
