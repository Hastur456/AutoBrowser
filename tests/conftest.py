"""Suite-wide isolation from the developer's AutoBrowser configuration and data.

Every test gets its own empty ``AUTOBROWSER_HOME`` under ``tmp_path``, and the checkout's
legacy ``config.yaml`` / ``.env`` (a personal profile) plus any ``AUTOBROWSER_CONFIG_FILE``
are hidden. So sessions, memory, logs and the input history of a test run never land in the
real home or in a ``storage.root_dir`` the personal profile points at, and no personal
setting (memory, hooks, permissions, a model) changes what a test sees. A test that needs a
profile writes one and sets ``AUTOBROWSER_CONFIG_FILE`` itself. Settings are re-read around
each test because the home and the files are resolved when ``Settings`` is built.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from src import config


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    home = tmp_path / "autobrowser-home"
    monkeypatch.setenv(config.HOME_ENV_VAR, str(home))
    monkeypatch.delenv(config.CONFIG_FILE_ENV_VAR, raising=False)
    monkeypatch.setattr(config, "DEFAULT_CONFIG_FILE_YAML", tmp_path / "no-repo-profile" / "config.yaml")
    monkeypatch.setattr(config, "LEGACY_ENV_FILE", tmp_path / "no-repo-profile" / ".env")
    config.get_settings.cache_clear()
    yield home
    config.get_settings.cache_clear()
