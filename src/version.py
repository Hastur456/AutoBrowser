"""The installed version of AutoBrowser (a neutral leaf: imports nothing from ``src``)."""

from __future__ import annotations

from importlib import metadata

#: Distribution name in ``pyproject.toml``.
PACKAGE_NAME = "autobrowser"


def package_version() -> str:
    """The installed distribution's version, ``""`` when the package is not installed."""

    try:
        return metadata.version(PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return ""


__all__ = ["PACKAGE_NAME", "package_version"]
