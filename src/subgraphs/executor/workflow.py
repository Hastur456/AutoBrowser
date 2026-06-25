from langgraph.graph import START, END, StateGraph

from . import mcp_invoke_node, backoff_node, retry_router
from . import ExecutorState


class ExecutorWorkflow:
    def __init__(
            self,
            tools,
            max_retries: int = 3
    ):
        self.tools = tools
        self.max_retries = max_retries
        self.workflow = self._build_workflow()

    async def mcp(self, state: ExecutorState):
        return await mcp_invoke_node(state, self.tools)

    async def backoff_node(self, state: ExecutorState):
        return await backoff_node(state)
    
    def retry_router(self, state: ExecutorState):
        return retry_router(state, self.max_retries)

    def _build_workflow(self):
        workflow = StateGraph(ExecutorState)

        workflow.add_node("mcp", self.mcp)
        workflow.add_node("backoff", self.backoff_node)

        workflow.add_edge(START, "mcp")

        workflow.add_conditional_edges( 
            "mcp", 
            self.retry_router, 
            {
                "backoff": "backoff",
                "abort": END,
            }, 
        )

        workflow.add_edge("backoff", "mcp")

        return workflow.compile()

    async def run(self, state: ExecutorState):
        return await self.workflow.ainvoke(state)
