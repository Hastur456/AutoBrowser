"""Observer leaf helpers for the legacy LangGraph observer.

Split by responsibility:

- Browser-schema adaptation and deterministic tool-result rendering (element refs, tab
  activation, invalid/stale-ref detection, observation-line formatting) now live in the
  browser layer at :mod:`src.browser.observation` and are re-exported here unchanged so
  existing ``from src.agent.subgraphs.observer.utils import ...`` imports keep working.
- Plan/step-completion heuristics (hardcoded natural-language term lists + keyword matching
  used to auto-advance the plan) are agent-loop reasoning, not browser-schema adaptation, so
  they stay defined **here**, local to the legacy observer. The engine-native execution path
  deliberately does not use them.
"""

from __future__ import annotations

from typing import Any
import re

from src.agent.state import CompactToolObservation, PlanStep
from src.browser.observation import (
    BROWSER_ACTION_TOOLS,
    INVALID_REF_PATTERN,
    MAX_CONTENT_PREVIEW_CHARS,
    MAX_REFS_IN_OBSERVATION,
    NEW_TAB_MARKER_PATTERN,
    REF_INTERACTION_TOOLS,
    REF_PATTERN,
    REF_VALUE_PATTERN,
    STALE_OR_MISSING_ELEMENT_PATTERNS,
    TAB_INDEX_PATTERN,
    TAB_LIST_ITEM_PATTERN,
    _clean_invalid_ref_text,
    _has_invalid_ref_error,
    _needs_fresh_snapshot_after_error,
    _observation_lines,
    compact_text,
    extract_element_refs,
    has_invalid_ref_text,
    has_stale_or_missing_element_text,
    is_ref_value,
    pending_tab_activation_from_result,
    raw_text,
    request_ref_value,
    snapshot_contains_ref,
)

WORD_PATTERN = re.compile(r"[\w]+", re.UNICODE)
COMPLETION_EVIDENCE_TERMS = {
    "completed",
    "extracted",
    "appeared",
    "appears",
    "available",
    "displayed",
    "found",
    "loaded",
    "located",
    "opened",
    "present",
    "shown",
    "visible",
}
NEGATIVE_EVIDENCE_TERMS = {
    "failed",
    "missing",
    "not available",
    "not found",
    "not located",
    "not present",
    "not shown",
    "not visible",
    "unavailable",
}
STEP_STOPWORDS = {
    "a",
    "an",
    "and",
    "button",
    "click",
    "enter",
    "find",
    "for",
    "go",
    "input",
    "inspect",
    "locate",
    "navigate",
    "open",
    "page",
    "press",
    "search",
    "submit",
    "the",
    "to",
    "type",
}
SNAPSHOT_COMPLETION_STEP_TERMS = {
    "capture",
    "inspect",
    "observe",
    "read",
    "snapshot",
    "view",
}
ACTION_COMPLETION_TERMS = {
    "browser_click": {
        "click",
        "clicked",
        "open",
        "opened",
        "press",
        "pressed",
        "select",
        "selected",
        "клик",
        "кликни",
        "нажм",
        "нажать",
        "открой",
        "открыть",
        "перейди",
        "выбери",
        "выбрать",
    },
    "browser_type": {
        "type",
        "typed",
        "enter",
        "entered",
        "input",
        "введ",
        "ввести",
        "напиш",
        "напечат",
    },
    "browser_hover": {"hover", "hovered", "навед", "навести"},
    "browser_press": {"press", "pressed", "нажм", "нажать"},
    "browser_select": {"select", "selected", "выбери", "выбрать"},
    "browser_drag": {"drag", "dragged", "перетащ"},
}


def _advance_plan(plan: list[PlanStep], current_step: int) -> tuple[list[PlanStep], int]:
    if current_step < 0 or current_step >= len(plan):
        return plan, current_step

    updated_plan: list[PlanStep] = [dict(step) for step in plan]
    updated_plan[current_step]["status"] = "completed"
    next_step = current_step + 1
    if next_step < len(updated_plan):
        updated_plan[next_step]["status"] = "in_progress"
    return updated_plan, next_step


def _step_keywords(description: str) -> set[str]:
    words = set(WORD_PATTERN.findall(description.lower()))
    keywords = {word for word in words if len(word) > 2 and word not in STEP_STOPWORDS}
    return keywords or {word for word in words if len(word) > 2}


def _has_keyword_match(keywords: set[str], evidence_words: set[str]) -> bool:
    for keyword in keywords:
        if keyword in evidence_words:
            return True
        if len(keyword) < 5:
            continue
        keyword_prefix = keyword[:5]
        if any(len(word) >= 5 and word[:5] == keyword_prefix for word in evidence_words):
            return True
    return False


def _has_step_completion_evidence(
    step: PlanStep,
    compact: CompactToolObservation,
) -> bool:
    """Return true only when observation text supports the step goal."""

    evidence_text = " ".join(
        str(compact.get(key, "") or "") for key in ("summary", "visible_state")
    ).lower()
    if not evidence_text:
        return False

    if any(term in evidence_text for term in NEGATIVE_EVIDENCE_TERMS):
        return False

    if not any(term in evidence_text for term in COMPLETION_EVIDENCE_TERMS):
        return False

    keywords = _step_keywords(str(step.get("description", "") or ""))
    evidence_words = set(WORD_PATTERN.findall(evidence_text))
    return _has_keyword_match(keywords, evidence_words)


def _snapshot_completes_step(step: PlanStep) -> bool:
    description_words = set(
        WORD_PATTERN.findall(str(step.get("description", "") or "").lower())
    )
    return bool(description_words & SNAPSHOT_COMPLETION_STEP_TERMS)


def _action_completes_step(step: PlanStep, tool_name: str) -> bool:
    terms = ACTION_COMPLETION_TERMS.get(tool_name, set())
    if not terms:
        return False
    description = str(step.get("description", "") or "").lower()
    return any(term in description for term in terms)


def _plan_completion_update(
    plan: list[PlanStep],
    current_step: int,
    compact: CompactToolObservation,
    *,
    tool_name: str = "",
) -> dict[str, Any]:
    if current_step < 0 or current_step >= len(plan):
        return {}

    current_plan_step = plan[current_step]
    if tool_name == "browser_snapshot" and _snapshot_completes_step(current_plan_step):
        updated_plan, next_step = _advance_plan(plan, current_step)
        return {"plan": updated_plan, "current_step": next_step}

    if _action_completes_step(current_plan_step, tool_name):
        updated_plan, next_step = _advance_plan(plan, current_step)
        return {"plan": updated_plan, "current_step": next_step}

    if not _has_step_completion_evidence(current_plan_step, compact):
        return {}

    updated_plan, next_step = _advance_plan(plan, current_step)
    return {"plan": updated_plan, "current_step": next_step}


__all__ = [
    "ACTION_COMPLETION_TERMS",
    "BROWSER_ACTION_TOOLS",
    "COMPLETION_EVIDENCE_TERMS",
    "INVALID_REF_PATTERN",
    "MAX_CONTENT_PREVIEW_CHARS",
    "MAX_REFS_IN_OBSERVATION",
    "NEGATIVE_EVIDENCE_TERMS",
    "NEW_TAB_MARKER_PATTERN",
    "REF_INTERACTION_TOOLS",
    "REF_PATTERN",
    "REF_VALUE_PATTERN",
    "SNAPSHOT_COMPLETION_STEP_TERMS",
    "STALE_OR_MISSING_ELEMENT_PATTERNS",
    "STEP_STOPWORDS",
    "TAB_INDEX_PATTERN",
    "TAB_LIST_ITEM_PATTERN",
    "WORD_PATTERN",
    "_action_completes_step",
    "_advance_plan",
    "_clean_invalid_ref_text",
    "_has_invalid_ref_error",
    "_has_keyword_match",
    "_has_step_completion_evidence",
    "_needs_fresh_snapshot_after_error",
    "_observation_lines",
    "_plan_completion_update",
    "_snapshot_completes_step",
    "_step_keywords",
    "compact_text",
    "extract_element_refs",
    "has_invalid_ref_text",
    "has_stale_or_missing_element_text",
    "is_ref_value",
    "pending_tab_activation_from_result",
    "raw_text",
    "request_ref_value",
    "snapshot_contains_ref",
]
