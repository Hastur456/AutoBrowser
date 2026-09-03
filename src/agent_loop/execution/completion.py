"""Native session-state carry-forward for the engine-native execution path.

``native_latest_state_loader`` supplies the cross-task carry-forward the enclosing
:class:`~src.agent_loop.goals.GoalRunner` hands to
:meth:`~src.harness.session.SessionContext.state.replace`: the native loop already emits the
``SESSION_STATE_KEYS`` subset as :attr:`AgentLoopResult.session_state` (via
:meth:`LoopState.to_session_state`), so the loader just unwraps that dict. It matches the
:data:`~src.agent_loop.goals.LatestStateLoader` callable shape.

There is no completion compiler here any more — terminal status and ``final_answer`` live
directly on :class:`~src.agent_loop.execution.loop.AgentLoopResult`, so ``GoalRunner`` reads
them without an observation-compiler adapter. (The legacy compiler that mapped an ``AgentState``
into a ``GoalState`` was removed with ``src/agent_loop/outcomes.py``.)
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.agent_loop.execution.loop import AgentLoopResult


async def native_latest_state_loader(
    task_config: Mapping[str, Any],
    fallback: Any | None,
) -> dict[str, Any] | None:
    """Return the durable carry-forward state for a native run.

    The native loop's result already carries the ``SESSION_STATE_KEYS`` dict, so no harness
    state read is needed.
    """

    if isinstance(fallback, AgentLoopResult):
        return dict(fallback.session_state)
    if isinstance(fallback, Mapping):
        return dict(fallback)
    return None


__all__ = [
    "native_latest_state_loader",
]
