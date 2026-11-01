"""The sync ledger of a batch, ``<batch_dir>/platform_sync.json``, and its lock (§5.6.1).

The ledger is the bridge's memory: which experiment, dataset version and baseline a batch
was synced as, the state of every item trace (``pending``/``sending``/``unknown``/
``exported``), the score batch and the annotation queue. It holds only hosts, ids and
states — never keys. Writes are atomic (a temporary file and ``os.replace``, retried once,
for a Windows antivirus or indexer holding the file); one sync per batch at a time is
enforced by an ``O_EXCL`` lock file that goes stale after twice the sync budget.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LEDGER_FILE_NAME = "platform_sync.json"
LOCK_FILE_NAME = "platform_sync.lock"
LEDGER_SCHEMA_VERSION = 1
PLATFORM = "langfuse"

PENDING, SENDING, UNKNOWN, EXPORTED = "pending", "sending", "unknown", "exported"
COMPLETE = "complete"


class LedgerLocked(Exception):
    """Another sync of the same batch holds the lock."""


class SyncLedger:
    def __init__(self, batch_dir: Path, data: dict[str, Any]) -> None:
        self.path = Path(batch_dir) / LEDGER_FILE_NAME
        self.data = data

    @classmethod
    def load(cls, batch_dir: Path) -> SyncLedger:
        path = Path(batch_dir) / LEDGER_FILE_NAME
        if path.is_file():
            return cls(batch_dir, json.loads(path.read_text(encoding="utf-8")))
        return cls(batch_dir, cls.empty())

    @staticmethod
    def empty() -> dict[str, Any]:
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "platform": PLATFORM,
            "host": None,
            "project_id": None,
            "experiment": None,
            "dataset": None,
            "baseline_batch_id": None,
            "items": {},
            "scores": None,
            "triage": None,
            "state": PENDING,
            "last_error": None,
            "updated_at": None,
        }

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @property
    def items(self) -> dict[str, dict[str, Any]]:
        return self.data.setdefault("items", {})

    def save(self, *, retry_after_seconds: float, now: datetime | None = None) -> None:
        self.data["updated_at"] = (now or datetime.now(UTC)).isoformat()
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.replace(temporary, self.path)
        except PermissionError:
            time.sleep(retry_after_seconds)
            os.replace(temporary, self.path)


@contextlib.contextmanager
def ledger_lock(
    batch_dir: Path,
    *,
    stale_after_seconds: float,
    now: Callable[[], datetime],
) -> Iterator[None]:
    """Hold ``platform_sync.lock`` for one sync; a lock older than ``stale_after_seconds`` is
    abandoned (a killed sync) and taken over."""

    path = Path(batch_dir) / LOCK_FILE_NAME
    for _ in range(2):
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not _stale(path, stale_after_seconds, now()):
                raise LedgerLocked(f"another sync of {Path(batch_dir).name} is running ({path.name})") from None
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump({"pid": os.getpid(), "at": now().isoformat()}, file)
        break
    else:  # pragma: no cover - a racing sync took the stale lock first
        raise LedgerLocked(f"another sync of {Path(batch_dir).name} is running ({path.name})")
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _stale(path: Path, stale_after_seconds: float, now: datetime) -> bool:
    try:
        taken = datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return True  # unreadable: written by a sync that died mid-write
    return (now - taken).total_seconds() > stale_after_seconds
