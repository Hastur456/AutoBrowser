"""Public entry point for the engine-native AutoBrowser execution loop.

Historically this module held a thin compatibility shell that wrapped
:class:`~src.agent_loop.goals.GoalRunner`. That shell is gone: the real control-flow owner now
lives in :mod:`src.agent_loop.execution.loop`, and this module simply re-exports it so existing
imports (``from src.agent_loop.engine import AgentLoopEngine, AgentLoopResult``) keep working.
"""

from __future__ import annotations

from src.agent_loop.execution.loop import (
    DEFAULT_TURN_CAP,
    AgentLoopEngine,
    AgentLoopResult,
    HumanInputCallback,
    TurnController,
    TurnResult,
    native_task_runner,
)

__all__ = [
    "DEFAULT_TURN_CAP",
    "AgentLoopEngine",
    "AgentLoopResult",
    "HumanInputCallback",
    "TurnController",
    "TurnResult",
    "native_task_runner",
]
