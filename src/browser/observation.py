"""Deterministic Playwright MCP browser-schema adaptation (provider-neutral).

Canonical home for the stateless helpers that read Playwright MCP tool output: element
refs, tab activation, invalid/stale-ref detection, and deterministic rendering of a tool
result into observation text. These are pure browser-schema leaves — no agent state, no LLM,
no plan reasoning — so they belong in the browser layer per the project layering rules
("browser schema adaptation goes in ``src/browser/``").

Both agent-loop implementations consume these:

- the legacy LangGraph observer (``src/agent/subgraphs/observer/utils.py`` re-exports these
  names unchanged for backward compatibility); and
- the engine-native execution package (``src/agent_loop/execution/``), which imports directly
  from here.

Deliberately **not** here: plan/step-completion heuristics (hardcoded natural-language term
lists and keyword matching used to auto-advance the plan). Those are agent-loop reasoning, not
browser-schema adaptation, and must not live in the browser layer. They remain local to the
legacy observer (``observer/utils.py``); the engine-native path does not use them at all.

Contracts come from :mod:`src.contracts`; this module imports nothing from ``src/agent/`` or
``src/agent_loop/``. Keep it that way so the engine-native path carries no dependency on the
legacy graph.
"""

from __future__ import annotations

from typing import Any
import re

from src.contracts import CompactToolObservation, ToolResult


MAX_CONTENT_PREVIEW_CHARS = 1200
MAX_REFS_IN_OBSERVATION = 25
REF_PATTERN = re.compile(r"\bref=([A-Za-z][A-Za-z0-9_-]*)\b")
REF_VALUE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
TAB_INDEX_PATTERN = re.compile(r"\bTab\s+(?P<index>\d+)\b", re.IGNORECASE)
TAB_LIST_ITEM_PATTERN = re.compile(
    r"^\s*(?:[-*]\s*)?(?P<index>\d+)\s*:\s*(?P<body>.+?)\s*$",
    re.MULTILINE,
)
NEW_TAB_MARKER_PATTERN = re.compile(
    r"\b(?:new|opened|created)\s+(?:browser\s+)?tab\b|"
    r"\bopened\s+in\s+a\s+new\s+tab\b",
    re.IGNORECASE,
)
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


def fallback_compact_observation(result: ToolResult) -> CompactToolObservation:
    """Create a deterministic compact observation when no observer LLM is usable.

    Native copy of the observer LLM fallback (``observer/observer_llm.py``); both use the
    same 1200-char preview budget, so the produced observation is identical. Kept here so
    the engine-native path never imports the legacy observer subgraph.
    """

    tool_name = str(result.get("name", "tool") or "tool").strip()
    status = str(result.get("status", "error") or "error")
    content = str(result.get("content", "") or "")
    error = str(result.get("error", "") or "")
    payload = content if content else error
    errors = [error] if error else []
    return {
        "summary": compact_text(f"{tool_name} returned {status}: {payload}", 400),
        "visible_state": compact_text(payload or "No tool output."),
        "important_refs": [],
        "errors": errors,
        "next_observation_hint": "",
    }


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


def pending_tab_activation_from_result(result: ToolResult) -> tuple[int, str] | None:
    """Return the tab that should be activated after a tool opened a new tab."""

    tool_name = str(result.get("name", "") or "")
    if (
        result.get("status") != "success"
        or tool_name == "browser_tabs"
        or tool_name not in {*BROWSER_ACTION_TOOLS, "browser_navigate"}
    ):
        return None

    payload = str(result.get("content", "") or "")
    if not payload or "tab" not in payload.lower():
        return None

    has_new_tab_marker = bool(NEW_TAB_MARKER_PATTERN.search(payload))
    explicit_tab_indexes = [
        int(match.group("index")) for match in TAB_INDEX_PATTERN.finditer(payload)
    ]
    listed_tab_indexes = [
        int(match.group("index")) for match in TAB_LIST_ITEM_PATTERN.finditer(payload)
    ]
    if not has_new_tab_marker and len(set(listed_tab_indexes)) < 2:
        return None

    if explicit_tab_indexes:
        tab_index = explicit_tab_indexes[-1]
    elif listed_tab_indexes:
        tab_index = max(listed_tab_indexes)
    else:
        return None

    return (
        tab_index,
        (
            f"The last browser action opened or exposed Tab {tab_index}. "
            f"Switch to it with browser_tabs action=select index={tab_index} "
            "before taking browser_snapshot or using page refs. Do not repeat "
            "the click that opened the tab."
        ),
    )


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
