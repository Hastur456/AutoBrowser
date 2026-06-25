"""Nodes of planner subgraph"""

from langchain_core.tools import render_text_description
from langchain.chat_models import BaseChatModel
from langchain.messages import SystemMessage, HumanMessage, AIMessage

from .state import PlannerState, PlanSteps
from .prompts import task_decomposition_prompt, get_list_of_tools_prompt


async def task_decomposition_node(state: PlannerState, llm: BaseChatModel, tools):
    """
    Узел декомпозиции задачи. 
    Генерирует план выполнения задачи на основе инструментов Playwright.
    """

    tools_description = render_text_description(tools)

    response = await llm.ainvoke(
        [
            SystemMessage(content=task_decomposition_prompt.format(
                tools_description=tools_description
            )),
        ]
        + state["messages"]
    )

    return {
        "messages": [response],
        "current_plan": response.content
    }


async def get_list_of_tools_node(state: PlannerState, llm: BaseChatModel, tools):
    """Генерация списка tools, после докомпозиции задачи"""
    llm_structured: BaseChatModel = llm.with_structured_output(
        PlanSteps,
        method="json_schema"
    ).with_retry(
        stop_after_attempt=3
    )

    tools_description = render_text_description(tools)

    response = await llm_structured.ainvoke(
        (
            SystemMessage(content=get_list_of_tools_prompt.format(
                tools_description=tools_description
            )),
            HumanMessage(content=state.get("current_plan"))
        )
    )

    return {
        "messages": [AIMessage(content=str(response))],
        "plan_steps": response,
    }
