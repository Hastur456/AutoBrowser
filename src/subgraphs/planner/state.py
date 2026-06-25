"""Planning subgraph state module"""


from typing import (
    Annotated,
    TypedDict,
    List,
)
from pydantic import BaseModel, Field
from langchain.messages import (
    SystemMessage,
    HumanMessage,
    AIMessage,
    ToolMessage,
)
from langgraph.graph.message import add_messages


class PlanStep(BaseModel):
    """Plan step schema"""

    step_id: int = Field(description="Порядковый номер шага")
    description: str = Field(description="Человеческое описание шага (например, 'Найти товар')")
    action_type: str = Field(description="Тип действия: navigate, click, type, scroll, etc.")
    is_sensitive: bool = Field(
        default=False,
        description="Требует ли шаг вмешательства человека (оплата, ввод пароля)"
    )
    estimated_tool: str | None = Field(
        default=None,
        description="Предполагаемый инструмент (например, playwright_navigate)"
    )


class PlanSteps(BaseModel):
    """Plan steps schema"""

    steps: List[PlanStep] = Field(default_factory=list, description="Шаги плана для валидации ответа модели")


class PlannerState(TypedDict):
    """State of planner subgraph"""

    messages: Annotated[
        List[SystemMessage | HumanMessage | AIMessage | ToolMessage],
        add_messages
    ]
    current_plan: str
    plan_steps: PlanSteps | None
    current_step_index: int = Field(default=0)
