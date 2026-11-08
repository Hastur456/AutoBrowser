"""The worker the stand drives: one session behind its :class:`SessionController`."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from src.contracts import RunProfile
from src.ui.views import SessionView, TaskOutcome

_LOG = logging.getLogger(__name__)

#: MCP connection states (``ServerView.state``) after which a server is restarted before a run.
UNHEALTHY_SERVER_STATES = frozenset({"failed", "disconnected"})


class WorkerPort(Protocol):
    """What the stand needs of :class:`src.ui.controller.SessionController` (tests: a fake)."""

    async def start(self) -> Any: ...
    async def run_outcome(self, text: str, *, profile: RunProfile | None = None) -> TaskOutcome: ...
    async def cancel(self) -> bool: ...
    async def reset(self) -> Any: ...
    async def reconnect(self, server: str) -> Any: ...
    def session_view(self) -> SessionView: ...
    async def tool_names(self) -> tuple[str, ...]: ...
    def has_recording(self, name: str) -> bool: ...
    async def shutdown(self, timeout_seconds: float) -> None: ...
    def answer(self, ticket_id: str, answer: str, *, via: str) -> bool: ...


async def ensure_healthy(worker: WorkerPort) -> None:
    """Before a run: restart a failed MCP server; when that fails, recreate the session."""

    for server in worker.session_view().servers:
        if server.state not in UNHEALTHY_SERVER_STATES:
            continue
        try:
            await worker.reconnect(server.name)
        except Exception:  # noqa: BLE001 - any failure: a fresh session instead
            _LOG.warning("MCP server %s did not reconnect; recreating the session", server.name)
            await worker.reset()
            return


__all__ = ["UNHEALTHY_SERVER_STATES", "WorkerPort", "ensure_healthy"]
