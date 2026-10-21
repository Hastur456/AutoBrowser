"""While a front end owns the terminal, nothing else may write to it (B2, §6.2)."""

from __future__ import annotations

import logging
import sys
import threading
import warnings
from pathlib import Path

import pytest

from src.config import UiSettings
from src.ui import launch
from src.ui.logging_setup import terminal_quiet
from src.ui.tui.terminal import TerminalCaps


def test_logging_warnings_and_thread_errors_go_to_the_log_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_file = tmp_path / "logs" / "autobrowser.log"
    before = list(logging.getLogger().handlers)
    hooks = (sys.excepthook, threading.excepthook)

    with terminal_quiet(log_file):
        logging.getLogger("some.library").warning("library warning from main")

        def noisy() -> None:
            logging.getLogger("worker").error("worker error")
            warnings.warn("deprecated thing", UserWarning, stacklevel=1)
            raise RuntimeError("thread blew up")

        thread = threading.Thread(target=noisy, name="noisy")
        thread.start()
        thread.join()
        sys.excepthook(ValueError, ValueError("main blew up"), None)

    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    text = log_file.read_text(encoding="utf-8")
    for expected in (
        "library warning from main",
        "worker error",
        "deprecated thing",
        "thread blew up",
        "main blew up",
    ):
        assert expected in text
    assert logging.getLogger().handlers == before
    assert (sys.excepthook, threading.excepthook) == hooks


def test_session_output_is_relayed_to_the_attached_front_end() -> None:
    relay = launch.OutputRelay()
    relay("before", "attach")  # goes to the log, never to the terminal
    shown: list[str] = []
    detach = relay.attach(shown.append)
    relay("Подключение", "к серверу")
    detach()
    relay("after")

    assert shown == ["Подключение к серверу"]


def test_a_tui_that_cannot_start_falls_back_to_plain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Broken:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise OSError("No console screen buffer")

    ran: list[str] = []

    class FakePlain:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def notice(self, _text: str) -> None:
            pass

        def run(self) -> int:
            ran.append("plain")
            return 0

    import src.ui.tui.app as app_module

    monkeypatch.setattr(app_module, "TuiApp", Broken)
    monkeypatch.setattr(launch, "PlainFrontend", FakePlain)
    caps = TerminalCaps(
        level="full", color="truecolor", unicode=True, sync_output=False, hyperlinks=False,
        title=False, width=80, height=24, reason="test",
    )

    code = launch.run_frontend(
        object(), object(), settings=UiSettings(), log_file=tmp_path / "ui.log", caps=caps  # type: ignore[arg-type]
    )

    assert code == 0 and ran == ["plain"]
    assert "terminal UI unavailable (OSError" in capsys.readouterr().out
    assert "No console screen buffer" in (tmp_path / "ui.log").read_text(encoding="utf-8")
