"""Command hooks: the stdin/exit-code/stdout protocol and real subprocess runs.

Engines are built from an explicit ``HooksSettings(...)``, never from ``get_settings()``.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from src.config import ROOT_PATH, HooksSettings
from src.contracts import HookEvent, HookResult
from src.harness.command_hooks import CommandHook, parse_command_output
from src.harness.hooks import HookEngine

FIXTURE = Path(__file__).with_name("command_hook_fixture.py")


def _command(mode: str) -> str:
    return f'"{sys.executable}" "{FIXTURE}" {mode}'


def _event(name: str = "pre_tool_use", **fields: Any) -> HookEvent:
    base: dict[str, Any] = {
        "session_id": "session-1",
        "goal_id": "task-1",
        "task_id": "task-1",
        "task": "найди куртку",
        "tool": "browser_navigate",
        "server": "playwright",
        "args": {"url": "https://evil.test"},
    }
    base.update(fields)
    return HookEvent(name=name, **base)  # type: ignore[arg-type]


def _engine(*specs: dict[str, Any]) -> HookEngine:
    engine = HookEngine.from_settings(
        HooksSettings(enabled=True, registry=list(specs)),
        progress_timeout_seconds=120.0,
    )
    assert isinstance(engine, HookEngine)
    return engine


def _spec(hook_id: str, mode: str, event: str = "pre_tool_use", **fields: Any) -> dict[str, Any]:
    return {"id": hook_id, "event": event, "type": "command", "command": _command(mode), **fields}


# --------------------------------------------------------------------------
# Output protocol
# --------------------------------------------------------------------------


def test_empty_stdout_is_no_opinion() -> None:
    assert parse_command_output("pre_tool_use", 0, "  \n", "") is None


@pytest.mark.parametrize("event", ["pre_tool_use", "permission_request", "stop", "goal_start"])
def test_exit_code_2_denies_with_stderr_as_reason(event: str) -> None:
    result = parse_command_output(event, 2, "ignored", "  not allowed\n")  # type: ignore[arg-type]

    assert result == HookResult(decision="deny", reason="not allowed")


def test_exit_code_2_without_stderr_still_has_a_reason() -> None:
    result = parse_command_output("pre_tool_use", 2, "", "")

    assert result is not None and result.decision == "deny" and result.reason


@pytest.mark.parametrize("event", ["post_tool_use", "post_tool_use_failure"])
def test_exit_code_2_after_the_tool_ran_feeds_stderr_to_the_model(event: str) -> None:
    result = parse_command_output(event, 2, "", "page text is untrusted")  # type: ignore[arg-type]

    assert result == HookResult(additional_context="page text is untrusted")


def test_other_exit_codes_are_failures_with_the_stderr_tail() -> None:
    with pytest.raises(RuntimeError, match="exit code 1: boom"):
        parse_command_output("pre_tool_use", 1, "", "boom\n")


def test_json_stdout_becomes_a_hook_result() -> None:
    stdout = (
        '{"decision": "ask", "reason": "confirm", "updated_input": {"url": "x"}, '
        '"updated_output": "y", "additional_context": "c", "user_message": "u"}'
    )

    assert parse_command_output("pre_tool_use", 0, stdout, "") == HookResult(
        decision="ask",
        reason="confirm",
        updated_input={"url": "x"},
        updated_output="y",
        additional_context="c",
        user_message="u",
    )


def test_block_is_an_alias_of_deny() -> None:
    result = parse_command_output("stop", 0, '{"decision": "block", "reason": "r"}', "")

    assert result == HookResult(decision="deny", reason="r")


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        ("{not json", "not valid JSON"),
        ('{"decision": "maybe"}', "unknown decision"),
        ('{"decision": ["deny"]}', "unknown decision"),
        ('{"permissionDecision": "deny"}', "unknown output keys"),
        ('{"reason": 1}', "must be a string"),
        ('{"updated_input": "x"}', "must be an object"),
        ('{"updated_output": {}}', "must be a string"),
    ],
)
def test_malformed_json_stdout_is_a_failure(stdout: str, message: str) -> None:
    with pytest.raises((ValueError, TypeError), match=message):
        parse_command_output("pre_tool_use", 0, stdout, "")


def test_plain_text_is_context_on_goal_start_only() -> None:
    assert parse_command_output("goal_start", 0, "note\n", "") == HookResult(
        additional_context="note"
    )
    assert parse_command_output("pre_tool_use", 0, "note\n", "") is None


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        {"id": "a", "event": "stop", "type": "command"},
        {"id": "a", "event": "stop", "type": "command", "command": "  "},
        {"id": "a", "event": "stop", "type": "command", "command": "x", "handler": "m:f"},
        {"id": "a", "event": "stop", "type": "command", "command": "x", "options": {"k": 1}},
        {"id": "a", "event": "stop", "command": "x", "handler": "m:f"},
        {"id": "a", "event": "stop"},
        {"id": "a", "event": "stop", "type": "http", "command": "x"},
    ],
)
def test_specs_must_fit_their_type(spec: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        HooksSettings(registry=[spec])


def test_a_command_spec_loads_a_command_hook() -> None:
    engine = _engine(_spec("cmd", "echo", match={"tool": "browser_navigate"}))

    (hook,) = engine._hooks["pre_tool_use"]
    assert isinstance(hook.handler, CommandHook)
    assert hook.handler.command == _command("echo")
    assert hook.tool is not None


# --------------------------------------------------------------------------
# Real subprocess runs
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_command_gets_the_event_on_stdin_in_the_repository_root() -> None:
    outcome = await _engine(_spec("echo", "echo")).run(_event())

    name, tool, args, env_event, cwd = outcome.additional_context.split("|")
    assert (name, tool, env_event) == ("pre_tool_use", "browser_navigate", "pre_tool_use")
    assert args == "{'url': 'https://evil.test'}"
    assert Path(cwd).resolve() == ROOT_PATH.resolve()


@pytest.mark.asyncio
async def test_exit_code_2_blocks_the_tool_call() -> None:
    outcome = await _engine(_spec("urls", "block")).run(_event())

    assert outcome.decision == "deny"
    assert outcome.reason == "no navigation to https://evil.test"
    assert outcome.records[0].error == ""


@pytest.mark.asyncio
async def test_json_output_rewrites_the_arguments_for_the_next_hook() -> None:
    outcome = await _engine(_spec("rewrite", "rewrite"), _spec("urls", "block")).run(_event())

    assert outcome.decision == "deny"
    assert outcome.reason == "no navigation to https://safe.test"
    assert outcome.updated_input == {"url": "https://safe.test"}


@pytest.mark.asyncio
async def test_plain_text_on_goal_start_becomes_context() -> None:
    outcome = await _engine(_spec("note", "text", event="goal_start")).run(_event("goal_start"))

    assert outcome.decision is None
    assert outcome.additional_context == "remember the budget"


@pytest.mark.asyncio
async def test_a_crashing_command_follows_the_failure_default() -> None:
    engine = _engine(_spec("pre", "crash"), _spec("post", "crash", event="post_tool_use"))

    pre = await engine.run(_event())
    post = await engine.run(_event("post_tool_use", result={"status": "success"}))

    assert pre.decision == "deny" and "exit code 1: boom" in pre.reason
    assert post.decision is None and post.records[0].error == "RuntimeError: exit code 1: boom"


@pytest.mark.asyncio
async def test_a_command_that_times_out_is_killed() -> None:
    engine = _engine(_spec("slow", "sleep", timeout_seconds=1.0, fail_closed=False))

    started = time.perf_counter()
    outcome = await engine.run(_event())

    assert time.perf_counter() - started < 10
    assert outcome.decision is None
    assert outcome.records[0].error == "timeout"
