"""``/record``: arm, disarm and show the recording of the next task."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src.ui.tui.commands import find_command
from src.ui.views import SessionView


class Bridge:
    async def call(self, fn: Any) -> Any:
        return fn()


class Controller:
    def __init__(self, *, enabled: bool = True) -> None:
        self.armed = ""
        self.enabled = enabled

    def arm_recording(self, name: str | None) -> str | None:
        if name is None:
            self.armed = ""
            return None
        if not self.enabled:
            raise ValueError("replay is disabled: set replay.enabled to record runs")
        self.armed = name
        return name

    def session_view(self) -> SessionView:
        return SessionView(session_id="s", recording_armed=self.armed)


async def record(args: str, controller: Controller) -> str:
    command = find_command("record")
    assert command is not None
    host = SimpleNamespace(controller=controller, bridge=Bridge(), locale="en")
    result = await command.run(host, args)  # type: ignore[arg-type]
    (message,) = result.messages
    return message.text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_record_arms_shows_and_disarms() -> None:
    controller = Controller()

    assert await record("", controller) == "no recording is armed"
    assert await record("price-watch", controller) == "the next task is recorded as price-watch"
    assert await record("", controller) == "the next task is recorded as price-watch"
    assert await record("off", controller) == "recording is off"
    assert controller.armed == ""


@pytest.mark.asyncio
async def test_record_reports_why_it_cannot_arm() -> None:
    assert "replay is disabled" in await record("x", Controller(enabled=False))
    assert await record("a b", Controller()) == "usage: /record [<name>|off]"
