"""Chrome CDP process helpers for harness-managed browser sessions."""

from __future__ import annotations

import asyncio
import socket
import subprocess
from typing import Any


def is_port_open(port: int) -> bool:
    """Return whether a localhost TCP port is accepting connections."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("localhost", port)) == 0


def start_chrome_cdp(
    chrome_path: str,
    user_data_dir: str,
    port: int,
) -> subprocess.Popen[Any] | None:
    """Start Chrome with CDP enabled unless the port is already open."""

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

    return subprocess.Popen(
        [
            chrome_path,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
    )


async def wait_for_port(port: int, timeout_seconds: float) -> None:
    """Wait until a localhost TCP port opens."""

    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not is_port_open(port):
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Chrome CDP port {port} did not open in time.")
        print("Подключение к серверу...")
        await asyncio.sleep(0.5)


__all__ = ["is_port_open", "start_chrome_cdp", "wait_for_port"]
