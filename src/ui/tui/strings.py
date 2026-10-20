"""Interface strings of the terminal UI per ``ui.locale``."""

from __future__ import annotations

from typing import Literal

Locale = Literal["ru", "en"]

_STRINGS: dict[str, dict[str, str]] = {
    "ru": {
        "plan": "План",
        "thinking": "Думаю…",
        "planning": "Планирую…",
        "judging": "Оцениваю действие…",
        "running": "Выполняю…",
        "turn": "ход",
        "calls": "вызовов",
        "esc_cancel": "esc — прервать",
        "done": "Готово",
        "blocked": "Остановлено",
        "cancelled": "Прервано",
        "failed": "Ошибка",
        "turns": "ходов",
        "more_lines": "ещё {n} строк",
        "args": "args",
        "access": "доступ",
        "observation": "наблюдение: {n} симв.",
        "snapshot": "snapshot сохранён",
        "approval_title": "Нужно подтверждение",
        "rule": "правило",
        "on_domain": "на",
        "reason": "Причина",
        "approve_once": "Да, один раз",
        "approve_session": "Да, для {tool} на {domain} до конца сессии",
        "approve_session_any": "Да, для {tool} до конца сессии",
        "deny": "Нет — задача будет остановлена",
        "approval_hint": "↑↓ выбрать · enter — подтвердить · 1–3 — сразу · esc — нет",
        "answered_once": "разрешено один раз",
        "answered_session": "разрешено до конца сессии",
        "answered_deny": "отклонено",
        "answered_timeout": "время ожидания истекло — отклонено",
        "answered_cancelled": "задача прервана до ответа",
        "banner_hint": "Опишите задачу словами. /help — команды · Esc — прервать · Ctrl+O — подробности",
        "model": "модель",
        "session": "сессия",
        "browser": "браузер",
        "permissions": "разрешения",
        "hooks_on": "хуки вкл.",
        "hooks_off": "хуки выкл.",
        "memory_on": "память вкл.",
        "memory_off": "память выкл.",
        "no_browser": "браузер выкл.",
        "ready": "готов",
        "help": "? помощь",
        "gap": "пропущено событий: {n}",
        "resumed": "Продолжена сессия {session} · задач: {count} · /tasks — история",
        "resume_title": "Продолжить сессию",
        "resume_none": "нет других сессий, которые можно продолжить",
        "picker_hint": "↑↓ выбрать · enter — открыть · 1–9 — сразу · esc — отмена",
        "ago_now": "только что",
        "ago_minutes": "{n} мин назад",
        "ago_hours": "{n} ч назад",
        "ago_days": "{n} дн назад",
        "hook": "hook",
        "permission": "разрешение",
        "guard": "guard",
    },
    "en": {
        "plan": "Plan",
        "thinking": "Thinking…",
        "planning": "Planning…",
        "judging": "Judging the action…",
        "running": "Running…",
        "turn": "turn",
        "calls": "calls",
        "esc_cancel": "esc to interrupt",
        "done": "Done",
        "blocked": "Blocked",
        "cancelled": "Interrupted",
        "failed": "Failed",
        "turns": "turns",
        "more_lines": "{n} more lines",
        "args": "args",
        "access": "access",
        "observation": "observation: {n} chars",
        "snapshot": "snapshot kept",
        "approval_title": "Approval needed",
        "rule": "rule",
        "on_domain": "on",
        "reason": "Reason",
        "approve_once": "Yes, once",
        "approve_session": "Yes, for {tool} on {domain} for this session",
        "approve_session_any": "Yes, for {tool} for this session",
        "deny": "No — the task will stop",
        "approval_hint": "↑↓ select · enter to confirm · 1–3 directly · esc for no",
        "answered_once": "allowed once",
        "answered_session": "allowed for this session",
        "answered_deny": "denied",
        "answered_timeout": "timed out — denied",
        "answered_cancelled": "the task stopped before an answer",
        "banner_hint": "Describe a task in words. /help commands · Esc interrupt · Ctrl+O details",
        "model": "model",
        "session": "session",
        "browser": "browser",
        "permissions": "permissions",
        "hooks_on": "hooks on",
        "hooks_off": "hooks off",
        "memory_on": "memory on",
        "memory_off": "memory off",
        "no_browser": "browser off",
        "ready": "ready",
        "help": "? help",
        "gap": "{n} events missed",
        "resumed": "Resumed session {session} · {count} tasks · /tasks for the history",
        "resume_title": "Resume a session",
        "resume_none": "no other session to resume",
        "picker_hint": "↑↓ select · enter to open · 1–9 directly · esc to cancel",
        "ago_now": "just now",
        "ago_minutes": "{n} min ago",
        "ago_hours": "{n} h ago",
        "ago_days": "{n} d ago",
        "hook": "hook",
        "permission": "permission",
        "guard": "guard",
    },
}


#: ``SessionView.browser`` states of the controller, shown in the locale; other text as is.
_BROWSER_STATES: dict[str, dict[str, str]] = {
    "ru": {
        "disabled": "выкл.",
        "not started": "не запущен",
        "no browser server": "нет браузерного сервера",
        "unknown": "неизвестно",
        "on": "вкл.",
    },
    "en": {"disabled": "off", "not started": "not started"},
}

#: Plural forms per locale: ru ``(1, 2-4, 5+)``, en ``(1, other)``.
_PLURALS: dict[str, dict[str, tuple[str, ...]]] = {
    "ru": {
        "turns": ("ход", "хода", "ходов"),
        "calls": ("вызов", "вызова", "вызовов"),
        "tasks": ("задача", "задачи", "задач"),
    },
    "en": {"turns": ("turn", "turns"), "calls": ("call", "calls"), "tasks": ("task", "tasks")},
}


class Strings:
    """Lookup of interface strings; ``ascii`` swaps the typographic ellipsis and dashes."""

    def __init__(self, locale: Locale, *, ascii_only: bool = False) -> None:
        self._table = _STRINGS[locale]
        self._plurals = _PLURALS[locale]
        self._browser_states = _BROWSER_STATES[locale]
        self._ascii = ascii_only

    def __call__(self, key: str, **values: object) -> str:
        text = self._table[key].format(**values) if values else self._table[key]
        if self._ascii:
            text = text.replace("…", "...").replace("—", "-").replace("–", "-")
            text = text.replace("↑↓", "up/down")
        return text

    def browser_state(self, state: str) -> str:
        """The controller's browser state in the locale; unknown states unchanged."""

        return self._browser_states.get(state, state)

    def ago(self, seconds: float) -> str:
        """``5 мин назад`` / ``3 h ago`` for an age in seconds."""

        minutes = int(max(seconds, 0) // 60)
        if minutes < 1:
            return self("ago_now")
        if minutes < 60:
            return self("ago_minutes", n=minutes)
        if minutes < 24 * 60:
            return self("ago_hours", n=minutes // 60)
        return self("ago_days", n=minutes // (24 * 60))

    def count(self, key: str, n: int) -> str:
        """``n`` with the plural form of ``key`` (``"4 вызова"``, ``"1 turn"``)."""

        forms = self._plurals[key]
        if len(forms) == 2:
            return f"{n} {forms[0] if n == 1 else forms[1]}"
        tail = n % 100
        if 11 <= tail <= 14:
            form = forms[2]
        elif n % 10 == 1:
            form = forms[0]
        elif 2 <= n % 10 <= 4:
            form = forms[1]
        else:
            form = forms[2]
        return f"{n} {form}"


__all__ = ["Locale", "Strings"]
