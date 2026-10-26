"""Smoke test of the real ``autobrowser`` TUI inside a Windows pseudo-console (§6.4, item 8).

Runs ``main.py --no-mcp`` in ConPTY via pywinpty, feeds the byte stream to a pyte screen and
checks the frames a user sees: the banner, ``/help`` and ``/tasks`` in the scrollback, the
input field and status line at the bottom, no duplicated lines, exit code 0. The model is
never called and the session starts lazily, so nothing leaves the machine.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

winpty = pytest.importorskip("winpty", reason="pywinpty is Windows-only")
pyte = pytest.importorskip("pyte")

pytestmark = [
    pytest.mark.conpty,
    pytest.mark.skipif(sys.platform != "win32", reason="ConPTY is Windows-only"),
]

ROOT = Path(__file__).resolve().parents[2]
COLUMNS, ROWS = 100, 30


class Console:
    def __init__(self) -> None:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", WT_SESSION="conpty-smoke")
        env.pop("NO_COLOR", None)
        # A neutral profile: the personal config.yaml must not change what is drawn.
        env["AUTOBROWSER_CONFIG_FILE"] = str(ROOT / "tests" / "ui" / "conpty_profile.yaml")
        self.process = winpty.PtyProcess.spawn(
            [sys.executable, "main.py", "--no-mcp"],
            cwd=str(ROOT),
            dimensions=(ROWS, COLUMNS),
            env=env,
        )
        self.screen = pyte.HistoryScreen(COLUMNS, ROWS, history=1000)
        self.stream = pyte.Stream(self.screen)

    def pump(self, until: str, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if until in self.text():
                return
            try:
                self.stream.feed(self.process.read(65536))
            except EOFError:
                break
            except Exception:  # noqa: BLE001 - no data yet
                time.sleep(0.05)
        raise AssertionError(f"{until!r} never appeared:\n{self.text()}")

    def settle(self, check: Callable[[Console], None], timeout: float = 10.0) -> None:
        """Keep reading until ``check`` passes: a frame may still be on its way."""

        deadline = time.monotonic() + timeout
        while True:
            try:
                check(self)
                return
            except AssertionError:
                if time.monotonic() > deadline:
                    raise
            try:
                self.stream.feed(self.process.read(65536))
            except EOFError:
                check(self)
                return
            except Exception:  # noqa: BLE001 - no data yet
                time.sleep(0.05)

    def lines(self) -> list[str]:
        history = ["".join(char.data for char in line.values()).rstrip() for line in self.screen.history.top]
        return [*history, *(line.rstrip() for line in self.screen.display)]

    def text(self) -> str:
        return "\n".join(self.lines())

    def send(self, text: str) -> None:
        self.process.write(text)

    def wait_exit(self, timeout: float = 15.0) -> int:
        deadline = time.monotonic() + timeout
        while self.process.isalive() and time.monotonic() < deadline:
            try:
                self.stream.feed(self.process.read(65536))
            except EOFError:
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.05)
        assert not self.process.isalive(), "the TUI did not exit"
        return int(self.process.exitstatus or 0)


@pytest.fixture
def console() -> Console:
    terminal = Console()
    yield terminal
    if terminal.process.isalive():
        terminal.process.terminate(force=True)


def assert_input_field(console: Console) -> None:
    """Exactly one input field — rule, prompt, rule, status — with the cursor in it, and
    nothing left of an older frame below or above it."""

    display = [line.rstrip() for line in console.screen.display]
    filled = [index for index, line in enumerate(display) if line]
    status = filled[-1]
    assert "готов" in display[status]
    assert display[status - 2].startswith("›")
    rules = [index for index, line in enumerate(display) if line and set(line) == {"─"}]
    assert rules == [status - 3, status - 1]
    assert console.screen.cursor.y == status - 2


def test_tui_starts_answers_commands_and_exits_cleanly(console: Console) -> None:
    console.pump("Опишите задачу словами")
    assert any("AutoBrowser" in line for line in console.screen.display)
    console.settle(assert_input_field)

    console.send("/help\r")
    console.pump("/exit                    выйти")
    console.send("/tasks\r")
    console.pump("нет задач")

    lines = console.lines()
    assert sum("Команды" == line.strip() for line in lines) == 1
    assert sum(line.strip() == "нет задач" for line in lines) == 1
    console.settle(assert_input_field)

    console.send("/exit\r")
    assert console.wait_exit() == 0
