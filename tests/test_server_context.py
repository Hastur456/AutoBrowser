"""Server context: the MCP ``instructions`` and named prompts become system blocks.

Against the real stdio fake server (``tests/mcp_fixtures/fake_server.py``); no
pytest-asyncio: every test drives its own event loop.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.agent_loop.context import ContextAssembler
from src.harness.server_context import SERVER_CONTEXT_PRIORITY, server_sections
from src.mcp import MCPManager, parse_server_configs

FAKE_SERVER = str(Path(__file__).parent / "mcp_fixtures" / "fake_server.py")
INSTRUCTIONS = "Read with get.user before you increment."


def fake(**overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [FAKE_SERVER],
        "env": {"FAKE_SERVER_INSTRUCTIONS": INSTRUCTIONS},
        "init_timeout_s": 20,
    }
    config.update(overrides)
    return config


def collect(servers: dict[str, Any]) -> tuple[list[Any], list[tuple[str, dict[str, Any]]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    async def go() -> list[Any]:
        async with MCPManager(servers) as manager:
            return await server_sections(
                manager, on_event=lambda kind, payload: events.append((kind, payload))
            )

    return asyncio.run(asyncio.wait_for(go(), timeout=60)), events


def test_the_server_instructions_become_a_system_block() -> None:
    sections, events = collect({"fake": fake()})

    assert [(s.name, s.title, s.text) for s in sections] == [
        ("mcp:fake", "Server Instructions: fake", INSTRUCTIONS)
    ]
    assert sections[0].priority == SERVER_CONTEXT_PRIORITY
    assert events == []
    prompt = ContextAssembler(sections=sections).get_system_prompt()
    assert "Server Instructions: fake" in prompt and INSTRUCTIONS in prompt


def test_a_named_prompt_without_arguments_becomes_a_block_and_the_rest_are_skipped() -> None:
    sections, events = collect(
        {"fake": fake(use_instructions=False, context_prompts=["rules", "greet", "absent"])}
    )

    assert [(s.name, s.text) for s in sections] == [
        ("mcp:fake:rules", "Call echo before increment.")
    ]
    assert events == [
        (
            "mcp.context_skipped",
            {"server": "fake", "prompt": "greet", "reason": "the prompt has required arguments"},
        ),
        (
            "mcp.context_skipped",
            {"server": "fake", "prompt": "absent", "reason": "the server does not offer this prompt"},
        ),
    ]


def test_each_block_is_cut_to_the_entry_limit() -> None:
    sections, _ = collect({"fake": fake(context_prompts=["rules"], context_max_chars=4)})

    assert [s.text for s in sections] == [INSTRUCTIONS[:4], "Call"]


def test_a_server_without_instructions_adds_nothing() -> None:
    sections, events = collect({"fake": fake(env=None)})

    assert sections == [] and events == []


def test_the_context_fields_validate() -> None:
    (config,) = parse_server_configs({"fake": fake()}).values()
    assert config.use_instructions is True
    assert config.context_prompts == []
    with pytest.raises(ValidationError):
        parse_server_configs({"fake": fake(context_max_chars=0)})


def test_an_ephemeral_server_contributes_its_context_too() -> None:
    sections, events = collect(
        {"fake": fake(connection_mode="ephemeral", context_prompts=["rules"])}
    )

    assert [s.name for s in sections] == ["mcp:fake", "mcp:fake:rules"] and events == []


class _StubManager:
    """A manager whose one READY server offers prompts that fail in different ways."""

    def __init__(self, get_prompt: Any, *, state: Any = None) -> None:
        from src.mcp import ConnectionState, PromptDescriptor
        from src.mcp.config import parse_server_config

        self._get_prompt = get_prompt
        self._state = state or ConnectionState.READY
        self._config = parse_server_config({**fake(), "context_prompts": ["p"]})
        self._prompt = PromptDescriptor(server="s", local_name="p", description=None, qualified_name="s__p")

    def servers(self) -> list[str]:
        return ["s"]

    def server_config(self, _name: str) -> Any:
        return self._config

    def instructions(self, _name: str) -> str | None:
        return None

    def list_prompts(self) -> list[Any]:
        return [self._prompt]

    def status(self) -> dict[str, Any]:
        from types import SimpleNamespace

        return {"s": SimpleNamespace(state=self._state)}

    async def get_prompt(self, name: str, arguments: Any = None) -> Any:
        return await self._get_prompt(name)


def stub_reasons(manager: _StubManager) -> list[str]:
    events: list[tuple[str, dict[str, Any]]] = []
    sections = asyncio.run(
        server_sections(manager, on_event=lambda kind, payload: events.append((kind, payload)))  # type: ignore[arg-type]
    )
    assert sections == []
    assert {kind for kind, _ in events} == {"mcp.context_skipped"}
    return [payload["reason"] for _, payload in events]


def test_a_failing_prompt_is_skipped_without_failing_startup() -> None:
    from src.mcp import RequestTimeoutError

    async def times_out(_name: str) -> Any:
        raise RequestTimeoutError("s", "prompts/get")

    assert stub_reasons(_StubManager(times_out)) == ["prompts/get failed: RequestTimeoutError"]


def test_a_prompt_without_text_is_skipped() -> None:
    from types import SimpleNamespace

    async def empty(_name: str) -> Any:
        return SimpleNamespace(messages=[SimpleNamespace(content=SimpleNamespace(type="image"))])

    assert stub_reasons(_StubManager(empty)) == ["the prompt has no text"]


def test_a_server_that_is_not_ready_says_so() -> None:
    from src.mcp import ConnectionState

    async def never(_name: str) -> Any:
        raise AssertionError("a server that is not ready is not asked")

    assert stub_reasons(_StubManager(never, state=ConnectionState.FAILED)) == [
        "the server is not ready (failed)"
    ]
