"""ServeRuntime: start the worker, check the probe tools, run until told to stop."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.serve.dispatcher import TriggerRef
from src.serve.runtime import ServeRuntime, ServeStartError
from tests.serve.serve_fakes import FakeWorker, Rig, job


def runtime(rig: Rig) -> ServeRuntime:
    return ServeRuntime(rig.settings, rig.worker, dispatcher=rig.dispatcher, events=rig.events)


@pytest.mark.asyncio
async def test_the_stand_starts_runs_and_stops(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    stop = asyncio.Event()
    serving = asyncio.create_task(runtime(rig).serve(until=stop.wait()))
    await asyncio.sleep(0)

    await rig.dispatcher.fire("price-watch", TriggerRef("api", "manual"))
    for _ in range(20):
        await asyncio.sleep(0)
    stop.set()
    await serving

    assert rig.worker.calls
    types = rig.types()
    assert types[0] == "serve.started"
    assert types[-1] == "serve.stopped"
    assert rig.payloads("serve.started")[0]["jobs"] == ["price-watch"]


@pytest.mark.asyncio
async def test_a_probe_tool_the_session_lacks_stops_the_start(tmp_path: Path) -> None:
    probe = {"steps": [{"tool": "browser_navigate"}, {"tool": "browser_snapshot"}]}
    rig = Rig(tmp_path, FakeWorker(tools=("browser_navigate",)), jobs={"j": job(probe=probe)})

    with pytest.raises(ServeStartError, match="browser_snapshot"):
        await runtime(rig).serve(until=asyncio.sleep(0))

    assert "serve.started" not in rig.types()


@pytest.mark.asyncio
async def test_the_started_event_carries_no_secret(tmp_path: Path) -> None:
    rig = Rig(tmp_path, token="t0ken-value")

    await runtime(rig).serve(until=asyncio.sleep(0))

    assert "t0ken-value" not in str(rig.sink.records)
