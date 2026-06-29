import asyncio
from typing import Literal

from langchain.chat_models import BaseChatModel
from langchain.messages import SystemMessage, HumanMessage
from pydantic import BaseModel

from .state import AgentState
from .prompts import REFLECT_SYSTEM_PROMPT, VISION_SYSTEM_PROMPT


class ReflectDecision(BaseModel):
    action: Literal["continue", "replan", "retry", "done", "fatal", "human"]


async def observe_node(state: AgentState, tools: list) -> dict:
    tool_registry = {t.name: t for t in tools}
    snapshot_tool = tool_registry.get("browser_snapshot")
    if snapshot_tool is None:
        return {"observation": None}
    try:
        result = await snapshot_tool.ainvoke({})
        return {"observation": str(result)}
    except Exception as exc:
        return {"observation": f"snapshot_error: {exc}"}


async def vision_node(state: AgentState, llm: BaseChatModel) -> dict:
    observation = state.get("observation") or "(no snapshot)"
    messages = [
        SystemMessage(content=VISION_SYSTEM_PROMPT),
        HumanMessage(content=observation),
    ]
    response = await llm.ainvoke(messages)
    return {"perception": response.content}


async def human_input_node(state: AgentState) -> dict:
    perception = state.get("perception") or "(no perception)"
    raw = state.get("plan_steps")
    plan_steps = raw.steps if hasattr(raw, "steps") else (raw or [])
    current_step = plan_steps[0] if plan_steps else None

    step_desc = (
        current_step.description if hasattr(current_step, "description")
        else (current_step or {}).get("description", "(unknown step)")
    )
    prompt = (
        f"\n[Human approval required]\n"
        f"Current page: {perception}\n"
        f"Next step: {step_desc}\n"
        f"Your input: "
    )

    loop = asyncio.get_event_loop()
    user_input = await loop.run_in_executor(None, input, prompt)
    return {"messages": [HumanMessage(content=user_input)]}


async def reflect_node(state: AgentState, llm: BaseChatModel, max_retries: int = 3) -> dict:
    raw = state.get("plan_steps")
    plan_steps = raw.steps if hasattr(raw, "steps") else (raw or [])
    last_error_type = state.get("last_error_type") or "none"
    retry_attempts = state.get("retry_attempts", 0)
    replan_count = state.get("replan_count", 0)

    # Force human approval if next step is sensitive
    if plan_steps:
        current_step = plan_steps[0]
        is_sensitive = (
            current_step.is_sensitive if hasattr(current_step, "is_sensitive")
            else (current_step if isinstance(current_step, dict) else {}).get("is_sensitive", False)
        )
        if is_sensitive:
            return {"reflection": "human"}

    perception = state.get("perception") or "(no perception)"

    def _step_str(s, i):
        sid = s.step_id if hasattr(s, "step_id") else s.get("step_id", i + 1)
        atype = s.action_type if hasattr(s, "action_type") else s.get("action_type", "?")
        desc = s.description if hasattr(s, "description") else s.get("description", "")
        return f"  {sid}. [{atype}] {desc}"

    steps_text = "\n".join(_step_str(s, i) for i, s in enumerate(plan_steps)) or "  (no steps)"

    context = (
        f"Page perception:\n{perception}\n\n"
        f"Plan steps:\n{steps_text}\n\n"
        f"last_error_type: {last_error_type}\n"
        f"retry_attempts: {retry_attempts} / {max_retries}\n"
        f"replan_count: {replan_count}"
    )

    messages = [
        SystemMessage(content=REFLECT_SYSTEM_PROMPT),
        *state.get("messages", []),
        HumanMessage(content=context),
    ]
    
    # Временное изменение на обычный ainvoke для теста провайдера

    # decision: ReflectDecision = await llm.with_structured_output(ReflectDecision).ainvoke(messages)
    # return {"reflection": decision.action}

    decision = await llm.ainvoke(messages)
    return {"reflection": decision.content}
