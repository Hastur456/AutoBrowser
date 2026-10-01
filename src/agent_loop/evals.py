"""Scenario eval harness for the engine-native agent loop."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import yaml

from src.llm import ModelResponse
from src.messages import Message

from src.agent_loop.engine import native_task_runner
from src.agent_loop.events import EventEmitter, InMemoryEventSink
from src.agent_loop.execution.resources import EngineResources
from src.harness.permissions import PermissionEngine
from src.agent_loop.replay import TraceSummary, print_action_sequence, summarize_trace
from src.browser.errors import BROWSER_ERROR_ACTION_FAILED, BROWSER_ERROR_INVALID_REF
from src.browser.names import is_browser_tool_name, to_playwright_browser_name
from src.browser.normalization import BrowserToolNormalizer
from src.contracts import Tool, ToolRequest, ToolResult
from src.harness.runtime import HARNESS_EVENT_METADATA_CONFIG_KEY, BrowserHarness
from src.harness.tools import ToolRegistry

_INVALID_REF_PATTERN = re.compile(
    r"\bRef\s+[A-Za-z][A-Za-z0-9_-]*\s+not\s+found\b",
    re.IGNORECASE,
)
_REF_PATTERN = re.compile(r"\bref=([A-Za-z][A-Za-z0-9_-]*)\b")
_REF_VALUE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class _FakeBrowserTools:
    """Replay browser tools from a deterministic sequence of snapshots.

    Doubles as a tool provider (``get_tools``) and a tool-call normalizer
    (``normalize_request``/``normalize_result``) for :class:`ToolRegistry`, so eval
    scenarios can drive the engine's browser tool-call path without Chrome/CDP.
    """

    def __init__(self, snapshots: list[str]) -> None:
        if not snapshots:
            raise ValueError("_FakeBrowserTools requires at least one snapshot.")

        self._snapshots = list(snapshots)
        self._snapshot_index = 0
        self._tools = self._build_tools()

    async def get_tools(self) -> Sequence[Any]:
        return list(self._tools)

    def normalize_request(
        self, request: ToolRequest, state: Any, tools: Any = None
    ) -> ToolRequest:
        _ = tools
        normalized_request = dict(request)
        args = dict(request.get("args") or {})
        requested_name = str(request.get("name", "") or "").strip()
        if not is_browser_tool_name(requested_name):
            normalized_request["args"] = args
            return normalized_request

        tool_name = to_playwright_browser_name(requested_name)
        normalized_request["name"] = tool_name

        if tool_name in {"browser_click", "browser_hover", "browser_type"}:
            ref = self._ref_from_args(args)
            if ref:
                args.setdefault("ref", ref)
                args.setdefault("target", ref)

        normalized_request["args"] = args
        return normalized_request

    def normalize_result(self, result: ToolResult) -> ToolResult:
        normalized_result = dict(result)
        tool_name = str(normalized_result.get("name", "") or "")
        if (
            normalized_result.get("status") != "error"
            or not is_browser_tool_name(tool_name)
            or normalized_result.get("error_code")
        ):
            return normalized_result

        if self._has_invalid_ref_error(normalized_result):
            normalized_result["error_code"] = BROWSER_ERROR_INVALID_REF
        else:
            normalized_result["error_code"] = BROWSER_ERROR_ACTION_FAILED
        return normalized_result

    @staticmethod
    def _has_invalid_ref_error(result: ToolResult) -> bool:
        payload = str(result.get("error", "") or result.get("content", "") or "")
        return bool(_INVALID_REF_PATTERN.search(payload))

    def _build_tools(self) -> list[Tool]:
        async def browser_navigate(url: str) -> str:
            """Navigate to a URL in the fake browser."""

            self._advance_snapshot()
            return f"Navigated to {url}."

        async def browser_snapshot(depth: int | None = None) -> str:
            """Return the current fake browser snapshot."""

            _ = depth
            return self._current_snapshot()

        async def browser_click(
            ref: str | None = None,
            target: str | None = None,
        ) -> str:
            """Click an element in the fake browser."""

            resolved_ref = self._require_ref(ref=ref, target=target)
            self._assert_ref_exists(resolved_ref)
            self._advance_snapshot()
            return f"Clicked ref {resolved_ref}."

        async def browser_type(
            text: str,
            ref: str | None = None,
            target: str | None = None,
        ) -> str:
            """Type text into an element in the fake browser."""

            resolved_ref = self._require_ref(ref=ref, target=target)
            self._assert_ref_exists(resolved_ref)
            self._advance_snapshot()
            return f"Typed into ref {resolved_ref}: {text}"

        async def browser_hover(
            ref: str | None = None,
            target: str | None = None,
        ) -> str:
            """Hover an element in the fake browser."""

            resolved_ref = self._require_ref(ref=ref, target=target)
            self._assert_ref_exists(resolved_ref)
            self._advance_snapshot()
            return f"Hovered ref {resolved_ref}."

        async def browser_evaluate(
            expression: str | None = None,
            script: str | None = None,
        ) -> dict[str, str]:
            """Evaluate a script in the fake browser without mutating page state."""

            payload = str(expression or script or "").strip()
            if not payload:
                raise ValueError(
                    "Fake browser evaluate requires an expression or script."
                )

            return {
                "source": "expression" if expression else "script",
                "expression": payload,
                "snapshot": self._current_snapshot(),
            }

        return [
            self._tool(browser_navigate, {"url": {"type": "string"}}, required=("url",)),
            self._tool(browser_snapshot, {"depth": {"type": "integer"}}),
            self._tool(
                browser_click,
                {
                    "ref": {"type": "string"},
                    "target": {"type": "string"},
                },
            ),
            self._tool(
                browser_type,
                {
                    "text": {"type": "string"},
                    "ref": {"type": "string"},
                    "target": {"type": "string"},
                },
                required=("text",),
            ),
            self._tool(
                browser_hover,
                {
                    "ref": {"type": "string"},
                    "target": {"type": "string"},
                },
            ),
            self._tool(
                browser_evaluate,
                {
                    "expression": {"type": "string"},
                    "script": {"type": "string"},
                },
            ),
        ]

    def _tool(
        self,
        func: Any,
        properties: dict[str, Any],
        *,
        required: Sequence[str] = (),
    ) -> Tool:
        """Wrap one async fake-browser function into a neutral ``Tool``."""

        return Tool(
            name=func.__name__,
            description=str(func.__doc__ or "").strip(),
            input_schema={
                "type": "object",
                "properties": dict(properties),
                **({"required": list(required)} if required else {}),
            },
            func=func,
        )

    def _current_snapshot(self) -> str:
        return self._snapshots[self._snapshot_index]

    def _advance_snapshot(self) -> None:
        if self._snapshot_index < len(self._snapshots) - 1:
            self._snapshot_index += 1

    def _assert_ref_exists(self, ref: str) -> None:
        if ref not in self._snapshot_refs(self._current_snapshot()):
            raise ValueError(f"Ref {ref} not found")

    def _require_ref(self, *, ref: str | None, target: str | None) -> str:
        resolved_ref = str(ref or "").strip()
        if resolved_ref:
            return resolved_ref

        resolved_target = str(target or "").strip()
        if self._looks_like_ref(resolved_target):
            return resolved_target

        raise ValueError("Fake browser action requires a ref or ref-like target.")

    def _ref_from_args(self, args: dict[str, Any]) -> str:
        ref = str(args.get("ref", "") or "").strip()
        if ref:
            return ref

        target = str(args.get("target", "") or "").strip()
        return target if self._looks_like_ref(target) else ""

    def _snapshot_refs(self, snapshot: str) -> set[str]:
        return {match.group(1) for match in _REF_PATTERN.finditer(snapshot)}

    def _looks_like_ref(self, value: str) -> bool:
        return bool(_REF_VALUE_PATTERN.fullmatch(value))


class FakeChatModel:
    """Deterministic provider-neutral model returning scenario responses in order.

    ``complete`` replays the next scripted response verbatim as ``content``
    (plan- and agent-turn JSON alike), so the eval scenarios drive the engine
    over the neutral ``ChatModel`` contract end to end.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._index = 0

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Sequence[Any] = (),
        **params: Any,
    ) -> ModelResponse:
        if not self._responses:
            raise IndexError("FakeChatModel has no scripted responses.")
        # Cycle through the scripted responses for deterministic replay.
        content = self._responses[self._index]
        self._index = (self._index + 1) % len(self._responses)
        return ModelResponse(content=content, finish_reason="stop")


@dataclass(frozen=True)
class EvalAssertions:
    """Assertions configured by a scenario fixture."""

    expected_terminal_status: str = "completed"
    final_answer_contains: list[str] = field(default_factory=list)
    max_tool_calls: int | None = None
    max_repeated_actions: int | None = None
    max_policy_blocks: int | None = None


@dataclass(frozen=True)
class EvalScenario:
    """One replayable browser-agent eval scenario."""

    name: str
    task: str
    model_responses: list[str]
    browser_snapshots: list[str]
    assertions: EvalAssertions
    turn_cap: int = 25


@dataclass(frozen=True)
class EvalResult:
    """Scenario result with metrics and compact trace output."""

    scenario_name: str
    summary: TraceSummary
    action_sequence: str
    final_state: dict[str, Any]

    def metrics(self) -> dict[str, Any]:
        return {
            "terminal_status": self.summary.terminal_status,
            "final_answer": self.summary.final_answer,
            "model_turn_count": self.summary.model_turn_count,
            "tool_call_count": self.summary.tool_call_count,
            "policy_block_count": self.summary.policy_block_count,
            "repeated_action_count": self.summary.repeated_action_count,
            "trace_complete": self.summary.trace_complete,
        }


def load_scenario(path: Path) -> EvalScenario:
    """Load an eval scenario from YAML."""

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    assertions = data.get("assertions") or {}
    return EvalScenario(
        name=str(data["name"]),
        task=str(data["task"]),
        model_responses=[
            _json_response(response)
            for response in (data.get("model", {}).get("responses") or [])
        ],
        browser_snapshots=[str(item) for item in data.get("browser", {}).get("snapshots") or []],
        assertions=EvalAssertions(
            expected_terminal_status=str(
                assertions.get("expected_terminal_status", "completed")
            ),
            final_answer_contains=[
                str(item) for item in assertions.get("final_answer_contains", [])
            ],
            max_tool_calls=assertions.get("max_tool_calls"),
            max_repeated_actions=assertions.get("max_repeated_actions"),
            max_policy_blocks=assertions.get("max_policy_blocks"),
        ),
        turn_cap=int(data.get("turn_cap", 25) or 25),
    )


async def run_scenario(scenario: EvalScenario) -> EvalResult:
    """Run one scenario against the engine-native execution loop."""

    sink = InMemoryEventSink()
    session_id = f"eval-{uuid4().hex}"
    task_id = f"task-{uuid4().hex}"
    emitter = EventEmitter(sink, session_id=session_id)
    provider = _FakeBrowserTools(scenario.browser_snapshots)
    llm = FakeChatModel(responses=scenario.model_responses)
    harness = BrowserHarness(
        llm=llm,
        # Same canonical-name normalizer the real session wires for the browser MCP
        # server (see src/harness/mcp_setup.py), so browser.click etc. resolve here too.
        tool_registry=ToolRegistry(
            providers=[provider], normalizers=[BrowserToolNormalizer(), provider]
        ),
        event_emitter=emitter,
    )
    # Evals are headless: approvals become non-terminal denies, never a terminal block.
    resources = EngineResources.from_harness(
        harness,
        llm=llm,
        events=emitter,
        permissions=PermissionEngine(mode="dont_ask"),
    )
    emitter.emit(
        "goal.started",
        source="agent_loop.evals",
        payload={"task": scenario.task},
        task_id=task_id,
        goal_id=task_id,
    )
    runner = native_task_runner(resources)
    task_config = {
        HARNESS_EVENT_METADATA_CONFIG_KEY: {
            "session_id": session_id,
            "task_id": task_id,
            "goal_id": task_id,
        },
    }
    session_config = SimpleNamespace(
        turn_cap=scenario.turn_cap,
        compress_tools=False,
    )
    try:
        result = await runner(harness, scenario.task, session_config, task_config)
    except Exception as exc:
        emitter.emit(
            "goal.failed",
            source="agent_loop.evals",
            payload={"task": scenario.task, "error": exc},
            task_id=task_id,
            goal_id=task_id,
        )
        final_state: dict[str, Any] = {}
    else:
        final_state = {
            **dict(result.session_state),
            "final_answer": str(result.final_answer or ""),
            "decision": str(result.status or ""),
        }
        emitter.emit(
            "goal.completed",
            source="agent_loop.evals",
            payload={"task": scenario.task, "result": final_state},
            task_id=task_id,
            goal_id=task_id,
        )
    events = sink.records
    summary = summarize_trace(events)
    return EvalResult(
        scenario_name=scenario.name,
        summary=summary,
        action_sequence=print_action_sequence(events),
        final_state=final_state,
    )


def assert_scenario_result(scenario: EvalScenario, result: EvalResult) -> None:
    """Assert configured scenario expectations with compact trace diagnostics."""

    metrics = result.metrics()
    action_sequence = result.action_sequence
    expected_status = scenario.assertions.expected_terminal_status
    assert result.summary.terminal_status == expected_status, action_sequence
    for expected_text in scenario.assertions.final_answer_contains:
        assert expected_text in result.summary.final_answer, action_sequence
    if scenario.assertions.max_tool_calls is not None:
        assert result.summary.tool_call_count <= scenario.assertions.max_tool_calls, (
            metrics,
            action_sequence,
        )
    if scenario.assertions.max_repeated_actions is not None:
        assert (
            result.summary.repeated_action_count
            <= scenario.assertions.max_repeated_actions
        ), (metrics, action_sequence)
    if scenario.assertions.max_policy_blocks is not None:
        assert result.summary.policy_block_count <= scenario.assertions.max_policy_blocks, (
            metrics,
            action_sequence,
        )
    assert result.summary.trace_complete is True, action_sequence


def _json_response(response: Any) -> str:
    if isinstance(response, str):
        return response
    return json.dumps(response, ensure_ascii=False)


__all__ = [
    "EvalAssertions",
    "EvalResult",
    "EvalScenario",
    "assert_scenario_result",
    "load_scenario",
    "run_scenario",
]
