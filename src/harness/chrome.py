"""Chrome CDP process helpers for harness-managed browser sessions."""

from __future__ import annotations

import asyncio
import socket
import subprocess
from collections.abc import Sequence
from typing import Any

from src.config import BrowserSettings, get_settings

#: Chrome's new headless mode (the old one is a separate, reduced browser).
HEADLESS_FLAG = "--headless=new"


def is_port_open(port: int) -> bool:
    """Return whether a localhost TCP port is accepting connections."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(get_settings().browser.cdp_poll_seconds)
        return sock.connect_ex(("localhost", port)) == 0


def chrome_flags(settings: BrowserSettings) -> list[str]:
    """The configured launch flags: ``--headless=new`` (``headless``), then ``extra_args``."""

    flags = [HEADLESS_FLAG] if settings.headless else []
    return [*flags, *settings.extra_args]


def start_chrome_cdp(
    chrome_path: str,
    user_data_dir: str,
    port: int,
    *,
    extra_args: Sequence[str] | None = None,
) -> subprocess.Popen[Any] | None:
    """Start Chrome with CDP enabled unless the port is already open.

    ``extra_args`` follow the fixed flags; ``None`` takes them from ``settings.browser``
    (:func:`chrome_flags`).
    """

    if is_port_open(port):
        return None

    if not chrome_path:
        raise RuntimeError(
            "Chrome path is not configured. Pass --chrome-path or set "
            "AUTOBROWSER_BROWSER__CHROME_PATH."
        )
    if not user_data_dir:
        raise RuntimeError(
            "Chrome user data dir is not configured. Pass --user-data-dir or set "
            "AUTOBROWSER_BROWSER__USER_DATA_DIR."
        )

    flags = list(extra_args) if extra_args is not None else chrome_flags(get_settings().browser)
    return subprocess.Popen(
        [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            *flags,
        ],
        # Chrome prints "DevTools listening on ..." and GPU warnings; nothing of ours to
        # show, and a stray writer would tear the terminal UI.
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def wait_for_port(port: int, timeout_seconds: float) -> None:
    """Wait until a localhost TCP port opens."""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not is_port_open(port):
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Chrome CDP port {port} did not open in time.")
        await asyncio.sleep(get_settings().browser.cdp_poll_seconds)


__all__ = ["HEADLESS_FLAG", "chrome_flags", "is_port_open", "start_chrome_cdp", "wait_for_port"]
