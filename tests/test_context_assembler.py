from __future__ import annotations

from dataclasses import dataclass

from src.agent_loop.context import AssembledContext, ContextAssembler, ContextBlock
from src.agent_loop.skills import browser_agent_rules_resource


@dataclass
class FakeTool:
    name: str
    description: str = ""


def test_context_block_model_assembles_state_and_tools() -> None:
    context = ContextAssembler().assemble(
        {
            "task": "find a product",
            "plan": [{"id": 1, "status": "in_progress", "description": "Search"}],
            "observation": "Searchbox is visible.",
            "snapshot": 'textbox "Search" [ref=e1]',
        },
        tools=[FakeTool("browser_snapshot", "Capture current page state")],
    )

    assert isinstance(context, AssembledContext)
    assert [block.name for block in context.blocks] == [
        "Task",
        "Plan",
        "Observation",
        "Tool Inventory",
        "Browser Rules",
    ]
    assert context.turn_prompt.startswith("Task:\nfind a product")
    assert "Tool Inventory:" not in context.turn_prompt
    assert "Tool Inventory:\n- browser_snapshot: Capture current page state" in (
        context.system_prompt
    )


def test_context_assembler_omits_empty_blocks() -> None:
    context = ContextAssembler().assemble(
        {"task": "summarize notes", "observation": "", "snapshot": ""},
        tools=[],
    )

    assert [block.name for block in context.blocks] == ["Task"]
    assert context.system_prompt == ""
    assert context.turn_prompt == "Task:\nsummarize notes"


def test_browser_rules_appear_for_browser_tools_and_state() -> None:
    context = ContextAssembler().assemble(
        {"task": "summarize this page", "snapshot": "button [ref=e1]"},
        tools=[FakeTool("browser_snapshot")],
    )

    assert "Browser Rules:" in context.system_prompt
    assert "source of truth" in context.system_prompt
    assert "browser_snapshot" in context.system_prompt
    assert "Browser Snapshot:" not in context.turn_prompt


def test_browser_rules_appear_for_explicit_browser_task_without_tools() -> None:
    context = ContextAssembler().assemble(
        {"task": "open the product page and inspect a page"},
        tools=[],
    )

    assert "Browser Rules:" in context.system_prompt


def test_browser_rules_are_omitted_for_plain_non_browser_task() -> None:
    context = ContextAssembler().assemble(
        {"task": "summarize the provided notes"},
        tools=[FakeTool("calculator")],
    )

    assert "Browser Rules:" not in context.system_prompt
    assert "Browser Snapshot:" not in context.turn_prompt


def test_browser_rule_resource_is_local_and_deterministic() -> None:
    resource = browser_agent_rules_resource()

    assert resource.name == "browser-agent-rules"
    assert resource.path.name == "browser-agent-rules.md"
    assert resource.load().startswith("# Browser Agent Rules")


def test_context_assembler_sorts_by_priority_then_name_and_source() -> None:
    blocks = [
        ContextBlock("Zeta", "user", "z", priority=10, source="b"),
        ContextBlock("Alpha", "user", "a", priority=10, source="b"),
        ContextBlock("Alpha", "user", "a2", priority=10, source="a"),
        ContextBlock("Later", "user", "later", priority=20),
    ]

    context = ContextAssembler().assemble({}, blocks=blocks)

    assert [(block.name, block.source) for block in context.blocks] == [
        ("Alpha", "a"),
        ("Alpha", "b"),
        ("Zeta", "b"),
        ("Later", "runtime"),
    ]
    assert context.turn_prompt == (
        "Alpha:\na2\n\nAlpha:\na\n\nZeta:\nz\n\nLater:\nlater"
    )


def test_context_assembler_render_can_filter_roles() -> None:
    blocks = [
        ContextBlock("System", "system", "runtime", priority=1),
        ContextBlock("Developer", "developer", "rules", priority=2),
        ContextBlock("User", "user", "task", priority=3),
    ]

    rendered = ContextAssembler().render(blocks, roles={"system", "developer"})

    assert rendered == "System:\nruntime\n\nDeveloper:\nrules"


def test_context_assembler_is_the_only_prompt_boundary() -> None:
    assembler = ContextAssembler()

    assert assembler.get_system_prompt().startswith(
        "You are the reasoning module for an AutoBrowser agent."
    )
    assert assembler.plan_prompt(
        {"task": "find a product", "observation": "Searchbox is visible."}
    ).startswith("You are the planning module for a browser automation agent.")


def test_user_turn_prompt_appends_action_instruction() -> None:
    prompt = ContextAssembler().user_turn_prompt(
        {
            "task": "find a product",
            "plan": [{"id": 1, "status": "in_progress", "description": "Search"}],
            "observation": "Searchbox is visible.",
        }
    )

    assert prompt.startswith("Task:\nfind a product")
    assert "Observation:\nSearchbox is visible." in prompt
    assert prompt.endswith("\n\nChoose the next action.")


def test_user_turn_prompt_falls_back_to_action_instruction() -> None:
    assert ContextAssembler().user_turn_prompt({}) == "Choose the next action."
