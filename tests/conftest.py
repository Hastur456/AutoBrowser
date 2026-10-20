"""Suite-wide isolation of the AutoBrowser home directory.

Every test gets its own empty ``AUTOBROWSER_HOME`` under ``tmp_path``, so sessions, memory,
logs and the input history of a test run never land in the developer's ``~/.autobrowser``,
and a ``config.yaml`` / ``.env`` there never leaks into a test. Settings are re-read
around each test because the home is resolved when ``Settings`` is built.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from src.config import HOME_ENV_VAR, get_settings


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    home = tmp_path / "autobrowser-home"
    monkeypatch.setenv(HOME_ENV_VAR, str(home))
    get_settings.cache_clear()
    yield home
    get_settings.cache_clear()
