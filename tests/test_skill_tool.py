"""skill_view: tool behavior, permissions, and on-demand loading inside the loop.

Stores are built from explicit ``SkillsSettings(...)`` over ``tmp_path``; engines from explicit
``PermissionsSettings``. Nothing reads ``get_settings()`` for skills or permissions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.agent_loop.context import ContextAssembler
from src.agent_loop.events import EventEmitter, InMemoryEventSink
from src.agent_loop.execution.loop import AgentLoopEngine
from src.agent_loop.execution.resources import EngineResources
from src.config import PermissionRule, PermissionsSettings, SkillsSettings
from src.contracts import PermissionCheck
from src.harness.permissions import PermissionEngine
from src.harness.runtime import BrowserHarness
from src.harness.skill_tool import skill_tools
from src.harness.skills import SKILLS_SERVER, VIEW_TOOL, SkillStore, always_sections, catalog_section
from src.harness.tools import ToolRegistry, to_tool_def, tool_is_read_only
from src.llm import ModelResponse
from tests.test_skills import write_skill

PLAN = {"steps": [{"id": 1, "description": "Do the task", "status": "pending"}]}
DONE = {"decision": "done", "final_answer": "All done."}


def make_store(root: Path, **fields: Any) -> SkillStore:
    write_skill(
        root,
        "pdf-forms",
        description="Fill PDF forms. Use when a task involves a PDF form.",
        body="# PDF forms\n\n1. Read reference.md.\n2. Fill the fields.\n",
        files={"reference.md": "Field names\nare case-sensitive\n"},
    )
    return SkillStore.load(SkillsSettings(enabled=True, **fields), roots=[("user", root)])


def view_tool(store: SkillStore) -> Any:
    (tool,) = skill_tools(store)
    return tool


# --------------------------------------------------------------------------- tool shape


def test_the_tool_has_the_mcp_tool_shape(tmp_path: Path) -> None:
    tool = view_tool(make_store(tmp_path))

    assert (tool.name, tool.server) == (VIEW_TOOL, SKILLS_SERVER)
    assert tool_is_read_only(tool) is True
    schema = to_tool_def(tool).input_schema
    assert schema["required"] == ["name"]
    assert set(schema["properties"]) == {"name", "path", "view_range"}


@pytest.mark.asyncio
async def test_view_returns_the_numbered_body(tmp_path: Path) -> None:
    tool = view_tool(make_store(tmp_path))

    text = await tool.invoke({"name": "pdf-forms"})

    assert text.splitlines()[0] == "pdf-forms — Fill PDF forms. Use when a task involves a PDF form."
    assert "1\t# PDF forms" in text
    assert "3\t1. Read reference.md." in text
    assert text == await tool.invoke({"name": "pdf-forms", "path": "SKILL.md"})


@pytest.mark.asyncio
async def test_view_returns_a_skill_file_and_a_line_range(tmp_path: Path) -> None:
    tool = view_tool(make_store(tmp_path))

    whole = await tool.invoke({"name": "pdf-forms", "path": "reference.md"})
    second = await tool.invoke({"name": "pdf-forms", "path": "reference.md", "view_range": [2, 2]})

    assert whole == "pdf-forms/reference.md\n1\tField names\n2\tare case-sensitive"
    assert second == "pdf-forms/reference.md\n2\tare case-sensitive"


@pytest.mark.asyncio
async def test_a_long_body_is_truncated(tmp_path: Path) -> None:
    tool = view_tool(make_store(tmp_path, body_max_chars=10))

    text = await tool.invoke({"name": "pdf-forms"})

    assert text.endswith("... [truncated]")
    assert "Fill the fields" not in text


@pytest.mark.asyncio
async def test_bad_names_and_paths_raise_for_the_model(tmp_path: Path) -> None:
    tool = view_tool(make_store(tmp_path))

    with pytest.raises(LookupError, match="Skills: pdf-forms"):
        await tool.invoke({"name": "nope"})
    with pytest.raises(ValueError, match="outside the skill"):
        await tool.invoke({"name": "pdf-forms", "path": "../x.md"})


# --------------------------------------------------------------------------- permissions


def check_for(tool: Any) -> PermissionCheck:
    return PermissionCheck(tool=tool.name, server=tool.server, args={}, read_only=tool_is_read_only(tool))


def test_read_only_mode_allows_skill_view(tmp_path: Path) -> None:
    engine = PermissionEngine.from_settings(PermissionsSettings(mode="read_only"))

    assert engine.evaluate(check_for(view_tool(make_store(tmp_path)))).decision == "allow"


def test_a_user_rule_can_address_the_skills_server(tmp_path: Path) -> None:
    rule = PermissionRule(id="no-skills", decision="deny", server="skills")
    engine = PermissionEngine.from_settings(PermissionsSettings(rules=[rule]))

    verdict = engine.evaluate(check_for(view_tool(make_store(tmp_path))))

    assert (verdict.decision, verdict.rule_id) == ("deny", "no-skills")


# --------------------------------------------------------------------------- inside the loop


class RecordingModel:
    """Scripted model that keeps the system and user prompt of every call."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = [json.dumps(response) for response in responses]
        self.calls: list[list[Any]] = []

    async def complete(self, messages: Any, **kwargs: Any) -> ModelResponse:
        self.calls.append(list(messages))
        content = self._responses[min(len(self.calls), len(self._responses)) - 1]
        return ModelResponse(content=content, finish_reason="stop")


@pytest.mark.asyncio
async def test_the_model_sees_the_catalog_and_loads_a_skill_on_demand(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    sink = InMemoryEventSink()
    emitter = EventEmitter(sink, session_id="session-1")
    llm = RecordingModel(
        [
            PLAN,
            {
                "decision": "tool_call",
                "tool_request": {"name": VIEW_TOOL, "args": {"name": "pdf-forms"}, "reason": "Fits.", "id": "c1"},
            },
            DONE,
        ]
    )
    harness = BrowserHarness(
        llm=llm,
        tool_registry=ToolRegistry(skill_tools(store)),
        context_assembler=ContextAssembler(sections=catalog_section(store) + always_sections(store)),
        event_emitter=emitter,
    )
    resources = EngineResources.from_harness(harness, llm=llm, events=emitter)

    result = await AgentLoopEngine(resources).run(
        "Fill the attached PDF form.",
        task_id="task-1",
        goal_id="task-1",
        session_id="session-1",
        turn_cap=10,
    )

    assert result.status == "done"
    system = "\n".join(str(message.content) for message in llm.calls[1] if message.role == "system")
    assert "Skills:\n" in system
    assert "- pdf-forms: Fill PDF forms." in system
    assert "Fill the fields" not in system  # the body is loaded, not listed
    tool_text = [str(message.content) for message in result.state.messages if message.role == "tool"]
    assert "2. Fill the fields." in tool_text[0]
