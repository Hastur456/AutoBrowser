"""Planner state contracts."""

from __future__ import annotations

from typing import TypedDict

from src.agent.state import PlanStep


class PlannerState(TypedDict, total=False):
    """State accepted and returned by the planner subgraph."""

    task: str
    observation: str
    plan: list[PlanStep]
    current_step: int
