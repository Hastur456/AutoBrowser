"""The stand's time zone and file layout."""

from __future__ import annotations

import zoneinfo
from datetime import datetime
from pathlib import Path

import pytest

from src.config import ServeSettings, StorageSettings
from src.serve.clock import SystemClock, resolve_timezone
from src.serve.paths import serve_paths


def test_an_empty_zone_is_the_system_zone() -> None:
    tz = resolve_timezone("")

    assert datetime.now(tz).utcoffset() == datetime.now().astimezone().utcoffset()


@pytest.mark.skipif(not zoneinfo.available_timezones(), reason="no IANA time zone database")
def test_an_iana_zone_is_used_as_named() -> None:
    assert str(resolve_timezone("Europe/Berlin")) == "Europe/Berlin"


def test_the_system_clock_is_aware_in_its_zone() -> None:
    tz = resolve_timezone("")

    assert SystemClock(tz).now().tzinfo is tz


def test_the_stand_files_live_under_the_storage_root(tmp_path: Path) -> None:
    paths = serve_paths(ServeSettings(), StorageSettings(root_dir=tmp_path))

    assert paths.root == tmp_path / "serve"
    assert paths.ledger == tmp_path / "serve" / "ledger.jsonl"
    assert paths.events == tmp_path / "serve" / "events.jsonl"
    assert paths.worker_profile(0) == tmp_path / "serve" / "profiles" / "w0"
