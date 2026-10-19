"""The UI-neutral session facade (:mod:`src.ui.controller`) over a real ``SessionRuntime``."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from src.agent_loop.execution.loop import AgentLoopResult
from src.config import PermissionsSettings, UiSettings
from src.contracts import PermissionVerdict, TaskRequest
from src.harness.approvals import ApprovalBroker
from src.harness.event_stream import EventBroadcaster
from src.harness.session import SessionRuntime
from src.messages import assistant_message, user_message
from src.ui.controller import SessionController, TaskBusyError
from src.ui.runtime_loop import RuntimeLoop
from tests.session_fakes import install_fake_agent
from tests.test_harness_session import (
    llm_factory,
    make_config,
    native_result,
    no_mcp_runtime,
    no_start,
    noop_wait,
)

VERDICT = PermissionVerdict(
    decision="ask",
    reason="Approval required by rule buy.",
    source="rule",
    rule_id="buy",
    grant_key=("playwright", "browser_click", "ozon.ru"),
)


def build(
    *, with_stream: bool = False, output: list[str] | None = None
) -> tuple[SessionController, SessionRuntime, ApprovalBroker, EventBroadcaster | None]:
    broker = ApprovalBroker(PermissionsSettings(), progress_timeout_seconds=10.0)
    broadcaster = EventBroadcaster(UiSettings()) if with_stream else None
    runtime = SessionRuntime(
        make_config(),
        llm_factory=llm_factory,
        start_chrome_cdp=no_start,
        wait_for_port=noop_wait,
        mcp_runtime_factory=no_mcp_runtime,
        human_input=broker,
        event_sinks=[broadcaster] if broadcaster is not None else [],
    )
    controller = SessionController(
        runtime,
        broker=broker,
        broadcaster=broadcaster,
        output_fn=output.append if output is not None else None,
    )
    return controller, runtime, broker, broadcaster


@pytest.fixture(autouse=True)
def _in_tmp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.mark.asyncio
async def test_start_reports_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_agent(monkeypatch, lambda _request: asyncio.sleep(0))
    controller, runtime, _, _ = build()

    view = await controller.start()

    assert view.session_id == runtime.context.session_id
    assert view.model == "test-model"
    assert view.browser == "disabled"
    assert view.servers == ()
    assert view.busy is False
    assert view.turn_cap == 10
    assert view.permission_mode == runtime.context.permissions.mode
    await runtime.close()


@pytest.mark.asyncio
async def test_submit_tracks_the_task_and_refuses_a_second_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    async def run(_request: TaskRequest) -> AgentLoopResult:
        await release.wait()
        return native_result(final_answer="done")

    install_fake_agent(monkeypatch, run)
    controller, runtime, _, _ = build()

    view = await controller.submit("find a phone")

    assert (view.index, view.task, view.status) == (1, "find a phone", "running")
    assert controller.busy is True
    assert controller.session_view().current_task_id == view.task_id
    with pytest.raises(TaskBusyError):
        await controller.submit("another")
    release.set()
    finished = await controller.wait()
    assert finished is not None
    assert (finished.status, finished.turns) == ("completed", 0)
    assert controller.busy is False
    await runtime.close()


@pytest.mark.asyncio
async def test_cancel_stops_the_running_task(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(_request: TaskRequest) -> AgentLoopResult:
        await asyncio.Event().wait()
        return native_result(final_answer="never")

    install_fake_agent(monkeypatch, run)
    controller, runtime, _, _ = build()

    assert await controller.cancel() is False
    await controller.submit("wait forever")
    assert await controller.cancel() is True

    (view,) = controller.task_views()
    assert view.status == "cancelled"
    assert view.finished_at is not None
    await runtime.close()


@pytest.mark.asyncio
async def test_run_returns_the_result_and_blocked_tasks_say_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(request: TaskRequest) -> AgentLoopResult:
        status = "blocked" if request.task == "pay" else "done"
        return native_result(final_answer=request.task, status=status)

    install_fake_agent(monkeypatch, run)
    controller, runtime, _, _ = build()

    result = await controller.run("pay")
    await controller.run("look")

    assert result.final_answer == "pay"
    assert [view.status for view in controller.task_views()] == ["blocked", "completed"]
    await runtime.close()


@pytest.mark.asyncio
async def test_approvals_are_bound_to_the_task_and_answered_through_the_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers: list[str] = []

    async def run(request: TaskRequest) -> AgentLoopResult:
        answers.append(await broker({"name": "browser_click", "args": {}}, VERDICT.reason, VERDICT))
        return native_result(final_answer="bought")

    install_fake_agent(monkeypatch, run)
    controller, runtime, broker, _ = build()
    seen: list[Any] = []
    controller.subscribe_approvals(seen.append)

    view = await controller.submit("buy")
    for _ in range(200):
        if controller.pending_approvals():
            break
        await asyncio.sleep(0.005)
    (ticket,) = controller.pending_approvals()
    assert ticket.task_id == view.task_id
    assert controller.answer(ticket.ticket_id, "session", via="tui") is True
    await controller.wait()

    assert answers == ["session"]
    assert seen[0] == ticket
    assert seen[-1].via == "tui"
    await runtime.close()


@pytest.mark.asyncio
async def test_the_stream_carries_the_task_and_reset_starts_a_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(_request: TaskRequest) -> AgentLoopResult:
        return native_result(final_answer="done")

    install_fake_agent(monkeypatch, run)
    controller, runtime, _, _ = build(with_stream=True)
    await controller.run("first")
    old_id = runtime.context.session_id

    stream = controller.subscribe()
    types = []
    for _ in range(3):
        item = await asyncio.wait_for(anext(stream), timeout=2)
        assert item.record is not None
        types.append(item.record["type"])
    assert types == ["session.started", "goal.started", "goal.completed"]

    view = await controller.reset()
    assert view.session_id != old_id
    assert controller.task_views() == []
    reset = await asyncio.wait_for(anext(stream), timeout=2)
    while reset.kind == "event":  # the old session's tail (session.closed, memory.*)
        reset = await asyncio.wait_for(anext(stream), timeout=2)
    assert reset.kind == "reset"
    assert reset.cursor.session_id == view.session_id
    await stream.aclose()
    await runtime.close()


@pytest.mark.asyncio
async def test_history_and_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(_request: TaskRequest) -> AgentLoopResult:
        return native_result(
            final_answer="done",
            messages=[user_message("hi"), assistant_message("hello")],
        )

    install_fake_agent(monkeypatch, run)
    controller, runtime, _, _ = build()
    await controller.run("chat")

    assert controller.history(1) == [{"role": "assistant", "content": "hello"}]
    assert len(controller.history(10)) == 2
    assert controller.clear_history() == 2
    assert controller.history(10) == []
    assert controller.memory_entries() == []
    await runtime.close()


@pytest.mark.asyncio
async def test_session_messages_go_to_the_output_function(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_agent(monkeypatch, lambda _request: asyncio.sleep(0))
    output: list[str] = []
    controller, runtime, _, _ = build(output=output)

    await controller.start()

    assert runtime._output == output.append  # the controller rerouted it
    await runtime.close()


def test_the_runtime_loop_runs_coroutines_on_its_own_thread() -> None:
    loop = RuntimeLoop()
    try:
        async def where() -> Any:
            return asyncio.get_running_loop()

        assert loop.run(where()) is loop.loop
        assert loop.submit(asyncio.sleep(0, result=7)).result(timeout=2) == 7
    finally:
        loop.stop()


@pytest.mark.asyncio
async def test_cancel_reaches_a_submit_that_is_still_starting_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_agent(monkeypatch, lambda _request: asyncio.sleep(0))
    controller, runtime, _, _ = build()
    started = asyncio.Event()

    async def slow_start() -> None:
        started.set()
        await asyncio.Event().wait()  # Chrome / MCP that never come up

    monkeypatch.setattr(runtime, "start", slow_start)
    submit = asyncio.create_task(controller.submit("task"))
    await started.wait()

    assert controller.busy is True
    assert await controller.cancel() is True
    with pytest.raises(asyncio.CancelledError):
        await submit
    assert controller.busy is False
    assert controller.task_views() == []


@pytest.mark.asyncio
async def test_shutdown_waits_for_the_task_within_its_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    stopped = asyncio.Event()

    async def run(_request: TaskRequest) -> AgentLoopResult:
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return native_result(final_answer="never")

    install_fake_agent(monkeypatch, run)
    controller, runtime, _, _ = build()
    await controller.submit("long")

    await controller.shutdown(5.0)

    assert stopped.is_set() and controller.busy is False
    await runtime.close()


@pytest.mark.asyncio
async def test_a_cancel_during_memory_consolidation_closes_the_task_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_agent(monkeypatch, lambda _request: asyncio.sleep(0, native_result(final_answer="ok")))
    controller, runtime, _, _ = build()
    consolidating = asyncio.Event()

    async def slow_memory(*_args: Any) -> None:
        consolidating.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "_update_memory", slow_memory)
    await controller.submit("task")
    await consolidating.wait()

    assert await controller.cancel() is True
    (view,) = controller.task_views()
    assert view.status == "completed" and view.finished_at is not None
    assert runtime.context.current_task is None
    await runtime.close()
