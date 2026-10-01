"""The basic command hooks in ``scripts/hooks/``, run as real processes through ``CommandHook``.

Engines are built from an explicit ``HooksSettings(...)``, never from ``get_settings()``.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
import yaml

from src.config import ROOT_PATH, HooksSettings
from src.contracts import HookEvent, HookResult
from src.harness.command_hooks import CommandHook
from src.harness.hooks import HookEngine

HOOKS_DIR = ROOT_PATH / "scripts" / "hooks"
NBSP = chr(0xA0)
CARD = "4111 1111 1111 1111"


def _command(script: str, *options: str) -> str:
    return " ".join([f'"{sys.executable}"', f'"{HOOKS_DIR / script}"', *options])


async def run(script: str, event: HookEvent, *options: str) -> HookResult | None:
    return await CommandHook(_command(script, *options))(event)


def tool_event(tool: str, task: str = "Shop.", **args: Any) -> HookEvent:
    return HookEvent(
        name="pre_tool_use",
        session_id="session-1",
        goal_id="task-1",
        task_id="task-1",
        task=task,
        tool=tool,
        server="playwright",
        args=args,
    )


def result_event(text: str, *, failed: bool = False, task: str = "Shop.") -> HookEvent:
    return HookEvent(
        name="post_tool_use_failure" if failed else "post_tool_use",
        session_id="session-1",
        goal_id="task-1",
        task_id="task-1",
        task=task,
        tool="browser_snapshot",
        server="playwright",
        result={
            "name": "browser_snapshot",
            "status": "error" if failed else "success",
            "content": "" if failed else text,
            "error": text if failed else "",
        },
    )


def stop_event(answer: str, *evidence: str, task: str = "Find a jacket.") -> HookEvent:
    return HookEvent(
        name="stop",
        session_id="session-1",
        goal_id="task-1",
        task_id="task-1",
        task=task,
        final_answer=answer,
        evidence=evidence,
    )


# --------------------------------------------------------------------------
# sensitive_action_guard.py
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "element",
    ["Кнопка «Оплатить онлайн»", f"Оформить{NBSP} заказ", "Place   Order", "Удалить аккаунт"],
)
async def test_sensitive_action_guard_asks(element: str) -> None:
    result = await run("sensitive_action_guard.py", tool_event("browser_click", element=element))

    assert result is not None and result.decision == "ask"
    assert "irreversible" in result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("element", ["Payment methods", "В корзину", ""])
async def test_sensitive_action_guard_ignores_ordinary_controls(element: str) -> None:
    event = tool_event("browser_click", element=element)

    assert await run("sensitive_action_guard.py", event) is None


@pytest.mark.asyncio
async def test_sensitive_action_guard_deny_and_custom_keywords() -> None:
    options = ("--decision", "deny", "--keyword", '"Отправить заявку"')

    denied = await run(
        "sensitive_action_guard.py", tool_event("browser_click", element="отправить заявку"), *options
    )
    default_gone = await run(
        "sensitive_action_guard.py", tool_event("browser_click", element="Оплатить"), *options
    )

    assert denied is not None and denied.decision == "deny"
    assert "отправить заявку" in denied.reason
    assert default_gone is None


@pytest.mark.asyncio
async def test_bad_option_is_a_hook_failure_not_a_block() -> None:
    with pytest.raises(RuntimeError, match="exit code 1"):
        await run("sensitive_action_guard.py", tool_event("browser_click"), "--decision", "allow")


# --------------------------------------------------------------------------
# secret_input_guard.py
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"element": "Card", "ref": "e3", "text": CARD},
        {"element": "Card", "ref": "e3", "text": "4111-1111-1111-1111"},
        {"fields": [{"name": "Card", "ref": "e3", "type": "textbox", "value": "4111111111111111"}]},
    ],
)
async def test_secret_input_guard_denies_card_numbers(args: dict[str, Any]) -> None:
    result = await run("secret_input_guard.py", tool_event("browser_type", **args))

    assert result is not None and result.decision == "deny"
    assert "4111" not in result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["4111 1111 1111 1112", "куртка 1 299 ₽", "+7 495 123-45-67", "order 1234567890"]
)
async def test_secret_input_guard_lets_ordinary_text_through(text: str) -> None:
    event = tool_event("browser_type", element="Search", ref="e2", text=text)

    assert await run("secret_input_guard.py", event) is None


@pytest.mark.asyncio
async def test_secret_input_guard_extra_pattern_with_ask() -> None:
    event = tool_event("browser_type", element="Key", text="sk-ABCDEFGH1234")

    result = await run("secret_input_guard.py", event, "--decision", "ask", "--pattern", "sk-[A-Za-z0-9]{8,}")

    assert result is not None and result.decision == "ask"


# --------------------------------------------------------------------------
# page_obstacle_detector.py
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page",
    [
        '- heading "Подтвердите, что вы не робот" ref=e1',
        '- iframe "reCAPTCHA" ref=e4',
        '- heading "Access Denied" ref=e1',
        "- Page Title: Just a moment...",
    ],
)
async def test_page_obstacle_detector_adds_context(page: str) -> None:
    result = await run("page_obstacle_detector.py", result_event(page))

    assert result is not None
    assert result.decision is None and result.updated_output is None
    assert "bot check" in result.additional_context


@pytest.mark.asyncio
async def test_page_obstacle_detector_ignores_normal_pages_and_takes_extra_patterns() -> None:
    page = '- text "Слишком много запросов" ref=e2'

    assert await run("page_obstacle_detector.py", result_event('- link "Куртка" ref=e10')) is None
    assert await run("page_obstacle_detector.py", result_event(page)) is None
    extra = await run("page_obstacle_detector.py", result_event(page), "--pattern", "много\\s+запросов")
    assert extra is not None and "bot check" in extra.additional_context


# --------------------------------------------------------------------------
# pii_redaction.py
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pii_redaction_masks_email_phone_and_card() -> None:
    page = (
        '- text "support@shop.ru" ref=e1\n'
        '- text "+7 (495) 123-45-67" ref=e2\n'
        '- text "8 800 555-35-35" ref=e3\n'
        f'- text "{CARD}" ref=e4\n'
        '- text "1 299 ₽" ref=e1234567890123'
    )

    result = await run("pii_redaction.py", result_event(page))

    assert result is not None and result.updated_output is not None
    output = result.updated_output
    assert "support@shop.ru" not in output and "[redacted email]" in output
    assert output.count("[redacted phone]") == 2
    assert "[redacted card]" in output and "4111" not in output
    assert "1 299 ₽" in output and "ref=e1234567890123" in output
    assert "4 value(s)" in result.user_message


@pytest.mark.asyncio
async def test_pii_redaction_keeps_task_values_masks_errors_and_honors_kinds() -> None:
    from_task = result_event('- textbox "Email": me@mail.ru', task="Type me@mail.ru.")
    failed = result_event("Timeout while typing a@b.com", failed=True)

    assert await run("pii_redaction.py", from_task) is None
    assert await run("pii_redaction.py", result_event('- text "a@b.com"'), "--kinds", "card") is None
    result = await run("pii_redaction.py", failed)
    assert result is not None and result.updated_output == "Timeout while typing [redacted email]"


# --------------------------------------------------------------------------
# grounded_urls.py
# --------------------------------------------------------------------------

SNAPSHOT = (
    "- Page URL: https://www.ozon.ru/search/?text=jacket\n"
    '- link "Куртка зимняя" ref=e10:\n'
    "  - /url: /product/kurtka-zimnyaya-123456/"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        "Нашёл: https://www.ozon.ru/product/kurtka-zimnyaya-123456/.",
        "[Куртка](https://www.ozon.ru/product/kurtka-zimnyaya-123456/?at=abc)",
        "Сайт: https://www.ozon.ru",
        "Куртка без ссылки, 1 299 ₽.",
    ],
)
async def test_grounded_urls_accepts_observed_links(answer: str) -> None:
    assert await run("grounded_urls.py", stop_event(answer, SNAPSHOT)) is None


@pytest.mark.asyncio
async def test_grounded_urls_rejects_invented_links() -> None:
    answer = "Куртка: https://www.ozon.ru/product/kurtka-999/, ещё https://example.com/x"

    result = await run("grounded_urls.py", stop_event(answer, SNAPSHOT))

    assert result is not None and result.decision == "deny"
    assert "https://www.ozon.ru/product/kurtka-999/" in result.reason
    assert "https://example.com/x" in result.reason


# --------------------------------------------------------------------------
# The registry from config.example.yaml
# --------------------------------------------------------------------------


def _example_registry() -> list[dict[str, Any]]:
    """The commented basic-hooks registry in ``config.example.yaml``, uncommented."""

    text = (ROOT_PATH / "config.example.yaml").read_text(encoding="utf-8")
    start = text.index("  # registry:", text.index("Basic command hooks"))
    lines = []
    for line in text[start:].splitlines():
        if not line.startswith("  #"):
            break
        lines.append(line[4:])
    return yaml.safe_load("\n".join(lines))["registry"]


def test_example_registry_names_every_basic_hook_script() -> None:
    commands = [spec["command"] for spec in _example_registry()]

    assert sorted(command.split()[-1].rsplit("/", 1)[-1] for command in commands) == sorted(
        path.name for path in HOOKS_DIR.glob("*.py")
    )


@pytest.mark.asyncio
async def test_example_registry_loads_and_runs() -> None:
    registry = _example_registry()
    for spec in registry:  # the example says `python`; run the scripts with this interpreter
        spec["command"] = spec["command"].replace("python ", f'"{sys.executable}" ', 1)
    engine = HookEngine.from_settings(
        HooksSettings(enabled=True, registry=registry), progress_timeout_seconds=120.0
    )

    click = await engine.run(tool_event("browser_click", element="Оплатить", ref="e7"))
    typed = await engine.run(tool_event("browser_type", element="Card", ref="e3", text=CARD))
    page = await engine.run(result_event('- heading "Не робот?" ref=e1\n- text "a@b.com"'))
    stop = await engine.run(stop_event("См. https://example.com/x", SNAPSHOT))

    assert click.decision == "ask"
    assert typed.decision == "deny"
    assert page.updated_output is not None and "[redacted email]" in page.updated_output
    assert "bot check" in page.additional_context
    assert stop.decision == "deny"
