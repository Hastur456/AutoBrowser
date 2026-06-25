"""Minimal tests for the planner subgraph."""

import pytest
from unittest.mock import AsyncMock, MagicMock
from langchain.messages import HumanMessage, AIMessage

from src.subgraphs.planner.state import PlannerState, PlanSteps, PlanStep
from src.subgraphs.planner.workflow import PlannerWorkflow


class FakeChatModel:
    """Fake async LLM that returns canned responses for testing."""

    def __init__(self, response_content: str):
        self._response = AIMessage(content=response_content)

    async def ainvoke(self, messages):
        return self._response


class FakeStructuredChatModel(FakeChatModel):
    """Fake async LLM with structured output support."""

    def with_structured_output(self, schema, *, method: str = "json_schema", **kwargs):
        """Return a fake that wraps the response as the target schema."""
        fake = AsyncMock()
        fake.ainvoke = AsyncMock(return_value=self._response)
        return fake.with_retry(stop_after_attempt=3)


@pytest.mark.asyncio
async def test_planner_workflow_produces_plan_steps():
    """
    PlannerWorkflow.run() should accept a PlannerState and return plan_steps.
    """
    plan_text = (
        "**plan_needed**: true\n\n"
        "**steps**:\n"
        "1. browser_navigate to https://example.com\n"
        "2. browser_snapshot to capture the page"
    )
    structured_response = PlanSteps(
        root=[
            PlanStep(
                step_id=1,
                description="Navigate to example.com",
                action_type="navigate",
                estimated_tool="browser_navigate",
                is_sensitive=False,
            ),
            PlanStep(
                step_id=2,
                description="Capture page snapshot",
                action_type="snapshot",
                estimated_tool="browser_snapshot",
                is_sensitive=False,
            ),
        ]
    )

    fake_llm = FakeChatModel(response_content=plan_text)

    fake_structured = AsyncMock()
    fake_structured.ainvoke = AsyncMock(return_value=structured_response)

    class PatchedWorkflow(PlannerWorkflow):
        async def task_decomposition(self, state):
            return {
                "messages": [AIMessage(content=plan_text)],
                "current_plan": plan_text,
            }

        async def get_list_of_tools(self, state):
            return {
                "messages": [AIMessage(content=str(structured_response))],
                "plan_steps": structured_response,
            }

    workflow = PatchedWorkflow(llm=fake_llm, tools=[])

    state = PlannerState(
        messages=[HumanMessage(content="Navigate to example.com and take a snapshot")],
        current_plan="",
    )

    result = await workflow.run(state)

    assert "plan_steps" in result
    steps = result["plan_steps"]
    assert steps is not None
    assert isinstance(steps, PlanSteps)
    assert len(steps.root) == 2
    assert steps.root[0].estimated_tool == "browser_navigate"
    assert steps.root[1].estimated_tool == "browser_snapshot"


@pytest.mark.asyncio
async def test_planner_state_current_plan_required():
    """
    PlannerState should accept a current_plan field.
    """
    state = PlannerState(
        messages=[HumanMessage(content="hello")],
        current_plan="Step 1: navigate",
    )
    assert state["current_plan"] == "Step 1: navigate"
