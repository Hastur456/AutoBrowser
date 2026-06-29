from langgraph.graph import StateGraph, END, START
from langchain.chat_models import BaseChatModel
from langchain.messages import HumanMessage

from ..subgraphs.planner.workflow import PlannerWorkflow
from ..subgraphs.executor.nodes import mcp_invoke_node, backoff_node
from ..subgraphs.planner.state import PlannerState
from .state import AgentState
from .nodes import observe_node, vision_node, human_input_node, reflect_node
from .routers import reflect_router


class AgentWorkflow:
    """
    START → plan → execute → mcp → observe → vision → reflect → (router)
                      ↑                                              |
                      └── continue ──────────────────────────────────┘
                      └── replan      → plan
                      └── retry       → backoff → mcp
                      └── human       → human_input → execute
                      └── done/fatal  → END
    """

    def __init__(
        self,
        llm: BaseChatModel,
        tools: list,
        max_retries: int = 3,
    ):
        self.planner = PlannerWorkflow(llm=llm, tools=tools)
        self.llm = llm
        self.tools = tools
        self.max_retries = max_retries
        self.graph = self._build_graph()

    async def plan(self, state: AgentState) -> dict:
        replan_count = state.get("replan_count", 0)
        planner_state = PlannerState(
            messages=state["messages"],
            current_plan="",
        )
        result = await self.planner.run(planner_state)
        return {
            "messages": result.get("messages", []),
            "plan_steps": result.get("plan_steps"),
            "replan_count": replan_count + 1,
        }

    async def execute(self, state: AgentState) -> dict:
        messages = list(state.get("messages", []))

        plan_steps = state.get("plan_steps")
        steps = list(plan_steps.steps if hasattr(plan_steps, "steps") else (plan_steps or []))

        # Advance: drop the completed step when coming from a successful reflect
        if steps and state.get("reflection") == "continue":
            steps = steps[1:]

        if not steps:
            return {"reflection": "done", "plan_steps": None}

        step = steps[0]
        desc = step.description if hasattr(step, "description") else step.get("description", "")
        tool_hint = step.estimated_tool if hasattr(step, "estimated_tool") else step.get("estimated_tool", "")
        hint = f"Execute: {desc}"
        if tool_hint:
            hint += f" (use {tool_hint})"
        messages = messages + [HumanMessage(content=hint)]

        if hasattr(plan_steps, "steps"):
            plan_steps.steps = steps
        else:
            plan_steps = steps

        llm_with_tools = self.llm.bind_tools(self.tools)
        response = await llm_with_tools.ainvoke(messages)
        return {"messages": [response], "plan_steps": plan_steps}

    async def mcp(self, state: AgentState) -> dict:
        return await mcp_invoke_node(state, self.tools)

    async def backoff(self, state: AgentState) -> dict:
        return await backoff_node(state)

    async def observe(self, state: AgentState) -> dict:
        return await observe_node(state, self.tools)

    async def vision(self, state: AgentState) -> dict:
        return await vision_node(state, self.llm)

    async def human_input(self, state: AgentState) -> dict:
        return await human_input_node(state)

    async def reflect(self, state: AgentState) -> dict:
        return await reflect_node(state, self.llm, self.max_retries)

    def _reflect_router(self, state: AgentState) -> str:
        return reflect_router(state)

    def _execute_router(self, state: AgentState) -> str:
        """Short-circuit to END when plan produced no steps."""
        plan_steps = state.get("plan_steps")
        steps = plan_steps.steps if hasattr(plan_steps, "steps") else (plan_steps or [])
        if not steps:
            return END
        return "mcp"

    def _build_graph(self):
        workflow = StateGraph(AgentState)

        workflow.add_node("plan", self.plan)
        workflow.add_node("execute", self.execute)
        workflow.add_node("mcp", self.mcp)
        workflow.add_node("backoff", self.backoff)
        workflow.add_node("observe", self.observe)
        workflow.add_node("vision", self.vision)
        workflow.add_node("human_input", self.human_input)
        workflow.add_node("reflect", self.reflect)

        workflow.add_edge(START, "plan")
        workflow.add_edge("plan", "execute")
        workflow.add_conditional_edges(
            "execute",
            self._execute_router,
            {"mcp": "mcp", END: END},
        )
        workflow.add_edge("mcp", "observe")
        workflow.add_edge("observe", "vision")
        workflow.add_edge("vision", "reflect")

        workflow.add_conditional_edges(
            "reflect",
            self._reflect_router,
            {
                "execute": "execute",
                "plan": "plan",
                "backoff": "backoff",
                "human_input": "human_input",
                END: END,
            },
        )

        workflow.add_edge("backoff", "mcp")
        workflow.add_edge("human_input", "execute")

        return workflow.compile()

    async def stream(self, state: AgentState):
        """Yield (node_name, update) after each node execution."""
        async for chunk in self.graph.astream(state, stream_mode="updates"):
            for node_name, update in chunk.items():
                yield node_name, update

    async def run(self, state: AgentState) -> dict:
        return await self.graph.ainvoke(state)
