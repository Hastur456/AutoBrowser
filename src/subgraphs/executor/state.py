"""State of executor subgraph"""


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


class ExecutorState(TypedDict):
    """
    Executor's state:

    messages: list of BaseMessages interfaces whose are answers of LLM provider
    error_count: number of errrors when calling tools
    retry_attemps: number of retries when calling tools 
    last_action: last tool calling action
    """
    messages: Annotated[
        List[SystemMessage | HumanMessage | AIMessage | ToolMessage],
        add_messages
    ]
    error_count: int
    retry_attempts: int
    total_tool_calls: int
    last_error_type: str | None
    last_action: Any | None
