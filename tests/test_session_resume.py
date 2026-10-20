"""Durable session state and ``--continue``/``--resume`` (``src/harness/session_store.py``)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from src.agent_loop.execution.loop import AgentLoopResult
from src.cli.sessions import ResumeError, resolve_resume, session_rows
from src.config import CONFIG_FILE_ENV_VAR, get_settings
from src.contracts import TaskRequest
from src.harness.session import SessionRuntime
from src.harness.session_store import (
    STATE_FILE_NAME,
    SessionInUseError,
    SessionLock,
    SessionNotFoundError,
    session_in_use,
    find_session,
    last_event_sequence,
    latest_resumable,
    list_sessions,
    load_state,
    save_state,
)
from src.messages import (
    Message,
    ToolCall,
    assistant_message,
    message_from_dict,
    message_to_dict,
    tool_message,
    user_message,
)
from tests.session_fakes import install_fake_agent
from tests.test_harness_session import (
    llm_factory,
    make_config,
    native_result,
    no_mcp_runtime,
    no_start,
    noop_wait,
)


@pytest.fixture(autouse=True)
def _neutral_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The personal config.yaml must not decide memory or storage here."""

    profile = tmp_path / "profile.yaml"
    profile.write_text("flags: {}\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(profile))
    get_settings.cache_clear()


def runtime(resume_from: Path | None = None) -> SessionRuntime:
    return SessionRuntime(
        make_config(),
        llm_factory=llm_factory,
        start_chrome_cdp=no_start,
        wait_for_port=noop_wait,
        mcp_runtime_factory=no_mcp_runtime,
        output_fn=lambda *_args, **_kwargs: None,
        resume_from=resume_from,
    )


def test_messages_round_trip_through_plain_json() -> None:
    call = ToolCall(id="c1", name="any_tool", arguments={"url": "https://x.test", "n": [1, 2]})
    messages = [
        user_message("найди"),
        Message(role="assistant", content="", tool_calls=(call,)),
        tool_message("c1", "result", name="any_tool"),
        assistant_message("готово"),
    ]

    restored = [message_from_dict(json.loads(json.dumps(message_to_dict(m)))) for m in messages]

    assert restored == messages
    with pytest.raises(ValueError):
        message_from_dict({"role": "robot"})


def test_state_file_round_trip_and_session_listing(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    for name, activity in (("aaa111", "2026-10-19T10:00:00+00:00"), ("aab222", "2026-10-20T10:00:00+00:00")):
        folder = sessions / name
        folder.mkdir(parents=True)
        (folder / "session.json").write_text(
            json.dumps({"session_id": name, "metadata": {"last_activity": activity}}), encoding="utf-8"
        )
        (folder / "tasks.json").write_text(json.dumps([{"task": f"task of {name}"}]), encoding="utf-8")
    save_state(sessions / "aaa111", "aaa111", {"messages": [user_message("hi")], "observation": "page"})
    (sessions / "aaa111" / "events.jsonl").write_text(
        '{"sequence": 3}\n\n{"sequence": 7}\nnot json\n', encoding="utf-8"
    )

    listed = list_sessions(sessions)

    assert [s.session_id for s in listed] == ["aab222", "aaa111"]  # most recent first
    assert [s.resumable for s in listed] == [False, True]
    assert latest_resumable(sessions).session_id == "aaa111"  # type: ignore[union-attr]
    assert find_session(sessions, "aab").session_id == "aab222"
    with pytest.raises(SessionNotFoundError, match="several"):
        find_session(sessions, "aa")
    with pytest.raises(SessionNotFoundError, match="No stored session"):
        find_session(sessions, "zzz")
    assert load_state(sessions / "aaa111") == {"messages": [user_message("hi")], "observation": "page"}
    assert last_event_sequence(sessions / "aaa111") == 7
    assert session_rows(listed)[1].startswith("aaa111")


@pytest.mark.asyncio
async def test_a_resumed_session_continues_the_dialogue_ids_tasks_and_event_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[TaskRequest] = []

    async def run(request: TaskRequest) -> AgentLoopResult:
        requests.append(request)
        history = list(request.state_overrides.get("messages") or [])
        return native_result(
            final_answer=f"answer {len(requests)}",
            messages=[*history, user_message(request.task), assistant_message(f"answer {len(requests)}")],
            observation=f"page {len(requests)}",
        )

    install_fake_agent(monkeypatch, run)
    first = runtime()
    await first.run_task("первая задача")
    session_dir = first.context.session_dir
    session_id = first.context.session_id
    sequence = first.context.event_emitter.sequence
    await first.close()
    assert session_dir is not None and (session_dir / STATE_FILE_NAME).is_file()

    second = runtime(resume_from=session_dir)
    assert second.context.session_id == session_id
    assert [task.task for task in second.context.tasks] == ["первая задача"]
    assert second.context.tasks[0].status == "completed"
    await second.run_task("вторая задача")

    carried = requests[1].state_overrides
    assert [m.content for m in carried["messages"]][-2:] == ["первая задача", "answer 1"]
    # Only the dialogue survives a restart: page state described a browser that is gone.
    assert "observation" not in carried or carried["observation"] in ("", None)
    assert requests[1].session_id == session_id
    assert second.context.event_emitter.sequence > sequence
    records = [json.loads(line) for line in (session_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    sequences = [record["sequence"] for record in records]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
    assert [task.task for task in second.context.tasks] == ["первая задача", "вторая задача"]
    await second.close()


@pytest.mark.asyncio
async def test_persist_state_off_writes_no_state_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile = tmp_path / "off.yaml"
    profile.write_text("storage:\n  persist_state: false\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(profile))
    get_settings.cache_clear()
    install_fake_agent(monkeypatch, lambda _request: _done())
    session = runtime()

    await session.run_task("task")

    assert session.context.session_dir is not None
    assert not (session.context.session_dir / STATE_FILE_NAME).exists()
    await session.close()


async def _done() -> AgentLoopResult:
    return native_result(final_answer="ok")


def _store(sessions: Path, name: str, *, resumable: bool = True) -> Path:
    folder = sessions / name
    folder.mkdir(parents=True)
    (folder / "session.json").write_text(json.dumps({"session_id": name, "metadata": {}}), encoding="utf-8")
    (folder / "tasks.json").write_text(json.dumps([{"task": "t", "status": "completed"}]), encoding="utf-8")
    if resumable:
        save_state(folder, name, {"messages": []})
    return folder


def ns(**values: Any) -> argparse.Namespace:
    return argparse.Namespace(continue_session=values.get("continue_session", False), resume=values.get("resume"))


def test_resolve_resume_by_continue_id_prefix_and_picker(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    one = _store(sessions, "1111aaaa")
    _store(sessions, "2222bbbb", resumable=False)

    assert resolve_resume(ns(), interactive=True, sessions_dir=sessions) is None
    assert resolve_resume(ns(continue_session=True), interactive=False, sessions_dir=sessions) == one
    assert resolve_resume(ns(resume="1111"), interactive=False, sessions_dir=sessions) == one
    with pytest.raises(ResumeError, match="no saved dialogue"):
        resolve_resume(ns(resume="2222"), interactive=False, sessions_dir=sessions)
    with pytest.raises(ResumeError, match="needs a terminal"):
        resolve_resume(ns(resume=""), interactive=False, sessions_dir=sessions)

    written: list[str] = []
    answers = iter(["9", "1"])
    picked = resolve_resume(
        ns(resume=""), interactive=True, sessions_dir=sessions, read=lambda _p: next(answers), write=written.append
    )
    assert picked == one
    assert any(row.strip().startswith("1  1111aaaa") for row in written)
    assert "Enter a number from 1 to 1." in written


def test_nothing_to_continue_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ResumeError, match="No session to continue"):
        resolve_resume(ns(continue_session=True), interactive=True, sessions_dir=tmp_path / "none")


def test_a_session_without_tasks_is_not_resumable_and_leaves_no_record(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    empty = sessions / "0000empty"
    empty.mkdir(parents=True)
    (empty / "session.json").write_text(json.dumps({"session_id": "0000empty", "metadata": {}}), encoding="utf-8")
    save_state(empty, "0000empty", {"messages": []})
    real = _store(sessions, "1111real")

    assert [s.session_id for s in list_sessions(sessions) if s.resumable] == ["1111real"]
    assert resolve_resume(ns(continue_session=True), interactive=False, sessions_dir=sessions) == real


@pytest.mark.asyncio
async def test_a_never_started_session_writes_nothing_on_close() -> None:
    session = runtime()

    await session.close()

    assert session.context.session_dir is None
    assert list_sessions(get_settings().storage.sessions_dir) == []


def test_a_damaged_state_file_or_a_session_open_elsewhere_is_a_clean_error(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    broken = _store(sessions, "brokenstate")
    (broken / STATE_FILE_NAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(ResumeError, match="cannot be resumed"):
        resolve_resume(ns(resume="broken"), interactive=False, sessions_dir=sessions)

    busy = _store(sessions, "busysession")
    lock = SessionLock.acquire(busy)
    try:
        assert session_in_use(busy) is True
        with pytest.raises(ResumeError, match="open in another AutoBrowser process"):
            resolve_resume(ns(resume="busy"), interactive=False, sessions_dir=sessions)
        with pytest.raises(SessionInUseError):
            SessionLock.acquire(busy)
    finally:
        lock.release()
    # Released (or left behind by a crash: the OS drops the lock): free again.
    assert session_in_use(busy) is False
    assert resolve_resume(ns(resume="busy"), interactive=False, sessions_dir=sessions) == busy


@pytest.mark.asyncio
async def test_a_resumed_session_holds_its_lock_until_close_and_never_writes_stale_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_agent(monkeypatch, lambda _request: _done())
    first = runtime()
    await first.run_task("t1")
    session_dir = first.context.session_dir
    assert session_dir is not None
    await first.close()

    idle = runtime(resume_from=session_dir)  # e.g. a TUI opened with --continue, doing nothing
    assert session_in_use(session_dir) is True
    with pytest.raises(SessionInUseError):
        runtime(resume_from=session_dir)  # a second process / -p --continue is refused
    await idle.close()
    assert session_in_use(session_dir) is False

    second = runtime(resume_from=session_dir)
    await second.run_task("t2")
    await second.close()
    tasks = json.loads((session_dir / "tasks.json").read_text(encoding="utf-8"))
    assert [task["task"] for task in tasks] == ["t1", "t2"]


def test_a_malformed_state_file_is_a_value_error(tmp_path: Path) -> None:
    for content in (
        "[]",
        "null",
        '{"version": 1, "state": {"messages": [{"role": "user", "tool_calls": [{"arguments": 5}]}]}}',
        '{"version": 1, "state": {"messages": "hello"}}',
        '{"version": 1, "state": {"messages": [{"role": "user", "name": 5}]}}',
    ):
        folder = tmp_path / str(abs(hash(content)))
        folder.mkdir()
        (folder / STATE_FILE_NAME).write_text(content, encoding="utf-8")
        with pytest.raises(ValueError):
            load_state(folder)



def test_a_failed_restore_releases_the_lock(tmp_path: Path) -> None:
    folder = _store(tmp_path / "sessions", "torn")
    (folder / STATE_FILE_NAME).write_text('{"version": 1, "state": {"messages": "x"}}', encoding="utf-8")

    with pytest.raises(ValueError):
        runtime(resume_from=folder)

    assert session_in_use(folder) is False


@pytest.mark.asyncio
async def test_a_new_session_takes_its_lock_at_start_and_records_itself_without_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_agent(monkeypatch, lambda _request: _done())
    session = runtime()
    assert session.context.session_lock is None

    await session.start()
    session_dir = session.context.session_dir
    assert session_dir is not None and session_in_use(session_dir) is True
    with pytest.raises(SessionInUseError):
        SessionLock.acquire(session_dir)
    await session.close()

    assert session_in_use(session_dir) is False
    listed = list_sessions(get_settings().storage.sessions_dir)
    assert [(s.session_id, s.task_count, s.resumable) for s in listed] == [
        (session.context.session_id, 0, False)
    ]


def test_persist_writes_nothing_without_the_lock(tmp_path: Path) -> None:
    from src.harness.session import SessionContext

    context = SessionContext(make_config(), session_dir=tmp_path / "unlocked")
    (tmp_path / "unlocked").mkdir()
    context.persist()

    assert list((tmp_path / "unlocked").iterdir()) == []


def test_a_file_system_that_cannot_lock_is_not_reported_as_in_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import errno

    import src.harness.session_store as store

    def no_locks(_handle: object) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(store, "_lock", no_locks)

    with pytest.raises(OSError, match="No locks available") as raised:
        SessionLock.acquire(tmp_path / "nfs")
    assert not isinstance(raised.value, SessionInUseError)
