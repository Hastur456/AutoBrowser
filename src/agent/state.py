from typing import (
    Annotated,
    TypedDict,
    List,
    Any
)
from langchain.messages import (
    SystemMessage,
    HumanMessage,
    AIMessage,
    ToolMessage,
)
from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    messages: Annotated[
        List[SystemMessage | HumanMessage | AIMessage | ToolMessage],
        add_messages
    ]
    error_count: int
    retry_attempts: int
    total_tool_calls: int
    last_error_type: str | None
    last_action: Any
    observation: str | None
    perception: str | None
    reflection: str | None
    replan_count: int
