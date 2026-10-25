"""The UI-neutral session facade (:mod:`src.ui.controller`) over a real ``SessionRuntime``."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from src.agent_loop.execution.loop import AgentLoopResult
from src.config import PermissionsSettings, UiSettings, get_settings
from src.contracts import PermissionVerdict, TaskRequest
from src.harness.approvals import ApprovalBroker
from src.harness.event_stream import EventBroadcaster
from src.harness.session import SessionRuntime
from src.messages import assistant_message, user_message
from src.harness.session_store import SessionInUseError, SessionLock, save_state, session_in_use
from src.config import SkillsSettings
from src.harness.skills import SkillStore
from src.ui.controller import SessionController, TaskBusyError
from src.ui.runtime_loop import RuntimeLoop
from src.ui.views import SkillsView
from tests.test_skills import write_skill
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


@pytest.fixture
def letter_session_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """New session ids start with a letter. Ids are hex, so a random one starting with a
    digit made a test's task number ("5", "7") read as that session's id prefix."""

    import uuid

    real_uuid4 = uuid.uuid4

    def lettered() -> uuid.UUID:
        return uuid.UUID(hex="f" + real_uuid4().hex[1:])

    monkeypatch.setattr("src.harness.session.uuid4", lettered)


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


@pytest.mark.asyncio
async def test_cancelling_a_submit_caller_stops_the_start_but_not_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_agent(monkeypatch, lambda _request: asyncio.sleep(0))
    controller, runtime, _, _ = build()
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def slow_start() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(runtime, "start", slow_start)
    caller = asyncio.create_task(controller.submit("task"))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    assert stopped.is_set() and controller.busy is False


@pytest.mark.asyncio
async def test_shutdown_gives_up_on_a_task_that_ignores_cancellation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    release = asyncio.Event()

    async def stubborn(_request: TaskRequest) -> AgentLoopResult:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return native_result(final_answer="late")

    install_fake_agent(monkeypatch, stubborn)
    controller, runtime, _, _ = build()
    await controller.submit("stubborn")
    loop = asyncio.get_running_loop()

    began = loop.time()
    await controller.shutdown(0.2)

    assert loop.time() - began < 1.0
    assert "did not stop within 0.2s" in caplog.text
    release.set()
    await controller.wait()
    await runtime.close()


@pytest.mark.asyncio
async def test_current_url_and_replay_read_the_session(
    monkeypatch: pytest.MonkeyPatch, letter_session_ids: None
) -> None:
    async def run(request: TaskRequest) -> AgentLoopResult:
        return native_result(
            final_answer="ok",
            observation="- Page URL: https://shop.test/cart\n- Page Title: Cart",
        )

    install_fake_agent(monkeypatch, run)
    controller, runtime, _, _ = build()

    assert controller.current_url() == ""
    with pytest.raises(LookupError, match="no tasks"):
        await controller.replay()
    await controller.run("первая")
    await controller.run("вторая")

    assert controller.current_url() == "https://shop.test/cart"
    last = await controller.replay()
    first = await controller.replay("1")
    assert last[0] == "#2 · completed" and "goal.started: вторая" in last[1]
    assert first[0] == "#1 · completed" and "goal.started: первая" in first[1]
    with pytest.raises(LookupError, match="from 1 to 2"):
        await controller.replay("5")
    other = await controller.replay(runtime.context.session_id[:10])
    assert other[0].startswith(runtime.context.session_id[:12])
    await runtime.close()



@pytest.mark.asyncio
async def test_resume_switches_to_a_stored_session_and_replays_its_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def run(request: TaskRequest) -> AgentLoopResult:
        return native_result(final_answer=f"ответ на {request.task}")

    install_fake_agent(monkeypatch, run)
    # An earlier session with two tasks, closed.
    first, first_runtime, _, _ = build()
    await first.run("первая")
    await first.run("вторая")
    stored_id = first_runtime.context.session_id
    await first_runtime.close()

    controller, runtime, _, _ = build(with_stream=True)
    await controller.run("текущая")
    current_dir = runtime.context.session_dir
    assert [s.session_id for s in await controller.stored_sessions()] == [stored_id]  # not the current one

    view = await controller.resume("1")

    assert view.session_id == stored_id and view.resumed and view.task_count == 2
    assert [(t.task, t.final_answer) for t in controller.past_tasks()] == [
        ("первая", "ответ на первая"),
        ("вторая", "ответ на вторая"),
    ]
    assert current_dir is not None and session_in_use(current_dir) is False  # the old one closed
    with pytest.raises(LookupError, match="current one"):
        await controller.resume(stored_id[:8])
    await controller.run("продолжение")
    assert [t.task for t in controller.task_views()] == ["первая", "вторая", "продолжение"]
    await runtime.close()


@pytest.mark.asyncio
async def test_resume_refuses_while_busy_and_keeps_the_session_on_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    async def wait_forever(_request: TaskRequest) -> AgentLoopResult:
        await asyncio.Event().wait()
        return native_result(final_answer="never")

    install_fake_agent(monkeypatch, lambda _request: asyncio.sleep(0, native_result(final_answer="ok")))
    other, other_runtime, _, _ = build()
    await other.run("в другой")
    other_id = other_runtime.context.session_id
    other_dir = other_runtime.context.session_dir
    await other_runtime.close()
    lock = SessionLock.acquire(other_dir)  # type: ignore[arg-type]  # "another process"

    install_fake_agent(monkeypatch, wait_forever)
    controller, runtime, _, _ = build()
    await controller.submit("долгая")
    with pytest.raises(TaskBusyError):
        await controller.resume(other_id)
    await controller.cancel()

    before = runtime.context.session_id
    with pytest.raises(SessionInUseError):
        await controller.resume(other_id)  # open elsewhere
    assert runtime.context.session_id == before
    with pytest.raises(LookupError):
        await controller.resume("zzzz")
    lock.release()
    await runtime.close()



@pytest.mark.asyncio
async def test_a_replayed_conversation_is_sanitized_and_an_id_prefix_wins_over_a_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    esc = chr(0x1B)
    hostile = f"ok{esc}]0;pwned{chr(7)}{esc}[2Jdone"

    async def run(_request: TaskRequest) -> AgentLoopResult:
        return native_result(final_answer=hostile)

    install_fake_agent(monkeypatch, run)
    first, first_runtime, _, _ = build()
    await first.run(f"задача{esc}[31m")
    stored_id = first_runtime.context.session_id
    await first_runtime.close()

    controller, runtime, _, _ = build()
    await controller.resume(stored_id[:10])
    (past,) = controller.past_tasks()

    assert past.task == "задача" and past.final_answer == "okdone"
    assert esc not in past.final_answer
    await runtime.close()


@pytest.mark.asyncio
async def test_nothing_starts_while_a_session_switch_is_in_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_agent(monkeypatch, lambda _request: asyncio.sleep(0, native_result(final_answer="ok")))
    first, first_runtime, _, _ = build()
    await first.run("в первой")
    stored_id = first_runtime.context.session_id
    await first_runtime.close()

    controller, runtime, _, _ = build()
    await controller.run("текущая")
    closing = asyncio.Event()
    release = asyncio.Event()
    original_close = runtime.close

    async def slow_close() -> None:
        closing.set()
        await release.wait()
        await original_close()

    monkeypatch.setattr(runtime, "close", slow_close)
    switch = asyncio.create_task(controller.resume(stored_id))
    await closing.wait()

    assert controller.busy is True
    with pytest.raises(TaskBusyError):
        await controller.submit("в промежутке")
    with pytest.raises(TaskBusyError):
        await controller.resume(stored_id)
    release.set()
    await switch
    assert controller.busy is False
    await runtime.close()


@pytest.mark.asyncio
async def test_replay_reads_an_all_digit_id_prefix_as_a_session(
    monkeypatch: pytest.MonkeyPatch, letter_session_ids: None
) -> None:
    install_fake_agent(monkeypatch, lambda _request: asyncio.sleep(0, native_result(final_answer="ok")))
    controller, runtime, _, _ = build()
    await controller.run("одна")
    stored = get_settings().storage.sessions_dir / "1234567890abcdef"
    stored.mkdir(parents=True)
    (stored / "session.json").write_text('{"session_id": "1234567890abcdef", "metadata": {}}', encoding="utf-8")
    (stored / "events.jsonl").write_text('{"type": "goal.started", "source": "t", "payload": {"task": "x"}}\n', encoding="utf-8")

    lines = await controller.replay("1234567890")

    assert lines[0].startswith("1234567890ab")
    with pytest.raises(LookupError, match="from 1 to 1"):
        await controller.replay("7")
    await runtime.close()



@pytest.mark.asyncio
async def test_a_list_number_beats_a_digit_id_prefix_and_an_exact_id_beats_a_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_agent(monkeypatch, lambda _request: asyncio.sleep(0, native_result(final_answer="ok")))
    sessions_dir = get_settings().storage.sessions_dir
    for session_id, when in (("2aaaaaaaaaaaaaaa", "2026-10-20T10:00:00+00:00"), ("1bbbbbbbbbbbbbbb", "2026-10-19T10:00:00+00:00")):
        folder = sessions_dir / session_id
        folder.mkdir(parents=True)
        (folder / "session.json").write_text(
            json.dumps({"session_id": session_id, "metadata": {"last_activity": when}}), encoding="utf-8"
        )
        (folder / "tasks.json").write_text(json.dumps([{"task": "t", "status": "completed"}]), encoding="utf-8")
        save_state(folder, session_id, {"messages": []})

    controller, runtime, _, _ = build()
    # The list is newest first: 1 -> "2aaa...", 2 -> "1bbb..."; "1" is also the only prefix of "1bbb...".
    view = await controller.resume("1")
    assert view.session_id == "2aaaaaaaaaaaaaaa"
    view = await controller.resume("1bbbbbbbbbbbbbbb")
    assert view.session_id == "1bbbbbbbbbbbbbbb"
    await runtime.close()


# -- /model: the model catalog and switching the model ----------------------------------------


class NamedLLM:
    def __init__(self, model: str, temperature: float | None) -> None:
        self.model, self.temperature = model, temperature

    async def complete(self, messages: Any, *, tools: Any = (), **params: Any) -> Any:
        raise AssertionError("not called in these tests")


def model_runtime(
    built: list[NamedLLM], catalog: Any = None, **config: Any
) -> tuple[SessionController, SessionRuntime]:
    def factory(*, model: str, temperature: float | None) -> NamedLLM:
        llm = NamedLLM(model, temperature)
        built.append(llm)
        return llm

    runtime = SessionRuntime(
        make_config(**config),
        llm_factory=factory,
        start_chrome_cdp=no_start,
        wait_for_port=noop_wait,
        mcp_runtime_factory=no_mcp_runtime,
        model_catalog=catalog,
    )
    return SessionController(runtime), runtime


@pytest.mark.asyncio
async def test_set_model_rebuilds_the_model_and_the_agent_of_a_started_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents = install_fake_agent(monkeypatch, lambda _request: asyncio.sleep(0, native_result(final_answer="ok")))
    built: list[NamedLLM] = []
    controller, runtime = model_runtime(built, provider="openai_compat")
    await controller.start()

    view = await controller.set_model("gemma-4")

    assert (view.model, view.provider) == ("gemma-4", "openai_compat")
    assert [(llm.model, llm.temperature) for llm in built] == [("test-model", 0.1), ("gemma-4", 0.1)]
    assert runtime.context.llm is built[-1]
    assert runtime.harness.llm is built[-1]
    assert len(agents) == 2 and agents[-1].resources.llm is built[-1]
    session_file = json.loads((runtime.context.session_dir / "session.json").read_text(encoding="utf-8"))  # type: ignore[operator]
    assert session_file["config"]["model"] == "gemma-4"

    # The same model again changes nothing.
    await controller.set_model("gemma-4")
    assert len(built) == 2
    await runtime.close()


@pytest.mark.asyncio
async def test_a_model_chosen_before_the_start_and_kept_across_a_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_agent(monkeypatch, lambda _request: asyncio.sleep(0, native_result(final_answer="ok")))
    built: list[NamedLLM] = []
    controller, runtime = model_runtime(built)

    await controller.set_model("qwen3:8b")
    assert built == []  # nothing is started yet
    await controller.start()
    await controller.reset()
    await controller.start()

    assert [llm.model for llm in built] == ["qwen3:8b", "qwen3:8b"]
    await runtime.close()


@pytest.mark.asyncio
async def test_set_model_refuses_while_a_task_runs_and_rejects_an_empty_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()

    async def run(_request: TaskRequest) -> AgentLoopResult:
        await release.wait()
        return native_result(final_answer="done")

    install_fake_agent(monkeypatch, run)
    built: list[NamedLLM] = []
    controller, runtime = model_runtime(built)

    with pytest.raises(ValueError):
        await controller.set_model("  ")
    await controller.submit("long task")
    with pytest.raises(TaskBusyError):
        await controller.set_model("gemma-4")
    release.set()
    await controller.wait()
    assert controller.session_view().model == "test-model"
    await runtime.close()


@pytest.mark.asyncio
async def test_a_model_that_cannot_be_built_leaves_the_session_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agents = install_fake_agent(monkeypatch, lambda _request: asyncio.sleep(0, native_result(final_answer="ok")))
    built: list[NamedLLM] = []

    def factory(*, model: str, temperature: float | None) -> NamedLLM:
        if model == "broken":
            raise ValueError("llm.host is required for provider openai_compat")
        llm = NamedLLM(model, temperature)
        built.append(llm)
        return llm

    runtime = SessionRuntime(
        make_config(), llm_factory=factory, start_chrome_cdp=no_start,
        wait_for_port=noop_wait, mcp_runtime_factory=no_mcp_runtime,
    )
    controller = SessionController(runtime)
    await controller.start()

    with pytest.raises(ValueError, match="llm.host"):
        await controller.set_model("broken")

    assert controller.session_view().model == "test-model"
    assert runtime.context.llm is built[0] and runtime.harness.llm is built[0]
    assert len(agents) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_the_approval_classifier_follows_the_new_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("permissions:\n  approval_judge: classifier\n", encoding="utf-8")
    monkeypatch.setenv("AUTOBROWSER_CONFIG_FILE", str(profile))
    get_settings.cache_clear()
    install_fake_agent(monkeypatch, lambda _request: asyncio.sleep(0, native_result(final_answer="ok")))
    built: list[NamedLLM] = []
    controller, runtime = model_runtime(built)
    await controller.start()

    await controller.set_model("gemma-4")

    classifier = runtime.context.approval.classifier
    assert classifier is not None and classifier._llm is built[-1]
    assert built[-1].model == "gemma-4"
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["evil\x1b[2Jmodel", "bell\x07", "rtl\u202eid"])
async def test_a_model_id_with_control_characters_is_refused(model: str) -> None:
    controller, runtime = model_runtime([])

    with pytest.raises(ValueError, match="control"):
        await controller.set_model(model)
    await runtime.close()


@pytest.mark.asyncio
async def test_available_models_come_from_the_catalog_sorted_and_unique() -> None:
    async def catalog() -> list[str]:
        return ["gpt-5", "", "gemma-4", "gpt-5", "evil\x1b[2Jmodel"]

    controller, runtime = model_runtime([], catalog=catalog)

    assert await controller.available_models() == ["gemma-4", "gpt-5"]
    await runtime.close()


@pytest.mark.asyncio
async def test_without_a_catalog_the_list_is_refused_with_a_hint() -> None:
    controller, runtime = model_runtime([])

    with pytest.raises(RuntimeError, match="name the model"):
        await controller.available_models()
    await runtime.close()


@pytest.mark.asyncio
async def test_a_provider_error_does_not_break_the_next_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: a task failing with ModelProviderError (an unknown model, 404) used to
    make every later save of the session raise, so no further task could start."""

    from src.llm import ModelProviderError

    calls: list[str] = []

    async def run(request: TaskRequest) -> AgentLoopResult:
        calls.append(request.task)
        if len(calls) == 1:
            raise ModelProviderError(
                "ollama 404: model or endpoint not found: model 'gpt-6.1-sol' not found",
                provider="ollama",
                status_code=404,
            )
        return native_result(final_answer="ok")

    install_fake_agent(monkeypatch, run)
    controller, runtime, _, _ = build()

    with pytest.raises(ModelProviderError, match="404"):
        await controller.run("first")
    result = await controller.run("second")

    assert result.final_answer == "ok"
    assert calls == ["first", "second"]
    tasks = json.loads((runtime.context.session_dir / "tasks.json").read_text(encoding="utf-8"))  # type: ignore[operator]
    assert [task["status"] for task in tasks] == ["failed", "completed"]
    assert tasks[0]["result"] == {
        "type": "ModelProviderError",
        "message": "ollama 404: model or endpoint not found: model 'gpt-6.1-sol' not found",
    }
    await runtime.close()


@pytest.mark.asyncio
async def test_any_exception_with_keyword_only_fields_can_be_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    class StrictError(RuntimeError):
        def __init__(self, message: str, *, code: int) -> None:
            super().__init__(message)
            self.code = code

    async def run(_request: TaskRequest) -> AgentLoopResult:
        raise StrictError("strict", code=7)

    install_fake_agent(monkeypatch, run)
    controller, runtime, _, _ = build()

    with pytest.raises(StrictError):
        await controller.run("first")

    tasks = json.loads((runtime.context.session_dir / "tasks.json").read_text(encoding="utf-8"))  # type: ignore[operator]
    assert tasks[0]["result"] == {"type": "StrictError", "message": "strict"}
    await runtime.close()


@pytest.mark.asyncio
async def test_a_failed_save_at_task_start_does_not_leave_the_session_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_agent(monkeypatch, lambda _request: asyncio.sleep(0, native_result(final_answer="ok")))
    controller, runtime, _, _ = build()
    await controller.start()
    real_persist = runtime.context.persist
    failures = iter([PermissionError("tasks.json is locked")])

    def flaky_persist() -> None:
        for error in failures:
            raise error
        real_persist()

    monkeypatch.setattr(runtime.context, "persist", flaky_persist)

    with pytest.raises(PermissionError):
        await controller.run("first")
    assert runtime.context.current_task is None and runtime.context.tasks == []
    result = await controller.run("second")

    assert result.final_answer == "ok"
    await controller.set_model("gemma-4")  # not refused as "a task is running"
    await runtime.close()


@pytest.mark.asyncio
async def test_a_failed_save_at_task_end_keeps_the_task_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_agent(monkeypatch, lambda _request: asyncio.sleep(0, native_result(final_answer="ok")))
    controller, runtime, _, _ = build()
    await controller.start()
    real_persist = runtime.context.persist
    calls = {"n": 0}

    def persist_fails_at_the_end() -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # 1: task start, 2: task end
            raise PermissionError("tasks.json is locked")
        real_persist()

    monkeypatch.setattr(runtime.context, "persist", persist_fails_at_the_end)

    result = await controller.run("first")

    assert result.final_answer == "ok"
    assert runtime.context.current_task is None
    assert runtime.context.tasks[0].status == "completed"
    await runtime.close()


@pytest.mark.asyncio
async def test_a_failed_save_after_a_failed_task_keeps_the_task_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.llm import ModelProviderError

    async def run(_request: TaskRequest) -> AgentLoopResult:
        raise ModelProviderError("ollama 404: model not found", provider="ollama", status_code=404)

    install_fake_agent(monkeypatch, run)
    controller, runtime, _, _ = build()
    await controller.start()
    failed: list[object] = []
    runtime.context.events.subscribe("task.failed", lambda _name, record: failed.append(record))
    real_persist = runtime.context.persist
    calls = {"n": 0}

    def persist_fails_at_the_end() -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # 1: task start, 2: task end
            raise PermissionError("tasks.json is locked")
        real_persist()

    monkeypatch.setattr(runtime.context, "persist", persist_fails_at_the_end)

    with pytest.raises(ModelProviderError, match="404"):
        await controller.run("first")

    assert runtime.context.current_task is None
    assert runtime.context.tasks[0].status == "failed"
    assert failed == [runtime.context.tasks[0]]
    await runtime.close()


@pytest.mark.asyncio
async def test_skills_view_reflects_the_session_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    controller, runtime, _, _ = build()
    # Before the first task the session has not read its skills yet.
    assert controller.skills().loaded is False

    write_skill(tmp_path / "builtin", "rules", metadata='metadata:\n  always: "true"')
    write_skill(tmp_path / "user", "rules", description="Mine.")
    write_skill(tmp_path / "user", "price-compare", description="Compare prices.")
    write_skill(tmp_path / "user", "broken", frontmatter="name: broken")
    runtime.context.skill_store = SkillStore.load(
        SkillsSettings(enabled=True),
        roots=[("builtin", tmp_path / "builtin"), ("user", tmp_path / "user")],
    )

    view = controller.skills()

    assert view.enabled is True
    assert [(skill.name, skill.source, skill.always) for skill in view.skills] == [
        ("price-compare", "user", False),
        ("rules", "user", False),
    ]
    assert view.shadowed == ("rules",)
    assert view.problems == ("user/broken: description is missing",)

    runtime.context.skill_store = SkillStore.load(
        SkillsSettings(),
        roots=[("builtin", tmp_path / "builtin")],
    )
    off = controller.skills()
    assert (off.enabled, [skill.name for skill in off.skills]) == (False, ["rules"])  # always-on only
    await runtime.close()


@pytest.mark.asyncio
async def test_a_multi_line_skill_problem_is_one_row(tmp_path: Path) -> None:
    controller, runtime, _, _ = build()
    write_skill(tmp_path, "yaml", frontmatter="name: [unclosed\ndescription: x")
    runtime.context.skill_store = SkillStore.load(SkillsSettings(enabled=True), roots=[("user", tmp_path)])

    (problem,) = controller.skills().problems

    assert problem.startswith("user/yaml: ") and "\n" not in problem
    await runtime.close()
