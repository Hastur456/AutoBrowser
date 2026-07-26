from __future__ import annotations

from typing import Any
import re

from src.agent.state import CompactToolObservation, PlanStep, ToolResult


MAX_CONTENT_PREVIEW_CHARS = 1200
MAX_REFS_IN_OBSERVATION = 25
REF_PATTERN = re.compile(r"\bref=([A-Za-z][A-Za-z0-9_-]*)\b")
REF_VALUE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
WORD_PATTERN = re.compile(r"[\w]+", re.UNICODE)
INVALID_REF_PATTERN = re.compile(
    r"\bRef\s+[A-Za-z][A-Za-z0-9_-]*\s+not\s+found\b",
    re.IGNORECASE,
)
REF_INTERACTION_TOOLS = {
    "browser_click",
    "browser_type",
    "browser_hover",
    "browser_select",
    "browser_press",
    "browser_drag",
}
BROWSER_ACTION_TOOLS = REF_INTERACTION_TOOLS
STALE_OR_MISSING_ELEMENT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bnot\s+found\b",
        r"\bnot\s+visible\b",
        r"\bnot\s+attached\b",
        r"\bdetached\b",
        r"\bnot\s+editable\b",
        r"\bnot\s+enabled\b",
        r"\belement\s+is\s+not\b",
        r"\bunable\s+to\s+(?:click|type|fill|hover)\b",
        r"\bcannot\s+(?:click|type|fill|hover)\b",
    )
)
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


def raw_text(value: Any) -> str:
    """Return normalized text without truncating tool output."""

    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


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


def is_ref_value(value: Any) -> bool:
    """Return true when a value looks like a Playwright MCP element ref."""

    return bool(REF_VALUE_PATTERN.fullmatch(str(value or "").strip()))


def request_ref_value(request: dict[str, Any] | None) -> str:
    """Return the explicit ref targeted by a tool request when one exists."""

    args = dict((request or {}).get("args") or {})
    for key in ("ref", "target"):
        value = str(args.get(key) or "").strip()
        if is_ref_value(value):
            return value
    return ""


def snapshot_contains_ref(snapshot: str, ref: str) -> bool:
    """Return true when the current snapshot exposes the requested ref."""

    return bool(ref) and ref in extract_element_refs(snapshot)


def _clean_invalid_ref_text(value: Any, *, compress: bool) -> str:
    text = compact_text(value) if compress else raw_text(value)
    return INVALID_REF_PATTERN.sub("Ref not found", text)


def has_invalid_ref_text(value: Any) -> bool:
    """Return true when the payload contains a Playwright MCP invalid ref error."""

    return bool(INVALID_REF_PATTERN.search(str(value or "")))


def has_stale_or_missing_element_text(value: Any) -> bool:
    """Return true when a browser error suggests the snapshot is stale."""

    payload = str(value or "")
    return any(pattern.search(payload) for pattern in STALE_OR_MISSING_ELEMENT_PATTERNS)


def _has_invalid_ref_error(result: ToolResult) -> bool:
    payload = str(result.get("error", "") or result.get("content", "") or "")
    return has_invalid_ref_text(payload)


def _needs_fresh_snapshot_after_error(result: ToolResult) -> bool:
    tool_name = str(result.get("name", "") or "")
    payload = str(result.get("error", "") or result.get("content", "") or "")
    if has_invalid_ref_text(payload):
        return True
    return tool_name in REF_INTERACTION_TOOLS and has_stale_or_missing_element_text(
        payload
    )


def _observation_lines(
    result: ToolResult,
    compact: CompactToolObservation,
    refs: list[str],
    *,
    compress: bool,
) -> list[str]:
    tool_name = str(result.get("name", "tool") or "tool").strip()
    status = result.get("status", "error")
    content = str(result.get("content", "") or "")
    error = str(result.get("error", "") or "")
    payload = content if content else error

    if status == "error":
        lines = ["Tool failed."]
        cleaned_error = _clean_invalid_ref_text(error or payload, compress=compress)
        if cleaned_error:
            lines.append(cleaned_error)
        if INVALID_REF_PATTERN.search(error or payload):
            lines.append("A fresh browser_snapshot is required.")
        elif (
            tool_name in REF_INTERACTION_TOOLS
            and has_stale_or_missing_element_text(error or payload)
        ):
            lines.append(
                "The current element/page structure may be stale; take a fresh "
                "browser_snapshot before the next ref-based action."
            )
        return lines

    if not compress:
        lines = [f"{tool_name} returned success."]
        if payload:
            lines.append(raw_text(payload))
        if refs:
            shown_refs = "\n".join(refs[:MAX_REFS_IN_OBSERVATION])
            lines.extend(["Refs:", shown_refs])
        return [line for line in lines if line]

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

    if not _has_step_completion_evidence(current_plan_step, compact):
        return {}

    updated_plan, next_step = _advance_plan(plan, current_step)
    return {"plan": updated_plan, "current_step": next_step}
