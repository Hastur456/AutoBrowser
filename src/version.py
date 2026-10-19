"""The installed version of AutoBrowser (a neutral leaf: imports nothing from ``src``)."""

from __future__ import annotations

from functools import cache
from importlib import metadata

#: Distribution name in ``pyproject.toml``.
PACKAGE_NAME = "autobrowser"


@cache
def package_version() -> str:
    """The installed distribution's version, ``""`` when the package is not installed.

    Cached: the status line asks every second, and reading distribution metadata scans the
    environment on the session loop."""

    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return ""


__all__ = ["PACKAGE_NAME", "package_version"]
