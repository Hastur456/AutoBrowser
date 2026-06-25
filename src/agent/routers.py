from langgraph.graph import END

from .state import AgentState

MAX_REPLANS = 2
MAX_HUMAN_INPUTS = 5


def reflect_router(state: AgentState) -> str:
    action = state.get("reflection")
    replan_count = state.get("replan_count", 0)

    if action == "continue":
        return "execute"
    if action == "replan" and replan_count < MAX_REPLANS:
        return "plan"
    if action == "retry":
        return "backoff"
    if action == "human":
        return "human_input"
    return END
