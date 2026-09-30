"""``HookEngine`` in isolation: loading, matching, aggregation and failure semantics.

Every engine here is built from an explicit ``HooksSettings(...)`` (or explicit
``RegisteredHook`` objects) — never from ``get_settings()``, which would pick up the
developer's own ``config.yaml``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest

from src.config import HooksSettings
from src.contracts import HookEvent, HookResult
from src.harness.hooks import (
    HookConfigError,
    HookDecisionRecord,
    HookEngine,
    HookOutcome,
    NullHookEngine,
    RegisteredHook,
    registry_digest,
)
from tests import hook_fixtures
from tests.hook_fixtures import CALLS

SCRIPTED = "tests.hook_fixtures:scripted"
PROGRESS_TIMEOUT = 120.0
TOOL_EVENTS = {"pre_tool_use", "permission_request", "post_tool_use", "post_tool_use_failure"}


@pytest.fixture(autouse=True)
def _reset_calls() -> Iterator[None]:
    CALLS.clear()
    yield
    CALLS.clear()


def _spec(hook_id: str, event: str = "pre_tool_use", **fields: Any) -> dict[str, Any]:
    options = fields.pop("options", None)
    spec: dict[str, Any] = {"id": hook_id, "event": event, "handler": SCRIPTED}
    spec["options"] = {"label": hook_id, **(options or {})}
    spec.update(fields)
    return spec


def _engine(*specs: dict[str, Any], **settings: Any) -> HookEngine:
    engine = HookEngine.from_settings(
        HooksSettings(enabled=True, registry=list(specs), **settings),
        progress_timeout_seconds=PROGRESS_TIMEOUT,
    )
    assert isinstance(engine, HookEngine)
    return engine


def _event(name: str = "pre_tool_use", **fields: Any) -> HookEvent:
    base: dict[str, Any] = {
        "session_id": "session-1",
        "goal_id": "task-1",
        "task_id": "task-1",
        "task": "find jackets",
    }
    if name in TOOL_EVENTS:
        base.update({"tool": "browser_navigate", "server": "playwright"})
    base.update(fields)
    return HookEvent(name=name, **base)  # type: ignore[arg-type]


def _labels() -> list[str]:
    return [label for label, _ in CALLS]


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def test_disabled_settings_give_a_null_engine() -> None:
    engine = HookEngine.from_settings(
        HooksSettings(enabled=False, registry=[_spec("a")]),
        progress_timeout_seconds=PROGRESS_TIMEOUT,
    )

    assert isinstance(engine, NullHookEngine)
    assert engine.has("pre_tool_use") is False


@pytest.mark.asyncio
async def test_null_engine_runs_nothing() -> None:
    engine = NullHookEngine()
    records: list[HookDecisionRecord] = []

    outcome = await engine.run(_event(), on_record=records.append)

    assert outcome == HookOutcome()
    assert records == []
    assert engine.skip("stop", "stop_budget_exhausted") == ()


def test_has_reports_registered_events_only() -> None:
    engine = _engine(_spec("a", "pre_tool_use"), _spec("b", "stop"))

    assert engine.has("pre_tool_use") and engine.has("stop")
    assert not engine.has("post_tool_use") and not engine.has("goal_start")


@pytest.mark.asyncio
async def test_a_factory_is_called_once_with_its_options() -> None:
    engine = _engine(
        {
            "id": "obj",
            "event": "stop",
            "handler": "tests.hook_fixtures:AsyncCallable",
            "options": {"reason": "from options"},
        }
    )

    outcome = await engine.run(_event("stop"))

    assert outcome.decision == "allow"
    assert outcome.reason == "from options"


@pytest.mark.asyncio
async def test_a_handler_without_options_is_used_as_is() -> None:
    engine = _engine({"id": "d", "event": "pre_tool_use", "handler": "tests.hook_fixtures:deny_all"})

    outcome = await engine.run(_event())

    assert outcome.decision == "deny"
    assert _labels() == ["deny_all"]


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ({"id": "x", "event": "stop", "handler": "tests.no_such_module:f"}, "cannot import"),
        ({"id": "x", "event": "stop", "handler": "tests.hook_fixtures:missing"}, "no attribute"),
        ({"id": "x", "event": "stop", "handler": "tests.hook_fixtures:sync_handler"}, "not an async"),
        ({"id": "x", "event": "stop", "handler": "tests.hook_fixtures:scripted"}, "factory needs"),
        ({"id": "x", "event": "stop", "handler": "tests.hook_fixtures:NOT_CALLABLE"}, "not an async"),
        (
            {"id": "x", "event": "stop", "handler": SCRIPTED, "options": {"bogus": 1}},
            "rejected its options",
        ),
    ],
)
def test_broken_handlers_fail_loading(spec: dict[str, Any], message: str) -> None:
    with pytest.raises(HookConfigError, match=message):
        _engine(spec)


def test_duplicate_ids_fail_loading() -> None:
    with pytest.raises(HookConfigError, match="Duplicate hook id"):
        _engine(_spec("a"), _spec("a", "stop"))


def test_a_hook_timeout_not_below_the_progress_timeout_fails_loading() -> None:
    with pytest.raises(HookConfigError, match="progress_timeout_seconds"):
        _engine(_spec("a", timeout_seconds=PROGRESS_TIMEOUT))


def test_a_default_timeout_not_below_the_progress_timeout_fails_loading() -> None:
    with pytest.raises(HookConfigError, match="default_timeout_seconds"):
        _engine(_spec("a"), default_timeout_seconds=PROGRESS_TIMEOUT + 1)


def test_a_sync_registered_hook_is_rejected() -> None:
    with pytest.raises(HookConfigError, match="async"):
        HookEngine([RegisteredHook(id="s", event="stop", handler=hook_fixtures.sync_handler)])


def test_the_registry_digest_is_stable_and_content_sensitive() -> None:
    first = HooksSettings(registry=[_spec("a")])
    same = HooksSettings(enabled=True, registry=[_spec("a")])
    other = HooksSettings(registry=[_spec("b")])

    assert registry_digest(first) == registry_digest(same)
    assert registry_digest(first) != registry_digest(other)
    assert re.fullmatch(r"[0-9a-f]{64}", registry_digest(HooksSettings()))


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("match", "tool", "server", "runs"),
    [
        ({}, "browser_navigate", "playwright", True),
        ({"tool": "browser_navigate"}, "browser_navigate", "playwright", True),
        ({"tool": "browser_navigate"}, "browser_navigate_back", "playwright", False),
        ({"tool": "browser_click|browser_type"}, "browser_type", "", True),
        ({"tool": "browser_click|browser_type"}, "browser_navigate", "", False),
        ({"tool": r"browser_\w+"}, "browser_tabs", "", True),
        ({"tool": r"browser_\w+"}, "fake__echo", "", False),
        ({"server": "playwright"}, "anything", "playwright", True),
        ({"server": "playwright"}, "anything", "other", False),
        ({"server": "fake", "tool": "fake__increment"}, "fake__increment", "fake", True),
        ({"server": "fake", "tool": "fake__increment"}, "fake__increment", "other", False),
    ],
)
async def test_matcher(match: dict[str, str], tool: str, server: str, runs: bool) -> None:
    engine = _engine(_spec("m", match=match))

    await engine.run(_event(tool=tool, server=server))

    assert (_labels() == ["m"]) is runs


@pytest.mark.asyncio
async def test_non_tool_events_ignore_the_match_filter() -> None:
    hook = RegisteredHook(
        id="s",
        event="stop",
        handler=hook_fixtures.scripted(label="s"),
        server="nope",
        tool=re.compile("nope"),
    )

    await HookEngine([hook]).run(_event("stop"))

    assert _labels() == ["s"]


@pytest.mark.asyncio
async def test_only_hooks_of_the_event_run() -> None:
    engine = _engine(_spec("pre", "pre_tool_use"), _spec("post", "post_tool_use"))

    await engine.run(_event("post_tool_use", result={"status": "success", "content": "x"}))

    assert _labels() == ["post"]


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handlers_run_in_registry_order() -> None:
    engine = _engine(_spec("c"), _spec("a"), _spec("b"))

    outcome = await engine.run(_event())

    assert _labels() == ["c", "a", "b"]
    assert outcome.decision is None
    assert [record.hook_id for record in outcome.records] == ["c", "a", "b"]


@pytest.mark.asyncio
async def test_the_first_deny_stops_the_chain() -> None:
    engine = _engine(
        _spec("a", options={"decision": "allow"}),
        _spec("d", options={"decision": "deny", "reason": "no"}),
        _spec("late", options={"decision": "allow"}),
    )

    outcome = await engine.run(_event())

    assert _labels() == ["a", "d"]
    assert outcome.decision == "deny"
    assert outcome.reason == "no"


@pytest.mark.asyncio
async def test_ask_keeps_running_and_a_later_deny_wins() -> None:
    engine = _engine(
        _spec("ask", options={"decision": "ask", "reason": "check"}),
        _spec("deny", options={"decision": "deny", "reason": "stop"}),
    )

    outcome = await engine.run(_event())

    assert _labels() == ["ask", "deny"]
    assert (outcome.decision, outcome.reason) == ("deny", "stop")


@pytest.mark.asyncio
async def test_ask_outranks_allow_and_keeps_its_reason() -> None:
    engine = _engine(
        _spec("allow", options={"decision": "allow", "reason": "fine"}),
        _spec("ask", options={"decision": "ask", "reason": "check"}),
        _spec("allow2", options={"decision": "allow", "reason": "also fine"}),
    )

    outcome = await engine.run(_event())

    assert _labels() == ["allow", "ask", "allow2"]
    assert (outcome.decision, outcome.reason) == ("ask", "check")


@pytest.mark.asyncio
async def test_updated_input_chains_through_the_handlers() -> None:
    engine = _engine(
        _spec("one", options={"updated_input": {"url": "https://a.example"}}),
        _spec("two", options={"updated_input": {"url": "https://b.example", "x": 1}}),
        _spec("three"),
    )

    outcome = await engine.run(_event(args={"url": "https://orig.example"}))

    seen = [event.args for _, event in CALLS]
    assert seen == [
        {"url": "https://orig.example"},
        {"url": "https://a.example"},
        {"url": "https://b.example", "x": 1},
    ]
    assert outcome.updated_input == {"url": "https://b.example", "x": 1}
    assert [record.modified for record in outcome.records] == [True, True, False]


@pytest.mark.asyncio
async def test_updated_output_chains_into_the_content_of_a_success() -> None:
    engine = _engine(
        _spec("one", "post_tool_use", options={"updated_output": "first"}),
        _spec("two", "post_tool_use", options={"updated_output": "second"}),
        _spec("three", "post_tool_use"),
    )

    outcome = await engine.run(
        _event("post_tool_use", result={"status": "success", "content": "raw", "error": ""})
    )

    assert [event.result["content"] for _, event in CALLS] == ["raw", "first", "second"]
    assert outcome.updated_output == "second"


@pytest.mark.asyncio
async def test_updated_output_chains_into_the_error_of_a_failure() -> None:
    engine = _engine(
        _spec("one", "post_tool_use_failure", options={"updated_output": "friendlier"}),
        _spec("two", "post_tool_use_failure"),
    )

    outcome = await engine.run(
        _event("post_tool_use_failure", result={"status": "error", "content": "", "error": "boom"})
    )

    assert CALLS[1][1].result["error"] == "friendlier"
    assert CALLS[1][1].result["content"] == ""
    assert outcome.updated_output == "friendlier"


@pytest.mark.asyncio
async def test_updates_are_ignored_on_events_that_do_not_take_them() -> None:
    engine = _engine(
        _spec("pre", "pre_tool_use", options={"updated_output": "x"}),
        _spec("post", "post_tool_use", options={"updated_input": {"a": 1}}),
    )

    pre = await engine.run(_event("pre_tool_use"))
    post = await engine.run(_event("post_tool_use", result={"status": "success"}))

    assert pre.updated_output is None and post.updated_input is None


@pytest.mark.asyncio
async def test_additional_context_is_concatenated_in_order() -> None:
    engine = _engine(
        _spec("one", options={"context": "first note"}),
        _spec("two"),
        _spec("three", options={"context": "second note"}),
    )

    outcome = await engine.run(_event())

    assert outcome.additional_context == "first note\n\nsecond note"


@pytest.mark.asyncio
async def test_on_record_is_called_once_per_handler_in_order() -> None:
    engine = _engine(
        _spec("a", options={"decision": "allow", "reason": "ok"}),
        _spec("skip", match={"tool": "other"}),
        _spec("b", options={"updated_input": {"k": "v"}}),
        _spec("d", options={"decision": "deny", "reason": "no"}),
        _spec("never"),
    )
    records: list[HookDecisionRecord] = []

    outcome = await engine.run(_event(), on_record=records.append)

    assert records == list(outcome.records)
    assert [(r.hook_id, r.decision, r.reason, r.modified) for r in records] == [
        ("a", "allow", "ok", False),
        ("b", None, "", True),
        ("d", "deny", "no", False),
    ]
    assert all(r.event == "pre_tool_use" and r.duration_ms >= 0 for r in records)


def test_skip_reports_every_hook_of_the_event() -> None:
    engine = _engine(_spec("s1", "stop"), _spec("s2", "stop"), _spec("p", "pre_tool_use"))

    records = engine.skip("stop", "stop_budget_exhausted")

    assert [(r.hook_id, r.skipped, r.decision) for r in records] == [
        ("s1", "stop_budget_exhausted", None),
        ("s2", "stop_budget_exhausted", None),
    ]


# --------------------------------------------------------------------------
# Failure semantics
# --------------------------------------------------------------------------

FAILURES = [
    pytest.param({"sleep": 1.0}, "timeout", id="timeout"),
    pytest.param({"error": "boom"}, "RuntimeError: boom", id="exception"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("failure", "error"), FAILURES)
@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("goal_start", "deny"),
        ("pre_tool_use", "deny"),
        ("permission_request", None),
        ("post_tool_use", None),
        ("post_tool_use_failure", None),
        ("stop", None),
        ("goal_end", None),
    ],
)
async def test_failure_follows_the_per_event_default(
    failure: dict[str, Any],
    error: str,
    event: str,
    expected: str | None,
) -> None:
    engine = _engine(_spec("f", event, options=failure, timeout_seconds=0.05))

    outcome = await engine.run(_event(event))

    assert outcome.decision == expected
    (record,) = outcome.records
    assert record.error == error
    assert record.decision == expected
    if expected == "deny":
        assert error in outcome.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(("failure", "error"), FAILURES)
async def test_explicit_fail_closed_overrides_the_default(
    failure: dict[str, Any],
    error: str,
) -> None:
    opened = _engine(
        _spec("f", "pre_tool_use", options=failure, timeout_seconds=0.05, fail_closed=False)
    )
    closed = _engine(_spec("f", "stop", options=failure, timeout_seconds=0.05, fail_closed=True))

    opened_outcome = await opened.run(_event("pre_tool_use"))
    closed_outcome = await closed.run(_event("stop"))

    assert opened_outcome.decision is None
    assert opened_outcome.records[0].error == error
    assert closed_outcome.decision == "deny"
    assert closed_outcome.records[0].error == error


@pytest.mark.asyncio
async def test_a_failed_handler_does_not_stop_the_chain_when_it_fails_open() -> None:
    engine = _engine(
        _spec("broken", "stop", options={"error": "boom"}),
        _spec("next", "stop", options={"decision": "deny", "reason": "ungrounded"}),
    )

    outcome = await engine.run(_event("stop"))

    assert _labels() == ["broken", "next"]
    assert (outcome.decision, outcome.reason) == ("deny", "ungrounded")


@pytest.mark.asyncio
async def test_a_handler_returning_a_wrong_type_counts_as_a_failure() -> None:
    async def bad(event: HookEvent) -> Any:
        return {"decision": "deny"}

    async def unknown(event: HookEvent) -> HookResult:
        return HookResult(decision="modify")  # type: ignore[arg-type]

    engine = HookEngine(
        [
            RegisteredHook(id="bad", event="stop", handler=bad),
            RegisteredHook(id="unknown", event="stop", handler=unknown),
        ]
    )

    outcome = await engine.run(_event("stop"))

    assert outcome.decision is None
    assert outcome.records[0].error.startswith("TypeError")
    assert outcome.records[1].error.startswith("ValueError")
