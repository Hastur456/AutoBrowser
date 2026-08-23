"""Native completion mapping for the engine-native execution path.

Replaces :class:`~src.agent_loop.outcomes.LegacyAgentStateObservationCompiler` for the native
loop. Where the legacy compiler inspects an ``AgentState`` mapping to decide the terminal
:class:`~src.agent_loop.outcomes.CompletionStatus`, :class:`NativeObservationCompiler` reads the
status the loop already computed on :class:`~src.agent_loop.execution.loop.EngineRunResult` — so
the native path never constructs an ``AgentState`` and never touches ``outcomes.py`` internals
beyond the public :class:`~src.agent_loop.outcomes.GoalState` it must return.

``native_latest_state_loader`` supplies the cross-task carry-forward: the loop already emits the
``SESSION_STATE_KEYS`` subset as ``EngineRunResult.session_state`` (via
:meth:`LoopState.to_session_state`), so the loader just hands that dict back to
:meth:`SessionContext.state.replace` through the ``GoalRunner``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.agent_loop.outcomes import CompletionStatus, GoalState
from src.agent_loop.execution.loop import EngineRunResult


class NativeObservationCompiler:
    """Map an :class:`EngineRunResult` to a :class:`GoalState` with no ``AgentState``."""

    def compile(
        self,
        *,
        latest_state: Mapping[str, Any] | None,
        result: Any,
    ) -> GoalState:
        status: CompletionStatus
        if isinstance(result, EngineRunResult):
            status = result.status
        else:
            # Defensive: an unexpected result shape cannot be treated as terminal-done.
            status = "blocked"
        return GoalState(status=status, latest_state=latest_state, result=result)


async def native_latest_state_loader(
    task_config: Mapping[str, Any],
    fallback: Any | None,
) -> dict[str, Any] | None:
    """Return the durable carry-forward state for a native run.

    Matches :data:`src.agent_loop.goals.LatestStateLoader`. The native loop's result already
    carries the ``SESSION_STATE_KEYS`` dict, so no harness state read is needed.
    """

    if isinstance(fallback, EngineRunResult):
        return dict(fallback.session_state)
    if isinstance(fallback, Mapping):
        return dict(fallback)
    return None


__all__ = [
    "NativeObservationCompiler",
    "native_latest_state_loader",
]
