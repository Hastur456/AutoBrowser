from __future__ import annotations

import pytest

from src.agent.prompts import AGENT_SYSTEM_PROMPT
from src.agent.prompts import AGENT_USER_PROMPT
from src.agent.prompts import BROWSER_CONTRACT_PROMPT
from src.agent.prompts import COMPLETION_PROMPT
from src.agent.prompts import CORE_RUNTIME_PROMPT
from src.agent.prompts import LOOP_GUARD_PROMPT
from src.agent.prompts import OUTPUT_FORMAT_PROMPT
from src.agent.prompts import render_compatibility_system_prompt
from src.agent.subgraphs.observer.prompts import OBSERVER_SYSTEM_PROMPT
from src.agent.subgraphs.planner.prompts import PLANNER_SYSTEM_PROMPT


PROMPT_CONSTRAINTS = {
    "core": (
        "use the bound tools when an external browser action is needed",
        "do not invent tool names",
        "return a final answer only when the task is complete",
        "prefer the fewest actions that can satisfy the task",
    ),
    "browser": (
        "treat browser.snapshot as the source of truth for page state",
        "snapshot refs are ephemeral",
        "call browser.snapshot next to obtain fresh refs",
        "do not invent css selectors, xpath, class names, or dom structure",
    ),
    "observation": (
        "follow observer correction hints",
        "if the observation or policy says the last browser action did not change",
        "latest browser.snapshot",
        "available refs",
    ),
    "completion": (
        "the task is not complete until you have extracted the list of results",
        "once you have data that satisfies the user request",
        "immediately set decision: 'done'",
    ),
    "output_format": (
        '{"decision":"replan","reason":"why the current plan is insufficient"}',
        '{"decision":"done","final_answer":"concise answer for the user"}',
        "do not describe a tool call in text",
    ),
}


@pytest.mark.parametrize(("category", "requirements"), PROMPT_CONSTRAINTS.items())
def test_agent_prompt_constraint_inventory(
    category: str,
    requirements: tuple[str, ...],
) -> None:
    prompt = " ".join(
        f"{AGENT_SYSTEM_PROMPT}\n{AGENT_USER_PROMPT}".lower().split()
    )
    missing = [requirement for requirement in requirements if requirement not in prompt]

    assert not missing, f"{category} constraints missing from agent prompt: {missing}"


def test_compatibility_renderer_preserves_legacy_system_prompt() -> None:
    assert render_compatibility_system_prompt() == AGENT_SYSTEM_PROMPT
    assert CORE_RUNTIME_PROMPT
    assert BROWSER_CONTRACT_PROMPT
    assert COMPLETION_PROMPT
    assert LOOP_GUARD_PROMPT
    assert OUTPUT_FORMAT_PROMPT


def test_agent_prompt_requires_search_input_inspection_before_submit() -> None:
    prompt = AGENT_SYSTEM_PROMPT.lower()

    assert "prefer the fewest actions" in prompt
    assert "do not take a fresh" in prompt
    assert "after every successful action" in prompt
    assert "follow the browser contract" in prompt
    assert "playwright mcp" not in prompt
    assert "use browser.type directly" in prompt
    assert "move straight to results extraction" in prompt
    assert "do this search-affordance click at most once" in prompt
    assert "https://www.ozon.ru/search/?text=<url-encoded query>" in prompt
    assert "repeated clicks/double-clicks" in prompt
    assert "typing a value into a filter field is not" in prompt
    assert "one price-filter ui attempt" in prompt


def test_planner_prompt_includes_search_contract_steps() -> None:
    prompt = PLANNER_SYSTEM_PROMPT.lower()

    assert "prefer 1-3 steps" in prompt
    assert "do not split a search task into separate locate, inspect, type, and submit" in prompt
    assert "direct search url navigation as an early" in prompt
    assert "never plan repeated clicks or double-clicks" in prompt
    assert "playwright mcp" not in prompt
    assert "browser.snapshot" in prompt
    assert "locate the search input" in prompt
    assert "verify and extract visible results" in prompt
    assert "filter contract" in prompt
    assert "typing into a filter field alone is not" in prompt


def test_observer_prompt_reports_search_field_alignment() -> None:
    prompt = OBSERVER_SYSTEM_PROMPT.lower()

    assert "playwright mcp" not in prompt
    assert "browser.snapshot is the source of truth" in prompt
    assert "browser.type fails" in prompt
    assert "empty" in prompt
    assert "already aligned with the requested search" in prompt
    assert "unrelated query" in prompt
    assert "inspect and correct the search input" in prompt
    assert "avoid asking for another snapshot" in prompt
    assert "never hint toward a" in prompt
    assert "double-click" in prompt


def test_agent_user_prompt_golden_browser_turn() -> None:
    rendered = AGENT_USER_PROMPT.format(
        task="find articles about browser automation",
        plan="1. [in_progress] Search for articles",
        current_step=0,
        observation="The latest snapshot shows a searchbox ref=e123.",
        consecutive_failures=1,
        repeat_count=0,
        snapshot='textbox "Search" [ref=e123]',
        refs="e123",
    )

    assert rendered == """Task:
find articles about browser automation

Plan:
1. [in_progress] Search for articles

Current step index:
0

Latest observation:
The latest snapshot shows a searchbox ref=e123.

Consecutive tool failures:
1

Repeated tool request count:
0

Latest browser.snapshot:
textbox "Search" [ref=e123]

Available refs:
e123

Snapshot reuse rule:
If the latest observation says browser.snapshot is already current or says to
reuse the existing snapshot/refs, do not call browser.snapshot again with any
depth. Continue from Latest browser.snapshot and Available refs. If the visible
snapshot is insufficient for the next step, prefer browser_find or
browser.evaluate; otherwise replan.

Choose the next action."""


def test_agent_user_prompt_golden_non_browser_turn_has_explicit_empty_browser_state() -> None:
    rendered = AGENT_USER_PROMPT.format(
        task="summarize the provided notes",
        plan="No plan yet.",
        current_step=0,
        observation="No observation yet.",
        consecutive_failures=0,
        repeat_count=0,
        snapshot="",
        refs="none",
    )

    assert "Task:\nsummarize the provided notes" in rendered
    assert "Latest observation:\nNo observation yet." in rendered
    assert "Latest browser.snapshot:\n\n\nAvailable refs:\nnone" in rendered
    assert rendered.endswith("Choose the next action.")
