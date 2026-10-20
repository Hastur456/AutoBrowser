"""Render snapshots: exact text for key blocks plus invariants over widths, glyphs and colours.

Snapshots are inline strings (no snapshot plugin): the plain-level output is the snapshot,
and ANSI output is checked for its invariants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from rich.cells import cell_len

from src.config import UiSettings
from src.ui.feed import project_event
from src.ui.tui.render import (
    render_banner,
    render_block,
    render_live,
    render_rule,
    render_status,
)
from src.ui.tui.terminal import TerminalCaps
from src.ui.tui.theme import Theme
from src.ui.tui.transcript import (
    ApprovalClosed,
    ApprovalShown,
    Block,
    Transcript,
    UserSubmitted,
    reduce,
)
from src.ui.views import FeedItem, ServerView, SessionView

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SETTINGS = UiSettings()


def caps(width: int, *, level: str = "plain", color: str = "none", unicode: bool = True) -> TerminalCaps:
    return TerminalCaps(
        level=level,  # type: ignore[arg-type]
        color=color,  # type: ignore[arg-type]
        unicode=unicode,
        sync_output=False,
        hyperlinks=False,
        title=False,
        width=width,
        height=24,
        reason="test",
    )


@dataclass(frozen=True)
class Ticket:
    ticket_id: str = "k1"
    task_id: str = "t1"
    tool: str = "browser_click"
    server: str = "playwright"
    domain: str = "www.ozon.ru"
    reason: str = "действие оформляет заказ"
    rule_id: str = "ask-checkout"
    args_preview: str = 'element: "Оформить заказ"  ref: e412'
    allow_session: bool = True
    created_at: datetime = T0
    deadline: datetime = T0 + timedelta(seconds=91)


@dataclass(frozen=True)
class Resolution:
    ticket_id: str = "k1"
    answer: str = "once"
    via: str = "tui"
    resolved_at: datetime = T0 + timedelta(seconds=4.2)


class Stream:
    def __init__(self) -> None:
        self.sequence = 0

    def __call__(self, event_type: str, payload: dict[str, object], at: float = 0.0) -> FeedItem:
        self.sequence += 1
        item = project_event(
            {
                "type": event_type,
                "payload": payload,
                "task_id": "t1",
                "sequence": self.sequence,
                "timestamp": (T0 + timedelta(seconds=at)).isoformat(),
            }
        )
        assert item is not None
        return item


def task_messages() -> list[object]:
    ev = Stream()
    return [
        UserSubmitted("найди на ozon беспроводные наушники до 5000 ₽"),
        ev("goal.started", {"task": "найди"}),
        ev("plan.updated", {"steps": ["Открыть ozon.ru", "Найти товары", "Выбрать 3 лучших"]}),
        ev("turn.started", {"turn": 1, "current_step": 0}),
        ev("action.proposed", {"tool_request": {"name": "browser_navigate", "args": {"url": "https://www.ozon.ru"}}}, 1),
        ev("permission.decided", {"tool": "browser_navigate", "server": "playwright", "decision": "allow"}, 1),
        ev("tool.started", {"tool_request": {"name": "browser_navigate"}}, 1),
        ev(
            "tool.finished",
            {
                "tool_result": {
                    "name": "browser_navigate",
                    "status": "success",
                    "content": "- Page URL: https://www.ozon.ru/\n- Page Title: OZON\n\x1b]0;pwned\x07tail",
                }
            },
            2.2,
        ),
        ev("observation.compiled", {"observation": "x" * 9100, "has_snapshot": True}, 2.3),
        ev("turn.started", {"turn": 2, "current_step": 1}, 3),
        ev("action.proposed", {"tool_request": {"name": "browser_navigate", "args": {"url": "https://example.org"}}}, 4),
        ev("hook.decided", {"hook_id": "url_policy", "decision": "deny", "reason": "домен вне списка"}, 4),
        ev("action.proposed", {"tool_request": {"name": "browser_click", "args": {"element": "Оформить заказ", "ref": "e412"}}}, 5),
        ev("approval.requested", {"tool_request": {"name": "browser_click"}, "reason": "r", "rule_id": "ask-checkout"}, 5),
        ApprovalShown(Ticket()),
        ApprovalClosed(Resolution()),
        ev("tool.started", {"tool_request": {"name": "browser_click"}}, 9),
        ev("tool.finished", {"tool_result": {"name": "browser_click", "status": "error", "error": "Element is not visible"}}, 9.6),
        ev("observation.compiled", {"observation": "o"}, 9.7),
        ev(
            "goal.completed",
            {"task": "найди", "status": "completed", "final_answer": "Нашёл **3 варианта**.\n\n1. JBL — 3 490 ₽", "turns": 6},
            72,
        ),
    ]


def render_task(width: int, terminal: TerminalCaps, settings: UiSettings = SETTINGS) -> str:
    theme = Theme.resolve(settings, terminal)
    transcript = Transcript()
    out: list[str] = []
    for message in task_messages():
        transcript, frozen = reduce(transcript, message)  # type: ignore[arg-type]
        out.extend(
            render_block(b, width=width, caps=terminal, theme=theme, settings=settings) for b in frozen
        )
    return "\n\n".join(out)


EXPECTED_80 = """\
› найди на ozon беспроводные наушники до 5000 ₽

● playwright · browser_navigate  url: https://www.ozon.ru
  ⎿ ✓ 1.2s  https://www.ozon.ru/

● browser_navigate  url: https://example.org
  ⎿ ⊘ hook url_policy: домен вне списка

  ⎿ ✓ разрешено один раз (tui) · 4.2s

● browser_click  element: "Оформить заказ"  ref: e412
  ⎿ ? Нужно подтверждение · правило ask-checkout
  ⎿ ✗ 0.6s  Element is not visible

  План
  ✓ Открыть ozon.ru
  ✓ Найти товары
  ✓ Выбрать 3 лучших

✓ Готово · 6 ходов · 2 вызова · 1:12

  Нашёл 3 варианта.

   1 JBL — 3 490 ₽"""


def test_task_transcript_at_80_columns_plain() -> None:
    assert render_task(80, caps(80)) == EXPECTED_80


def test_task_transcript_at_40_columns_ascii() -> None:
    text = render_task(40, caps(40, unicode=False))

    assert text.splitlines()[:3] == [
        "> найди на ozon беспроводные наушники",
        "  до 5000 ₽",
        "",
    ]
    assert "* playwright · browser_navigate" in text
    assert "  url: https://www.ozon.ru" in text
    assert "  ` ok 1.2s  https://www.ozon.ru/" in text
    assert "ok Готово · 6 ходов · 2 вызова · 1:12" in text


@pytest.mark.parametrize("width", [40, 80, 120])
@pytest.mark.parametrize("unicode", [True, False])
@pytest.mark.parametrize("level, color", [("plain", "none"), ("full", "16"), ("full", "truecolor")])
def test_no_line_reaches_the_last_column_and_no_hostile_escapes(
    width: int, unicode: bool, level: str, color: str
) -> None:
    terminal = caps(width, level=level, color=color, unicode=unicode)
    text = render_task(width, terminal)

    plain = ANSI.sub("", text)
    for line in plain.splitlines():
        assert cell_len(line) <= width - 1, line
    # Page content tried to set the window title (OSC 0); nothing of it may survive.
    assert "\x1b]" not in text and "\x07" not in text
    if level == "plain":
        assert "\x1b" not in text
    else:
        assert ANSI.search(text)
    if color == "16":
        assert "38;2;" not in text and "38;5;" not in text


def test_verbose_call_shows_details_and_a_bounded_preview() -> None:
    ev = Stream()
    content = "\n".join(f"- line {n}" for n in range(10))
    items = (
        ev("action.proposed", {"tool_request": {"name": "browser_click", "args": {"element": "Фильтры", "ref": "e212"}}}),
        ev("permission.decided", {"tool": "browser_click", "server": "playwright", "decision": "allow", "source": "mode"}),
        ev("tool.started", {"tool_request": {"name": "browser_click"}}),
        ev("tool.finished", {"tool_result": {"name": "browser_click", "status": "success", "content": content}}, 0.6),
        ev("observation.compiled", {"observation": "y" * 9100, "has_snapshot": True}, 0.7),
    )
    block = Block("b1", "call", state="frozen", expanded=True, items=items)
    terminal = caps(80)

    text = render_block(block, width=80, caps=terminal, theme=Theme.resolve(SETTINGS, terminal), settings=SETTINGS)

    assert text == "\n".join(
        [
            "● playwright · browser_click",
            '  │ args   {"element": "Фильтры", "ref": "e212"}',
            "  │ доступ allow · mode",
            "  ⎿ ✓ success · 0.6s",
            "    - line 0",
            "    - line 1",
            "    - line 2",
            "    … ещё 7 строк · наблюдение: 9.1k симв. · snapshot сохранён",
        ]
    )


def test_live_zone_card_spinner_and_height_limit() -> None:
    terminal = caps(80)
    theme = Theme.resolve(SETTINGS, terminal)
    ev = Stream()
    transcript = Transcript()
    for message in [
        ev("goal.started", {"task": "t"}),
        ev("turn.started", {"turn": 4, "current_step": 0}),
        ApprovalShown(Ticket()),
    ]:
        transcript, _ = reduce(transcript, message)  # type: ignore[arg-type]

    live = render_live(
        transcript, width=80, caps=terminal, theme=theme, settings=SETTINGS, now=T0 + timedelta(seconds=37), turn_cap=50
    )
    lines = live.splitlines()

    assert lines[0].startswith("▌ Нужно подтверждение · правило ask-checkout")
    assert lines[0].endswith("00:54")
    assert "▌ ❯ 1. Да, один раз" in lines
    assert "▌   2. Да, для browser_click на www.ozon.ru до конца сессии" in lines
    assert lines[-1] == "⠋ Думаю… · ход 4/50 · 00:37 · esc — прервать"

    # The card is never cut, even when it alone is taller than the live zone.
    small = UiSettings(live_max_lines=4)
    clipped = render_live(
        transcript, width=80, caps=terminal, theme=theme, settings=small, now=T0, turn_cap=50
    ).splitlines()
    assert clipped[0].startswith("▌ Нужно подтверждение")
    assert len(clipped) == 11  # 10 card lines + the spinner


def test_the_plan_gives_way_to_an_approval_card() -> None:
    terminal = caps(80)
    theme = Theme.resolve(SETTINGS, terminal)
    ev = Stream()
    transcript = Transcript()
    for message in [
        ev("goal.started", {"task": "t"}),
        ev("plan.updated", {"steps": [f"шаг {n}" for n in range(8)]}),
        ApprovalShown(Ticket()),
    ]:
        transcript, _ = reduce(transcript, message)  # type: ignore[arg-type]

    lines = render_live(
        transcript, width=80, caps=terminal, theme=theme, settings=UiSettings(live_max_lines=14), now=T0
    ).splitlines()

    assert len(lines) == 14
    assert lines[0] == "  … ещё 7 строк"  # plan header + 8 steps = 9 lines, 2 shown
    assert lines[3].startswith("▌ Нужно подтверждение")


def test_the_card_shows_hostile_ticket_text_inert_and_on_one_line() -> None:
    terminal = caps(80, level="full", color="16")
    theme = Theme.resolve(SETTINGS, terminal)
    esc, csi, rlo, newline = chr(0x1B), chr(0x9B), chr(0x202E), chr(10)
    hostile = Ticket(
        reason=f"ok{esc}[8mhidden{newline}second{rlo}evil",
        args_preview='{"a": "' + csi + '2J"}',
    )
    transcript, _ = reduce(Transcript(), ApprovalShown(hostile))

    live = render_live(transcript, width=80, caps=terminal, theme=theme, settings=SETTINGS, now=T0)

    plain = ANSI.sub("", live)
    assert "Причина: okhidden secondevil" in plain
    assert f"{esc}[8m" not in live and rlo not in live and csi not in live
    assert len(plain.splitlines()) == 10


def test_a_card_closed_by_a_cancellation_does_not_claim_a_refusal() -> None:
    terminal = caps(80)
    theme = Theme.resolve(SETTINGS, terminal)
    transcript, _ = reduce(Transcript(), ApprovalShown(Ticket()))
    _, frozen = reduce(transcript, ApprovalClosed(Resolution(answer="deny", via="cancelled")))
    unanswered = Block("b9", "approval", state="frozen", ticket=Ticket())

    cancelled = render_block(frozen[0], width=80, caps=terminal, theme=theme, settings=SETTINGS)
    no_resolution = render_block(unanswered, width=80, caps=terminal, theme=theme, settings=SETTINGS)

    assert cancelled.startswith("  ⎿ ⊘ задача прервана до ответа")
    assert no_resolution == "  ⎿ ⊘ задача прервана до ответа"
    assert "отклонено" not in cancelled


def session_view() -> SessionView:
    return SessionView(
        session_id="9c441f7e0000",
        version="0.1.0",
        model="gemma4:31b-cloud",
        provider="ollama",
        workspace="C:/WebApps/AutoBrowser",
        servers=(ServerView("playwright", "ready", tool_count=22), ServerView("memory", "ready")),
        permission_mode="default",
        turn_cap=50,
    )


def test_banner_and_rule() -> None:
    terminal = caps(80)
    theme = Theme.resolve(SETTINGS, terminal)

    banner = render_banner(session_view(), width=80, caps=terminal, theme=theme, settings=SETTINGS)

    assert banner.splitlines()[:5] == [
        "  ╭───────╮  AutoBrowser 0.1.0",
        "  │ ● ● ● │  модель     gemma4:31b-cloud · ollama",
        "  ├───────┤  сессия     9c441f7e · C:/WebApps/AutoBrowser",
        "  │  ›_   │  браузер    ● playwright  ● memory",
        "  ╰───────╯  разрешения default · хуки выкл. · память выкл.",
    ]
    narrow = render_banner(session_view(), width=40, caps=caps(40), theme=theme, settings=SETTINGS)
    assert "╭" not in narrow and narrow.splitlines()[0] == "  AutoBrowser 0.1.0"
    assert render_rule(width=80, caps=terminal, theme=theme, settings=SETTINGS) == "─" * 79


def test_status_line_drops_segments_by_priority() -> None:
    terminal = caps(120)
    theme = Theme.resolve(SETTINGS, terminal)
    ev = Stream()
    transcript, _ = reduce(Transcript(), ev("goal.started", {"task": "t"}))
    transcript, _ = reduce(transcript, ev("turn.started", {"turn": 4}))

    def status(width: int) -> str:
        return render_status(
            session_view(), transcript, width=width, caps=caps(width), theme=theme, settings=SETTINGS,
            now=T0 + timedelta(seconds=37),
        )

    wide = status(120)
    assert wide.startswith("  gemma4:31b-cloud · 9c44 · ● playwright  ● memory · default · ход 4/50 · 00:37")
    assert wide.endswith("? помощь")
    # Help, then permissions and session go; then the model is shortened; the turn stays.
    assert status(80) == "  gemma4:31b-cloud · 9c44 · ● playwright  ● memory · default · ход 4/50 · 00:37"
    assert status(70) == "  gemma4:31b-cloud · 9c44 · ● playwright  ● memory · ход 4/50 · 00:37"
    assert status(64) == "  gemma4:31b-cloud · ● playwright  ● memory · ход 4/50 · 00:37"
    assert status(55) == "  gemma4:… · ● playwright  ● memory · ход 4/50 · 00:37"
    assert status(30) == "  gemma4:… · ход 4/50 · 00:37"
    idle = render_status(
        session_view(), Transcript(), width=120, caps=terminal, theme=theme, settings=SETTINGS, now=T0
    )
    assert "готов" in idle


def test_english_locale() -> None:
    settings = UiSettings(locale="en")
    text = render_task(80, caps(80), settings)

    assert "✓ Done · 6 turns · 2 calls · 1:12" in text
    assert "  Plan" in text


def test_browser_state_of_the_controller_is_localized() -> None:
    terminal = caps(80)
    theme = Theme.resolve(SETTINGS, terminal)
    view = SessionView(session_id="abcd1234", model="m", browser="disabled", permission_mode="default")

    status = render_status(view, Transcript(), width=80, caps=terminal, theme=theme, settings=SETTINGS, now=T0)
    banner = render_banner(view, width=80, caps=terminal, theme=theme, settings=SETTINGS)

    assert "браузер выкл." in status and "disabled" not in status
    assert "браузер    выкл." in banner
    odd = SessionView(session_id="abcd1234", model="m", browser="connecting")
    assert "connecting" in render_status(odd, Transcript(), width=80, caps=terminal, theme=theme, settings=SETTINGS, now=T0)


def test_the_resume_list_scrolls_around_the_selection_and_ages_read_naturally() -> None:
    from src.ui.tui.commands import resume_picker
    from src.ui.tui.strings import Strings
    from src.ui.tui.transcript import KeyAction, PickerShown
    from src.ui.views import StoredSessionView

    sessions = [
        StoredSessionView(f"{n:04d}abcdef00", T0 - timedelta(minutes=n * 90), n, f"задача {n}")
        for n in range(1, 21)
    ]
    picker = resume_picker(sessions, Strings("ru"), now=T0)
    assert picker.options[0].label == "0001abcd · 1 ч назад · 1 задача"
    assert picker.options[19].label == "0020abcd · 1 дн назад · 20 задач"
    assert picker.options[0].detail == "задача 1"

    terminal = caps(80)
    theme = Theme.resolve(SETTINGS, terminal)
    transcript, _ = reduce(Transcript(), picker)
    for _ in range(15):
        transcript, _ = reduce(transcript, KeyAction("picker_down"))
    lines = render_live(transcript, width=80, caps=terminal, theme=theme, settings=SETTINGS, now=T0).splitlines()

    assert lines[0] == "▌ Продолжить сессию"
    assert lines[-1].startswith("▌ ↑↓ выбрать · enter — открыть")
    assert any(line.startswith("▌ ❯ 16. 0016abcd") for line in lines)
    assert len(lines) <= SETTINGS.live_max_lines + 2  # window + the "…" markers
    assert not any("1. 0001abcd" in line for line in lines)  # scrolled past the top
