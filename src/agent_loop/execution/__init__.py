"""Engine-native execution package for the explicit AutoBrowser agent loop.

This sub-package owns the control flow of the sole runtime: planning, reasoning,
routing, execution, and observation drive the explicit :class:`AgentLoopEngine`
in :mod:`src.agent_loop.execution.loop`. There is no compiled graph; the legacy
graph runtime was removed.

The modules here operate on the typed :class:`~src.agent_loop.execution.state.LoopState`
dataclass and stay server-neutral: they import nothing from ``src/browser/`` and compare tool
names exactly as exposed. Neutral contracts come from :mod:`src.contracts`, message builders
from :mod:`src.harness.memory`, and everything server-specific (tool roles, permission
resources, call normalizers) arrives through
:class:`~src.agent_loop.execution.resources.EngineResources`.
"""

from __future__ import annotations

from src.agent_loop.execution.guards import (
    blocked_response,
    done_response,
    guard_tool_request,
    progress_block_reason,
    replan_response,
    request_tracking_update,
    terminal_guard,
    tool_block_updates,
    tool_gate_updates,
    tool_request_update,
)
from src.agent_loop.execution.completion import native_latest_state_loader
from src.agent_loop.execution.loop import (
    AgentLoopEngine,
    AgentLoopResult,
    HumanInputCallback,
    TurnController,
    TurnResult,
    native_task_runner,
)
from src.agent_loop.execution.observation import (
    ObservationCompiler,
    compile_observation,
)
from src.agent_loop.execution.resources import EngineResources
from src.agent_loop.execution.state import (
    BrowserState,
    LoopState,
)
from src.agent_loop.execution.tools import ToolBroker

__all__ = [
    "AgentLoopEngine",
    "AgentLoopResult",
    "BrowserState",
    "EngineResources",
    "HumanInputCallback",
    "LoopState",
    "ObservationCompiler",
    "ToolBroker",
    "TurnController",
    "TurnResult",
    "blocked_response",
    "compile_observation",
    "done_response",
    "guard_tool_request",
    "native_latest_state_loader",
    "native_task_runner",
    "progress_block_reason",
    "replan_response",
    "request_tracking_update",
    "terminal_guard",
    "tool_block_updates",
    "tool_gate_updates",
    "tool_request_update",
]
