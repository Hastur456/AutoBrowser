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


# --------------------------------------------------------------------------- memory


from src.agent_loop.context import MEMORY_BLOCK_NAME, TRUNCATED_BLOCK_SUFFIX  # noqa: E402
from src.agent_loop.prompts import PLANNER_SYSTEM_PROMPT  # noqa: E402

REGRESSION_STATE = {
    "task": "find a kettle",
    "plan": [{"id": 1, "status": "in_progress", "description": "Search"}],
    "observation": "Results are visible.",
    "action_history": "1. browser_navigate {} -> success: ok",
    "working_notes": "",
}


def test_without_memory_the_turn_prompt_is_byte_for_byte_unchanged() -> None:
    """Regression snapshot: memory and working notes off must not change a single byte."""

    prompt = ContextAssembler().user_turn_prompt(REGRESSION_STATE, tools=[FakeTool("echo")])

    assert prompt == (
        "Task:\nfind a kettle\n\n"
        "Plan:\n1. [in_progress] Search\n\n"
        "Action History:\n1. browser_navigate {} -> success: ok\n\n"
        "Observation:\nResults are visible.\n\n"
        "Choose the next action."
    )


def test_without_memory_the_plan_prompt_is_unchanged() -> None:
    prompt = ContextAssembler().plan_prompt({"task": "find a kettle", "observation": ""})

    assert prompt == (
        f"{PLANNER_SYSTEM_PROMPT}\n\nTask:\nfind a kettle\n\n"
        "Observation context:\nNo observation yet.\n\nCreate or revise the plan."
    )


def test_memory_is_a_user_block_between_task_and_plan() -> None:
    context = ContextAssembler().assemble(REGRESSION_STATE, memory="Persistent memory: hint")

    block = next(block for block in context.blocks if block.name == MEMORY_BLOCK_NAME)
    assert (block.role, block.priority, block.source) == ("user", 15, "memory")
    assert [block.name for block in context.blocks][:3] == ["Task", "Memory", "Plan"]
    assert "Memory:\nPersistent memory: hint" in context.turn_prompt
    assert "Memory:" not in context.system_prompt


def test_empty_memory_adds_no_block() -> None:
    context = ContextAssembler().assemble(REGRESSION_STATE, memory="  ")

    assert MEMORY_BLOCK_NAME not in [block.name for block in context.blocks]


def test_a_block_is_cut_to_its_token_budget() -> None:
    block = ContextBlock(name="Memory", role="user", content="x" * 500, token_budget=100)

    rendered = ContextAssembler().render([block])

    assert rendered.startswith("Memory:\n")
    assert len(rendered) == len("Memory:\n") + 100
    assert rendered.endswith(TRUNCATED_BLOCK_SUFFIX)


def test_the_plan_prompt_gets_memory_as_its_own_section() -> None:
    prompt = ContextAssembler().plan_prompt({"task": "t"}, memory="Index: none")

    assert prompt.endswith("Create or revise the plan.\n\nMemory:\nIndex: none")


def test_working_notes_render_after_the_action_history() -> None:
    context = ContextAssembler().assemble({**REGRESSION_STATE, "working_notes": "Kettle A: 1990"})

    names = [block.name for block in context.blocks]
    assert names.index("Working Notes") == names.index("Action History") + 1
    assert "Working Notes:\nKettle A: 1990" in context.turn_prompt
