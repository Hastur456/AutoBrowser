"""SessionContext wiring of skills: capabilities, the catalog, skill_view, always-on bodies.

Settings are explicit ``Settings(skills=..., storage=...)`` patched into the session module,
never the developer's config.yaml or skills folder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.agent_loop.prompts import AGENT_SYSTEM_PROMPT
from src.config import Settings, SkillsSettings, StorageSettings
from src.harness.session import SessionContext
from src.harness.skills import VIEW_TOOL
from tests.test_harness_session import (
    FakeHarness,
    FakeMCPRuntime,
    llm_factory,
    make_config,
    no_start,
    noop_wait,
)
from tests.test_skills import write_skill


async def start_session(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    skills: SkillsSettings,
    *,
    browser: bool = True,
    no_mcp: bool = False,
) -> SessionContext:
    settings = Settings(skills=skills, storage=StorageSettings(root_dir=tmp_path / "root"))
    monkeypatch.setattr("src.harness.session.get_settings", lambda: settings)
    runtime = FakeMCPRuntime()
    runtime.browser_server = "playwright" if browser else None

    def mcp_runtime_factory(_port: int, _errlog: Any) -> FakeMCPRuntime:
        return runtime

    context = SessionContext(make_config(no_mcp=no_mcp))
    await context.initialize(
        llm_factory=llm_factory,
        start_chrome_cdp=no_start,
        wait_for_port=noop_wait,
        mcp_runtime_factory=mcp_runtime_factory,
        output_fn=lambda *_args, **_kwargs: None,
        print_tools=None,
        harness_factory=FakeHarness,
    )
    return context


def system_prompt(context: SessionContext) -> str:
    assert isinstance(context.harness, FakeHarness)
    return context.harness.kwargs["context_assembler"].get_system_prompt()


async def tool_names(context: SessionContext) -> list[str]:
    assert context.tool_registry is not None
    return sorted(await context.tool_registry.get_by_name())


@pytest.mark.asyncio
async def test_a_browser_session_sends_the_browser_rules_with_skills_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_skill(tmp_path / "root" / "skills", "mine")  # ignored: skills are off
    context = await start_session(monkeypatch, tmp_path, SkillsSettings())

    prompt = system_prompt(context)

    assert prompt.startswith(AGENT_SYSTEM_PROMPT + "\n\nBrowser Rules:\n# Browser Agent Rules")
    assert "Skills:" not in prompt
    assert "Tool Inventory" not in prompt
    assert VIEW_TOOL not in await tool_names(context)
    await context.close()


@pytest.mark.asyncio
async def test_a_session_without_a_browser_gets_no_browser_rules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = await start_session(monkeypatch, tmp_path, SkillsSettings(), browser=False)

    assert system_prompt(context) == AGENT_SYSTEM_PROMPT
    await context.close()


@pytest.mark.asyncio
async def test_a_no_mcp_session_gets_no_browser_rules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    context = await start_session(monkeypatch, tmp_path, SkillsSettings(), no_mcp=True)

    assert system_prompt(context) == AGENT_SYSTEM_PROMPT
    await context.close()


@pytest.mark.asyncio
async def test_enabled_skills_add_the_catalog_the_tool_and_user_skills(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_skill(tmp_path / "root" / "skills", "pdf-forms", description="Fill PDF forms.")
    context = await start_session(monkeypatch, tmp_path, SkillsSettings(enabled=True))

    prompt = system_prompt(context)

    assert "Skills:\n" in prompt
    assert "- browser-agent-rules: Snapshot-driven browser interaction" in prompt
    assert "- pdf-forms: Fill PDF forms." in prompt
    assert prompt.index("Skills:") < prompt.index("Browser Rules:")
    assert VIEW_TOOL in await tool_names(context)
    assert context.skill_store is not None
    assert context.skill_store.get("pdf-forms") is not None
    await context.close()


@pytest.mark.asyncio
async def test_without_visible_skills_there_is_no_catalog_and_no_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = await start_session(monkeypatch, tmp_path, SkillsSettings(enabled=True), no_mcp=True)

    assert system_prompt(context) == AGENT_SYSTEM_PROMPT
    assert VIEW_TOOL not in await tool_names(context)
    await context.close()


@pytest.mark.asyncio
async def test_a_user_skill_replaces_the_built_in_browser_rules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    write_skill(
        tmp_path / "root" / "skills",
        "browser-agent-rules",
        body="My own rules.\n",
        metadata='metadata:\n  always: "true"\n  title: Browser Rules',
    )
    context = await start_session(monkeypatch, tmp_path, SkillsSettings(enabled=True))

    prompt = system_prompt(context)

    assert prompt.endswith("Browser Rules:\nMy own rules.")
    assert "# Browser Agent Rules" not in prompt
    assert context.skill_store is not None and context.skill_store.shadowed() == ("browser-agent-rules",)
    await context.close()
