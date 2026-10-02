"""Working notes: offered as an optional tool argument, stripped, kept task-local."""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.agent_loop.execution.notes import NOTES_ARGUMENT, offer_notes_argument, split_notes
from src.agent_loop.execution.state import LoopState
from src.config import MemorySettings, Settings
from src.contracts import Tool
from src.harness.tools import ToolRegistry
from tests.test_memory_tool import PLAN, DONE, run


def tool(name: str, schema: dict[str, Any] | None = None, calls: list | None = None) -> Tool:
    async def handler(**kwargs: Any) -> str:
        if calls is not None:
            calls.append(dict(kwargs))
        return "ok"

    return Tool(
        name=name,
        description=name,
        input_schema=schema or {"type": "object", "properties": {"text": {"type": "string"}}},
        func=handler,
    )


def test_every_tool_offers_the_notes_argument() -> None:
    defs, colliding = offer_notes_argument([tool("echo")])

    assert NOTES_ARGUMENT in defs[0].input_schema["properties"]
    assert "text" in defs[0].input_schema["properties"]
    assert colliding == frozenset()


def test_a_tool_with_its_own_notes_keeps_it() -> None:
    own = tool("note_taker", {"type": "object", "properties": {"notes": {"type": "integer"}}})

    defs, colliding = offer_notes_argument([own])

    assert defs[0].input_schema["properties"]["notes"] == {"type": "integer"}
    assert colliding == {"note_taker"}
    request, notes = split_notes({"name": "note_taker", "args": {"notes": 3}}, colliding)
    assert (request["args"], notes) == ({"notes": 3}, None)


def test_split_strips_and_truncates() -> None:
    request, notes = split_notes({"name": "echo", "args": {"text": "a", "notes": "x" * 50}}, max_chars=10)

    assert request["args"] == {"text": "a"}
    assert notes == "x" * 10 + " [truncated]"


def test_absent_notes_keep_the_previous_ones() -> None:
    assert split_notes({"name": "echo", "args": {"text": "a"}})[1] is None


def test_working_notes_are_task_local() -> None:
    state = LoopState().apply({"working_notes": "price 1990"})

    assert "working_notes" not in state.to_session_state()


def call(call_id: str, **args: Any) -> dict[str, Any]:
    return {"decision": "tool_call", "tool_request": {"name": "echo", "args": args, "reason": "r", "id": call_id}}


def use_notes(monkeypatch: pytest.MonkeyPatch, max_chars: int) -> None:
    settings = Settings(memory=MemorySettings(working_notes_max_chars=max_chars))
    monkeypatch.setattr("src.agent_loop.execution.loop.get_settings", lambda: settings)


@pytest.mark.asyncio
async def test_notes_reach_the_next_turn_and_never_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    use_notes(monkeypatch, 200)
    calls: list = []

    result, llm, _ = await run(
        [PLAN, call("c1", text="a", notes="Kettle A costs 1990."), call("c2", text="b"), DONE],
        registry=ToolRegistry([tool("echo", calls=calls)]),
    )

    assert calls == [{"text": "a"}, {"text": "b"}]
    assert "Working Notes:\nKettle A costs 1990." in llm.prompts[2]
    assert "Working Notes:\nKettle A costs 1990." in llm.prompts[3]  # kept when omitted
    assert result.state.working_notes == "Kettle A costs 1990."
    offered = llm.tools[1][0]
    assert NOTES_ARGUMENT in offered.input_schema["properties"]
    assert all(NOTES_ARGUMENT not in json.dumps(m.tool_calls[0].arguments) for m in result.state.messages if m.tool_calls)


@pytest.mark.asyncio
async def test_with_notes_off_nothing_is_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    use_notes(monkeypatch, 0)

    _, llm, _ = await run([PLAN, call("c1", text="a"), DONE], registry=ToolRegistry([tool("echo")]))

    offered = llm.tools[1][0]
    assert NOTES_ARGUMENT not in offered.input_schema.get("properties", {})
    assert all("Working Notes" not in prompt for prompt in llm.prompts)
