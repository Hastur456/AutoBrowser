from __future__ import annotations

from src.agent.prompts import AGENT_SYSTEM_PROMPT
from src.agent.subgraphs.observer.prompts import OBSERVER_SYSTEM_PROMPT
from src.agent.subgraphs.planner.prompts import PLANNER_SYSTEM_PROMPT


def test_agent_prompt_requires_search_input_inspection_before_submit() -> None:
    prompt = AGENT_SYSTEM_PROMPT.lower()

    assert "prefer the fewest actions" in prompt
    assert "do not take a fresh" in prompt
    assert "after every successful action" in prompt
    assert "use browser_type directly" in prompt
    assert "move straight to results extraction" in prompt
    assert "do this search-affordance click at most once" in prompt
    assert "https://www.ozon.ru/search/?text=<url-encoded query>" in prompt
    assert "repeated clicks/double-clicks" in prompt


def test_planner_prompt_includes_search_contract_steps() -> None:
    prompt = PLANNER_SYSTEM_PROMPT.lower()

    assert "prefer 1-3 steps" in prompt
    assert "do not split a search task into separate locate, inspect, type, and submit" in prompt
    assert "direct search url navigation as an early" in prompt
    assert "never plan repeated clicks or double-clicks" in prompt
    assert "locate the search input" in prompt
    assert "verify and extract visible results" in prompt


def test_observer_prompt_reports_search_field_alignment() -> None:
    prompt = OBSERVER_SYSTEM_PROMPT.lower()

    assert "empty" in prompt
    assert "already aligned with the requested search" in prompt
    assert "unrelated query" in prompt
    assert "inspect and correct the search input" in prompt
    assert "avoid asking for another snapshot" in prompt
    assert "never hint toward a" in prompt
    assert "double-click" in prompt
