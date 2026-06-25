"""Workflow of planner subgraph"""

from langgraph.graph import StateGraph, END, START 
from langchain.chat_models import BaseChatModel

from .state import PlannerState
from .nodes import task_decomposition_node, get_list_of_tools_node


class PlannerWorkflow:
    """Planner workflow subgraph"""

    def __init__(self, llm: BaseChatModel, tools: list):
        self.llm = llm
        self.tools = tools
        self.workflow = self._build_workflow()

    async def task_decomposition(self, state: PlannerState):
        return await task_decomposition_node(state, llm=self.llm, tools=self.tools)

    async def get_list_of_tools(self, state: PlannerState):
        return await get_list_of_tools_node(state, llm=self.llm, tools=self.tools)

    def _build_workflow(self) -> StateGraph:
        workflow = StateGraph(PlannerState)

        workflow.add_node("task_decomposition", self.task_decomposition)
        workflow.add_node("get_list_of_tools", self.get_list_of_tools)

        workflow.add_edge(START, "task_decomposition")
        workflow.add_edge("task_decomposition", "get_list_of_tools")
        workflow.add_edge("get_list_of_tools", END)

        return workflow.compile()

    async def run(self, state: PlannerState) -> dict:
        return await self.workflow.ainvoke(state)

