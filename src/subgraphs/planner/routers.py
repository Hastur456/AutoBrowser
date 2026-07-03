"""Planner routers.

The initial planner is one-shot, so this module intentionally stays small.
Keeping it separate preserves the package shape when retries or validation are
added later.
"""

from __future__ import annotations

from typing import Literal

from src.subgraphs.planner.state import PlannerState


def route_planner_done(state: PlannerState) -> Literal["done"]:
    """Return the single terminal route for the planner subgraph."""

    return "done"
