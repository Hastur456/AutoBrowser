"""Interactive approvals in the CLI and the session permission mode."""

from __future__ import annotations

import argparse
import asyncio
import threading
from typing import Any

import pytest

from src.cli.approval import ApprovalPrompt, approval_question
from src.cli.bootstrap import build_session, effective_permission_mode, is_interactive
from src.cli.parser import build_parser
from src.config import PermissionsSettings, Settings
from src.contracts import PermissionVerdict

REQUEST = {"name": "browser_click", "args": {"element": "Купить", "target": "e2"}}
GRANTABLE = PermissionVerdict(
    decision="ask",
    reason="Approval required by rule buy.",
    source="rule",
    rule_id="buy",
    grant_key=("playwright", "browser_click", "ozon.ru"),
)
ALWAYS = PermissionVerdict(
    decision="ask",
    reason="Running JavaScript in the page needs approval (browser_evaluate).",
    source="rule",
    rule_id="browser-evaluate",
    always_ask=True,
)


async def ask(prompt: ApprovalPrompt, verdict: PermissionVerdict, *answers: str) -> str:
    """Run the callback and answer it from another thread, like the cmd2 prompt does."""

    task = asyncio.create_task(prompt(REQUEST, verdict.reason, verdict))

    def reply() -> None:
        for text in answers:
            while not prompt.pending:
                threading.Event().wait(0.005)
            prompt.answer(text)

    await asyncio.to_thread(reply)
    return await task


# --------------------------------------------------------------------------- the prompt


def test_the_question_names_tool_domain_rule_and_choices() -> None:
    text = approval_question(REQUEST, GRANTABLE.reason, GRANTABLE)
    assert text.startswith("Approval needed (rule buy): browser_click on ozon.ru")
    assert "Approval required by rule buy." in text
    assert '"element": "Купить"' in text
    assert "[y] once   [s] session for browser_click on ozon.ru   [n] deny" in text


def test_always_ask_hides_the_session_choice() -> None:
    text = approval_question({"name": "browser_evaluate"}, ALWAYS.reason, ALWAYS)
    assert "[s]" not in text and "[y] once   [n] deny" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (["y"], "once"),
        (["да"], "once"),
        (["s"], "session"),
        (["n"], "deny"),
        (["maybe", "нет"], "deny"),
    ],
)
async def test_answers(answers: list[str], expected: str) -> None:
    out: list[str] = []
    prompt = ApprovalPrompt(output=out.append, timeout_seconds=5)
    assert await ask(prompt, GRANTABLE, *answers) == expected
    assert not prompt.pending
    if answers[0] == "maybe":
        assert "Answer y / s / n." in out


@pytest.mark.asyncio
async def test_session_is_not_an_answer_for_always_ask() -> None:
    out: list[str] = []
    prompt = ApprovalPrompt(output=out.append, timeout_seconds=5)
    assert await ask(prompt, ALWAYS, "s", "y") == "once"
    assert "Answer y / n." in out


@pytest.mark.asyncio
async def test_no_answer_in_time_is_a_deny() -> None:
    out: list[str] = []
    prompt = ApprovalPrompt(output=out.append, timeout_seconds=0.05)
    assert await prompt(REQUEST, GRANTABLE.reason, GRANTABLE) == "deny"
    assert out[-1] == "No answer in 0.05s: denied."
    assert not prompt.pending
    assert prompt.answer("y") is False  # late answers are ignored


@pytest.mark.asyncio
async def test_serve_answers_on_the_calling_thread() -> None:
    lines = iter(["?", "s"])
    prompt = ApprovalPrompt(output=lambda _text: None, timeout_seconds=5)
    task = asyncio.create_task(prompt(REQUEST, GRANTABLE.reason, GRANTABLE))
    await asyncio.sleep(0)
    await asyncio.to_thread(prompt.serve, lambda _prompt: next(lines))
    assert await task == "session"


@pytest.mark.asyncio
async def test_serve_treats_end_of_input_as_deny() -> None:
    def closed(_prompt: str) -> str:
        raise EOFError

    prompt = ApprovalPrompt(output=lambda _text: None, timeout_seconds=5)
    task = asyncio.create_task(prompt(REQUEST, GRANTABLE.reason, GRANTABLE))
    await asyncio.sleep(0)
    await asyncio.to_thread(prompt.serve, closed)
    assert await task == "deny"


# --------------------------------------------------------------------------- the mode


def args(**overrides: Any) -> argparse.Namespace:
    parsed = build_parser().parse_args(["--no-mcp"])
    for key, value in overrides.items():
        setattr(parsed, key, value)
    return parsed


def use_permissions(monkeypatch: pytest.MonkeyPatch, permissions: PermissionsSettings) -> None:
    settings = Settings(permissions=permissions)
    monkeypatch.setattr("src.cli.bootstrap.get_settings", lambda: settings)
    monkeypatch.setattr("src.harness.session.get_settings", lambda: settings)


def test_the_parser_accepts_a_permission_mode() -> None:
    assert build_parser().parse_args([]).permission_mode is None
    assert build_parser().parse_args(["--permission-mode", "bypass"]).permission_mode == "bypass"
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--permission-mode", "yolo"])


def test_effective_permission_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    use_permissions(monkeypatch, PermissionsSettings())
    assert effective_permission_mode(args(), interactive=True) is None
    assert effective_permission_mode(args(), interactive=False) == "dont_ask"
    explicit = args(permission_mode="default")
    assert effective_permission_mode(explicit, interactive=False) == "default"
    use_permissions(monkeypatch, PermissionsSettings(mode="read_only"))
    assert effective_permission_mode(args(), interactive=False) is None


def test_is_interactive_prefers_the_explicit_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    assert is_interactive(args()) is True
    assert is_interactive(args(interactive=False)) is False


@pytest.mark.asyncio
async def test_the_session_uses_the_mode_and_the_human_callback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.chdir(tmp_path)
    use_permissions(monkeypatch, PermissionsSettings())
    captured: list[dict[str, Any]] = []

    def runner_factory(resources: Any, **kwargs: Any) -> Any:
        captured.append(kwargs)

        async def run(*_args: Any) -> Any:
            from src.agent_loop.execution.loop import AgentLoopResult
            from src.agent_loop.execution.state import LoopState

            return AgentLoopResult("done", "ok", {}, LoopState())

        return run

    monkeypatch.setattr("src.harness.session.native_task_runner", runner_factory)
    approvals = ApprovalPrompt()
    out: list[str] = []
    session = build_session(
        args(permission_mode="bypass", interactive=True),
        llm_factory=lambda **_kwargs: object(),
        human_input=approvals,
        output_fn=lambda *items, **_kw: out.append(" ".join(map(str, items))),
    )
    try:
        await session.run_task("first")
        await session.run_task("second")
    finally:
        await session.close()

    assert session.context.permissions.mode == "bypass"
    assert [kwargs["human_input"] for kwargs in captured] == [approvals, approvals]
    assert sum("permission mode 'bypass'" in line for line in out) == 1


@pytest.mark.asyncio
async def test_a_headless_session_denies_instead_of_asking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.chdir(tmp_path)
    use_permissions(monkeypatch, PermissionsSettings())
    session = build_session(args(interactive=False), llm_factory=lambda **_kwargs: object())
    try:
        await session.start()
        assert session.context.permissions.mode == "dont_ask"
    finally:
        await session.close()


def test_the_cmd2_shell_routes_the_next_line_to_a_pending_approval() -> None:
    from src.cli.agent_cli import AgentCli

    class Runtime:
        async def run_task(self, task: str) -> Any:
            raise AssertionError(f"no task should start: {task}")

    prompt = ApprovalPrompt(output=lambda _text: None, timeout_seconds=5)
    cli = AgentCli(Runtime(), use_color=False, approvals=prompt)  # type: ignore[arg-type]
    try:
        future = cli._runtime_loop.submit(prompt(REQUEST, GRANTABLE.reason, GRANTABLE))
        while not prompt.pending:
            threading.Event().wait(0.005)
        cli.onecmd_plus_hooks("s")
        assert future.result(timeout=5) == "session"
    finally:
        cli._runtime_loop.stop()
