"""A stand-in for the session's ``Agent`` so session tests can script task results."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from src.agent_loop.execution.loop import AgentLoopResult
from src.agent_loop.execution.resources import EngineResources
from src.contracts import TaskRequest

TaskScript = Callable[[TaskRequest], Awaitable[AgentLoopResult]]


@dataclass
class BuiltAgent:
    """What ``SessionContext.build_agent`` assembled for one fake agent."""

    resources: EngineResources
    human_input: Any


def install_fake_agent(monkeypatch: pytest.MonkeyPatch, run: TaskScript) -> list[BuiltAgent]:
    """Replace the session's ``Agent`` with one that hands every request to ``run``.

    Returns the agents the session built, in order (one per started session).
    """

    built: list[BuiltAgent] = []

    class FakeAgent:
        def __init__(self, resources: EngineResources, *, human_input: Any = None) -> None:
            self.resources = resources
            built.append(BuiltAgent(resources, human_input))

        async def run(self, request: TaskRequest) -> AgentLoopResult:
            return await run(request)

    monkeypatch.setattr("src.harness.session.Agent", FakeAgent)
    return built
