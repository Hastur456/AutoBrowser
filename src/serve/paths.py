"""Where the stand keeps its files, all under ``storage.root_dir``."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.config import ServeSettings, StorageSettings

LEDGER_FILE = "ledger.jsonl"
EVENTS_FILE = "events.jsonl"
#: Profile folder of worker ``i`` under ``serve.profiles_dir``.
WORKER_PROFILE = "w{index}"


@dataclass(frozen=True)
class ServePaths:
    root: Path
    ledger: Path
    events: Path
    profiles: Path

    def worker_profile(self, index: int) -> Path:
        return self.profiles / WORKER_PROFILE.format(index=index)


def serve_paths(serve: ServeSettings, storage: StorageSettings) -> ServePaths:
    root = Path(storage.root_dir) / serve.dir
    return ServePaths(
        root=root,
        ledger=root / LEDGER_FILE,
        events=root / EVENTS_FILE,
        profiles=Path(storage.root_dir) / serve.profiles_dir,
    )


__all__ = ["EVENTS_FILE", "LEDGER_FILE", "ServePaths", "serve_paths"]
