"""Observation translation for executor results."""

from __future__ import annotations

import re
from typing import Any

from src.agent.observer_llm import fallback_compact_observation
from src.agent.state import AgentState, CompactToolObservation, PlanStep, ToolResult

MAX_CONTENT_PREVIEW_CHARS = 1200
MAX_REFS_IN_OBSERVATION = 25
REF_PATTERN = re.compile(r"\bref=([A-Za-z][A-Za-z0-9_-]*)\b")
INVALID_REF_PATTERN = re.compile(
    r"\bRef\s+[A-Za-z][A-Za-z0-9_-]*\s+not\s+found\b",
    re.IGNORECASE,
)


def compact_text(value: Any, limit: int = MAX_CONTENT_PREVIEW_CHARS) -> str:
    """Return a deterministic text preview within a character budget."""

    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""

    lines = [" ".join(line.split()) for line in text.split("\n")]
    text = "\n".join(line for line in lines if line)
    if len(text) <= limit:
        return text

    omitted = len(text) - limit
    suffix = f"... [truncated {omitted} chars]"
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def extract_element_refs(snapshot: str) -> list[str]:
    """Extract Playwright MCP element refs while preserving snapshot order."""

    refs: list[str] = []
    seen: set[str] = set()
    for match in REF_PATTERN.finditer(snapshot):
        ref = match.group(1)
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def _clean_invalid_ref_text(value: Any) -> str:
    return INVALID_REF_PATTERN.sub("Ref not found", compact_text(value))


def _observation_lines(
    result: ToolResult,
    compact: CompactToolObservation,
    refs: list[str],
) -> list[str]:
    tool_name = str(result.get("name", "tool") or "tool").strip()
    status = result.get("status", "error")
    content = str(result.get("content", "") or "")
    error = str(result.get("error", "") or "")
    payload = content if content else error

    if status == "error":
        lines = ["Tool failed."]
        cleaned_error = _clean_invalid_ref_text(error or payload)
        if cleaned_error:
            lines.append(cleaned_error)
        if INVALID_REF_PATTERN.search(error or payload):
            lines.append("A fresh browser_snapshot is required.")
        return lines

    summary = compact.get("summary") or f"{tool_name} completed."
    visible_state = compact.get("visible_state") or payload
    lines = [compact_text(summary, 400)]
    if visible_state:
        lines.append(compact_text(visible_state))
    if refs:
        shown_refs = "\n".join(refs[:MAX_REFS_IN_OBSERVATION])
        lines.extend(["Refs:", shown_refs])
    hint = compact.get("next_observation_hint", "")
    if hint:
        lines.append(compact_text(hint, 300))
    return [line for line in lines if line]


def _advance_plan(plan: list[PlanStep], current_step: int) -> tuple[list[PlanStep], int]:
    if current_step < 0 or current_step >= len(plan):
        return plan, current_step

    updated_plan: list[PlanStep] = [dict(step) for step in plan]
    updated_plan[current_step]["status"] = "done"
    next_step = current_step + 1
    if next_step < len(updated_plan):
        updated_plan[next_step]["status"] = "in_progress"
    return updated_plan, next_step


def compile_observation(
    state: AgentState,
    compact_observation: CompactToolObservation | None = None,
) -> dict[str, Any]:
    """Translate the latest ToolResult into a plain observation string."""

    result = state.get("tool_result") or {}
    tool_name = str(result.get("name", "") or "")
    status = result.get("status", "error")
    content = str(result.get("content", "") or "")
    compact = compact_observation or fallback_compact_observation(result)

    is_browser_tool = tool_name.startswith("browser_")
    is_snapshot = tool_name == "browser_snapshot" and status == "success"
    refs = extract_element_refs(content) if is_snapshot else []
    observation = "\n\n".join(_observation_lines(result, compact, refs))

    updates: dict[str, Any] = {
        "observation": observation,
        "decision": "tool_call",
        "policy_decision": "",
        "tool_request": {},
    }

    if is_snapshot:
        updates["snapshot"] = content
        updates["refs"] = refs
    elif is_browser_tool:
        updates["snapshot"] = ""
        updates["refs"] = []

    if status == "success":
        plan = state.get("plan") or []
        current_step = int(state.get("current_step", 0) or 0)
        updated_plan, next_step = _advance_plan(plan, current_step)
        updates["plan"] = updated_plan
        updates["current_step"] = next_step
        updates["error"] = ""
    else:
        updates["error"] = str(result.get("error", "") or "")

    return updates
