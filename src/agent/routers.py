"""Top-level graph routing functions."""

from __future__ import annotations

from typing import Literal

from langgraph.graph import END

from src.agent.state import AgentState


def route_agent_decision(state: AgentState) -> Literal["policy", "plan", "__end__"]:
    """Route after the reasoning node."""

    decision = state.get("decision")
    if decision == "tool_call":
        return "policy"
    if decision == "replan":
        return "plan"
    return END


def route_policy_decision(state: AgentState) -> Literal["executor", "human_input", "agent"]:
    """Route after policy classification."""

    decision = state.get("policy_decision")
    if decision == "approved":
        return "executor"
    if decision == "needs_human":
        return "human_input"
    return "agent"


def route_human_decision(state: AgentState) -> Literal["executor", "agent"]:
    """Route after a human approval interrupt resumes."""

    if state.get("policy_decision") == "approved":
        return "executor"
    return "agent"
