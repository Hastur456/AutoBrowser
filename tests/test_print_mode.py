"""Print mode (``autobrowser -p``): only the result on stdout, exit codes, formats."""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from src.agent_loop.execution.loop import AgentLoopResult
from src.cli import bootstrap
from src.cli.parser import build_parser
from src.config import CONFIG_FILE_ENV_VAR, UiSettings, get_settings
from src.contracts import TaskRequest
from src.ui.print_mode import run_print
from src.ui.views import SessionView, TaskView
from tests.session_fakes import install_fake_agent
from tests.test_harness_session import native_result


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _neutral_profile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("flags: {}\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_FILE_ENV_VAR, str(profile))
    get_settings.cache_clear()


def run(monkeypatch: pytest.MonkeyPatch, result: AgentLoopResult | Exception, *argv: str, stdin: str | None = None) -> tuple[int, str, str, list[TaskRequest]]:
    requests: list[TaskRequest] = []

    async def agent(request: TaskRequest) -> AgentLoopResult:
        requests.append(request)
        if isinstance(result, Exception):
            raise result
        return result

    install_fake_agent(monkeypatch, agent)
    args = build_parser().parse_args(["-p", "--no-mcp", *argv])
    out, err = io.StringIO(), io.StringIO()
    code = bootstrap.run_print_cli(
        args,
        UiSettings(),
        stdin=io.StringIO(stdin) if stdin is not None else Tty(),
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue(), requests


def test_text_prints_only_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err, requests = run(monkeypatch, native_result(final_answer="Готово: 3 варианта"), "найди")

    assert code == 0
    assert out == "Готово: 3 варианта\n"
    assert err == ""
    assert [r.task for r in requests] == ["найди"]


def test_the_task_can_come_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, _err, requests = run(monkeypatch, native_result(final_answer="ok"), stdin="задача из pipe\n")

    assert (code, out) == (0, "ok\n")
    assert requests[0].task == "задача из pipe"


def test_no_task_is_a_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err, requests = run(monkeypatch, native_result(final_answer="never"))

    assert (code, out, requests) == (2, "", [])
    assert "needs a task" in err


def test_json_carries_status_answer_turns_and_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    result = AgentLoopResult(
        status="done", final_answer="ok", session_state={}, state=native_result().state, turns=4
    )
    code, out, _err, _requests = run(monkeypatch, result, "--output-format", "json", "t")

    data = json.loads(out)
    assert code == 0
    assert data["status"] == "completed" and data["final_answer"] == "ok" and data["turns"] == 4
    assert data["session_id"] and data["task_id"].startswith("task-")


def test_blocked_exits_3_and_reports_on_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err, _requests = run(
        monkeypatch, native_result(final_answer="Blocked: approval denied", status="blocked"), "купи"
    )

    assert (code, out) == (3, "")
    assert err.strip() == "[blocked] Blocked: approval denied"


def test_a_failing_task_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err, _requests = run(monkeypatch, RuntimeError("model unreachable"), "--output-format", "json", "t")

    data = json.loads(out)
    assert code == 1
    assert data["status"] == "failed" and data["error"] == "RuntimeError: model unreachable"
    assert err == ""


def test_stream_json_is_feed_items_then_the_result(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, _err, _requests = run(
        monkeypatch, native_result(final_answer="ok"), "--output-format", "stream-json", "t"
    )

    lines = [json.loads(line) for line in out.splitlines()]
    assert code == 0
    assert lines[0]["type"] == "goal.started"
    assert lines[-1]["type"] == "result" and lines[-1]["status"] == "completed"
    assert any(line["type"] == "goal.completed" for line in lines[:-1])


def test_verbose_progress_goes_to_stderr_only(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, err, _requests = run(monkeypatch, native_result(final_answer="ok"), "--verbose", "t")

    assert (code, out) == (0, "ok\n")
    assert "[done]" in err


class StartFailsOnResumedSession:
    """A controller whose session already has a finished task and cannot start again."""

    async def run_outcome(self, text: str) -> object:
        raise RuntimeError("chrome did not start")

    def session_view(self) -> SessionView:
        return SessionView(session_id="sid")

    def task_views(self) -> list[TaskView]:
        return [TaskView(index=1, task_id="old", task="earlier", status="completed")]

    async def _nothing(self) -> AsyncIterator[object]:
        await asyncio.Event().wait()
        yield None

    def subscribe(self, after: object = None) -> AsyncIterator[object]:
        return self._nothing()


@pytest.mark.asyncio
async def test_a_resumed_session_that_cannot_start_reports_this_run_as_failed() -> None:
    out: list[str] = []
    err: list[str] = []

    code = await run_print(
        StartFailsOnResumedSession(),  # type: ignore[arg-type]
        "task",
        output_format="json",
        verbose=False,
        settings=UiSettings(),
        write_out=out.append,
        write_err=err.append,
    )

    data = json.loads(out[0])
    assert code == 1
    assert data["status"] == "failed" and data["task_id"] == ""
    assert data["error"] == "RuntimeError: chrome did not start"


def test_print_mode_on_a_resumed_session_reports_only_the_new_task(monkeypatch: pytest.MonkeyPatch) -> None:
    code, out, _err, _requests = run(monkeypatch, native_result(final_answer="first"), "--output-format", "json", "t1")
    first = json.loads(out)
    assert code == 0

    session_dir = get_settings().storage.session_dir(first["session_id"])
    install_fake_agent(monkeypatch, lambda _request: _blocked())
    args = build_parser().parse_args(["-p", "--no-mcp", "--output-format", "json", "t2"])
    stdout = io.StringIO()
    code = bootstrap.run_print_cli(
        args, UiSettings(), resume_from=session_dir, stdin=Tty(), stdout=stdout, stderr=io.StringIO()
    )

    second = json.loads(stdout.getvalue())
    assert code == 3
    assert second["status"] == "blocked" and second["task_id"] != first["task_id"]
    assert second["session_id"] == first["session_id"]


async def _blocked() -> AgentLoopResult:
    return native_result(final_answer="Blocked: no", status="blocked")
