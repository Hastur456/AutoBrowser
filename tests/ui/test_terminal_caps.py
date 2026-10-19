from __future__ import annotations

import pytest

from src.config import UiSettings
from src.ui.tui.terminal import TerminalStreams, detect_terminal

WIN11 = 26100
WIN10 = 19045


def streams(
    *,
    tty: bool = True,
    encoding: str = "utf-8",
    console: bool = False,
    build: int | None = None,
) -> TerminalStreams:
    return TerminalStreams(
        stdin_tty=tty,
        stdout_tty=tty,
        encoding=encoding,
        win32_console=console,
        windows_build=build,
        width=100,
        height=30,
    )


@pytest.mark.parametrize(
    "env, observed, level, color, unicode, sync",
    [
        ({"WT_SESSION": "x"}, streams(console=True, build=WIN11), "full", "truecolor", True, True),
        ({"TERM_PROGRAM": "vscode"}, streams(console=True, build=WIN11), "full", "truecolor", True, False),
        ({}, streams(console=True, build=WIN11, encoding="cp1251"), "degraded", "256", False, False),
        ({}, streams(console=True, build=WIN10), "degraded", "16", False, False),
        ({"MSYSTEM": "MINGW64"}, streams(console=True, build=WIN11), "degraded", "256", True, False),
        ({"TERM_PROGRAM": "WezTerm"}, streams(), "full", "truecolor", True, True),
        ({"COLORTERM": "truecolor"}, streams(), "full", "truecolor", True, False),
        ({"TERM": "xterm-256color"}, streams(), "full", "256", True, False),
        ({"TMUX": "/tmp/tmux"}, streams(), "degraded", "256", True, False),
    ],
)
def test_terminal_matrix(
    env: dict[str, str],
    observed: TerminalStreams,
    level: str,
    color: str,
    unicode: bool,
    sync: bool,
) -> None:
    caps = detect_terminal(UiSettings(), env, observed)

    assert (caps.level, caps.color, caps.unicode, caps.sync_output) == (level, color, unicode, sync)
    assert caps.reason
    assert (caps.width, caps.height) == (100, 30)


@pytest.mark.parametrize(
    "env, observed, settings, reason",
    [
        ({}, streams(tty=False), UiSettings(), "stdin is not a terminal"),
        ({"TERM": "dumb"}, streams(), UiSettings(), "TERM=dumb"),
        ({"MSYSTEM": "MINGW64"}, streams(console=False, build=WIN11), UiSettings(), "mintty"),
        ({"WT_SESSION": "x"}, streams(), UiSettings(frontend="plain"), "ui.frontend=plain"),
    ],
)
def test_plain_level(
    env: dict[str, str], observed: TerminalStreams, settings: UiSettings, reason: str
) -> None:
    caps = detect_terminal(settings, env, observed)

    assert caps.level == "plain"
    assert caps.color == "none"
    assert reason in caps.reason


def test_no_color_and_overrides() -> None:
    wt = streams(console=True, build=WIN11)

    assert detect_terminal(UiSettings(), {"WT_SESSION": "x", "NO_COLOR": "1"}, wt).color == "none"
    # An empty NO_COLOR does not count (no-color.org).
    assert detect_terminal(UiSettings(), {"WT_SESSION": "x", "NO_COLOR": ""}, wt).color == "truecolor"
    overridden = detect_terminal(
        UiSettings(color="16", glyphs="ascii", sync_output="off"), {"WT_SESSION": "x"}, wt
    )
    assert (overridden.color, overridden.unicode, overridden.sync_output) == ("16", False, False)
    forced = detect_terminal(
        UiSettings(glyphs="unicode", sync_output="on"), {}, streams(console=True, build=WIN10)
    )
    assert (forced.unicode, forced.sync_output) == (True, True)
