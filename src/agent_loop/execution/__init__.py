"""Engine-native execution package for the explicit AutoBrowser agent loop.

This sub-package owns the control flow of the sole runtime: planning, reasoning,
routing, execution, and observation drive the explicit :class:`AgentLoopEngine`
in :mod:`src.agent_loop.execution.loop`. There is no compiled graph; the legacy
graph runtime was removed.

The modules here operate on the typed :class:`~src.agent_loop.execution.state.LoopState`
dataclass instead of the legacy ``AgentState`` TypedDict, and import **nothing** from
``src/agent/``: neutral contracts/thresholds come from :mod:`src.contracts`, browser-schema
leaves from :mod:`src.browser.observation`, and message builders from :mod:`src.harness.memory`.
Only the stateful control logic lives here; those stateless leaves are reused as-is.
"""

from __future__ import annotations

from src.agent_loop.execution.guards import (
    REPEATED_SNAPSHOT_FINAL_ANSWER,
    SNAPSHOT_REUSE_MARKERS,
    blocked_response,
    done_response,
    fresh_snapshot_request,
    guard_tool_request,
    pending_tab_activation_request,
    replan_response,
    request_tracking_update,
    stale_snapshot_retry_update,
    terminal_guard,
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
from src.agent_loop.execution.policy import (
    BLOCKED_TOOL_MARKERS,
    classify_tool_request,
    policy_updates,
)
from src.agent_loop.execution.resources import EngineResources
from src.agent_loop.execution.state import (
    MAX_CONSECUTIVE_FAILURES,
    MAX_REPLANS,
    MAX_STEPS_WITHOUT_PLAN_ADVANCE,
    MAX_UNCHANGED_SNAPSHOTS,
    BrowserState,
    LoopState,
)
from src.agent_loop.execution.tools import ToolBroker

__all__ = [
    "BLOCKED_TOOL_MARKERS",
    "MAX_CONSECUTIVE_FAILURES",
    "MAX_REPLANS",
    "MAX_STEPS_WITHOUT_PLAN_ADVANCE",
    "MAX_UNCHANGED_SNAPSHOTS",
    "REPEATED_SNAPSHOT_FINAL_ANSWER",
    "SNAPSHOT_REUSE_MARKERS",
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
    "classify_tool_request",
    "compile_observation",
    "done_response",
    "fresh_snapshot_request",
    "guard_tool_request",
    "native_latest_state_loader",
    "native_task_runner",
    "pending_tab_activation_request",
    "policy_updates",
    "replan_response",
    "request_tracking_update",
    "stale_snapshot_retry_update",
    "terminal_guard",
    "tool_request_update",
]
