"""Terminal capability detection for the terminal UI (§6.3 of the terminal UI design).

:func:`detect_terminal` is pure: it reads only the settings, an environment mapping and the
observed :class:`TerminalStreams`, so every row of the terminal matrix is testable without a
real console. :meth:`TerminalStreams.from_process` is the one place that looks at the process.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TextIO

from src.config import UiSettings

SupportLevel = Literal["full", "degraded", "plain"]
ColorDepth = Literal["none", "16", "256", "truecolor"]

#: First Windows build whose conhost renders 256 colours and VT sequences reliably.
WINDOWS_11_BUILD = 22000

#: ``TERM_PROGRAM`` values of hosts that support synchronized output (DEC 2026).
_SYNC_OUTPUT_HOSTS = frozenset({"wezterm", "iterm.app", "ghostty", "kitty"})


@dataclass(frozen=True)
class TerminalStreams:
    """What the process observed about its standard streams and console."""

    stdin_tty: bool
    stdout_tty: bool
    encoding: str
    #: A Win32 console is attached (conhost, Windows Terminal or ConPTY).
    win32_console: bool = False
    #: The Windows build number on Windows, else ``None``.
    windows_build: int | None = None
    width: int = 80
    height: int = 24

    @classmethod
    def from_process(
        cls, stdin: TextIO | None = None, stdout: TextIO | None = None
    ) -> TerminalStreams:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        size = shutil.get_terminal_size()
        build: int | None = None
        console = False
        if sys.platform == "win32":
            build = sys.getwindowsversion().build
            console = _has_win32_console()
        return cls(
            stdin_tty=_isatty(stdin),
            stdout_tty=_isatty(stdout),
            encoding=str(getattr(stdout, "encoding", "") or ""),
            win32_console=console,
            windows_build=build,
            width=size.columns,
            height=size.lines,
        )


@dataclass(frozen=True)
class TerminalCaps:
    """The detected support level and features; ``reason`` explains it for ``/doctor``."""

    level: SupportLevel
    color: ColorDepth
    #: UTF-8-capable output and a font with the chrome glyphs.
    unicode: bool
    #: Synchronized output (DEC 2026).
    sync_output: bool
    #: OSC 8 hyperlinks.
    hyperlinks: bool
    #: OSC 0 window title.
    title: bool
    width: int
    height: int
    reason: str
    #: The detected host, for ``/doctor`` (``windows-terminal``, ``vscode``, ``conhost``...).
    host: str = ""


def detect_terminal(
    settings: UiSettings, env: Mapping[str, str], streams: TerminalStreams
) -> TerminalCaps:
    """Detect the support level in the order of §6.3, then apply the settings' overrides."""

    width, height = max(streams.width, 1), max(streams.height, 1)

    plain_reason = _plain_reason(settings, env, streams)
    if plain_reason is not None:
        return TerminalCaps(
            level="plain",
            color="none",
            unicode=_utf8(streams.encoding),
            sync_output=False,
            hyperlinks=False,
            title=False,
            width=width,
            height=height,
            reason=plain_reason,
            host="plain",
        )

    host, level, host_color, unicode, sync, reason = _host_profile(env, streams)
    color = _color_depth(settings, env, host_color)
    if settings.glyphs == "unicode":
        unicode = True
    elif settings.glyphs == "ascii":
        unicode = False
    if settings.sync_output == "on":
        sync = True
    elif settings.sync_output == "off":
        sync = False
    return TerminalCaps(
        level=level,
        color=color,
        unicode=unicode,
        sync_output=sync,
        hyperlinks=level == "full",
        title=True,
        width=width,
        height=height,
        reason=reason,
        host=host,
    )


def _plain_reason(
    settings: UiSettings, env: Mapping[str, str], streams: TerminalStreams
) -> str | None:
    if settings.frontend == "plain":
        return "ui.frontend=plain"
    if not streams.stdin_tty or not streams.stdout_tty:
        which = "stdin" if not streams.stdin_tty else "stdout"
        return f"{which} is not a terminal"
    if env.get("TERM", "").lower() == "dumb":
        return "TERM=dumb"
    if env.get("MSYSTEM") and streams.windows_build is not None and not streams.win32_console:
        return (
            "mintty without a console (Git Bash): run in Windows Terminal, "
            "via `winpty autobrowser`, or with MSYS=enable_pcon"
        )
    return None


def _host_profile(
    env: Mapping[str, str], streams: TerminalStreams
) -> tuple[str, SupportLevel, ColorDepth, bool, bool, str]:
    """``(host, level, colour, unicode, sync output, reason)`` of the terminal host."""

    utf8 = _utf8(streams.encoding) or streams.win32_console
    program = env.get("TERM_PROGRAM", "").lower()
    if env.get("TMUX") or env.get("STY"):
        return "multiplexer", "degraded", "256", utf8, False, "tmux/screen: no DEC 2026"
    if env.get("WT_SESSION"):
        return "windows-terminal", "full", "truecolor", True, True, "Windows Terminal"
    if program == "vscode":
        return "vscode", "full", "truecolor", utf8, False, "VS Code terminal: DEC 2026 off"
    if streams.windows_build is not None:
        if env.get("MSYSTEM"):
            return "mintty-conpty", "degraded", "256", utf8, False, "Git Bash with ConPTY"
        if streams.windows_build >= WINDOWS_11_BUILD:
            return "conhost", "degraded", "256", False, False, "conhost (Windows 11)"
        return "conhost", "degraded", "16", False, False, "conhost (Windows 10)"
    if program in _SYNC_OUTPUT_HOSTS or env.get("KITTY_WINDOW_ID"):
        return program or "kitty", "full", "truecolor", utf8, True, f"{program or 'kitty'}"
    colorterm = env.get("COLORTERM", "").lower()
    depth: ColorDepth = "truecolor" if colorterm in {"truecolor", "24bit"} else "256"
    return program or "posix", "full", depth, utf8, False, program or "POSIX terminal"


def _color_depth(settings: UiSettings, env: Mapping[str, str], host: ColorDepth) -> ColorDepth:
    if "NO_COLOR" in env and env.get("NO_COLOR", "") != "":
        return "none"
    if settings.color != "auto":
        return settings.color
    if env.get("COLORTERM", "").lower() in {"truecolor", "24bit"} and host != "16":
        return "truecolor"
    return host


def _utf8(encoding: str) -> bool:
    return encoding.lower().replace("-", "").replace("_", "") in {"utf8", "utf8sig", "cp65001"}


def _isatty(stream: TextIO | None) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def _has_win32_console() -> bool:
    """Whether a Win32 console is attached (false under mintty without ConPTY)."""

    try:
        import ctypes

        return bool(ctypes.windll.kernel32.GetConsoleWindow())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def process_env() -> Mapping[str, str]:
    """The process environment, for :func:`detect_terminal`."""

    return dict(os.environ)


__all__ = [
    "ColorDepth",
    "SupportLevel",
    "TerminalCaps",
    "TerminalStreams",
    "detect_terminal",
    "process_env",
]
