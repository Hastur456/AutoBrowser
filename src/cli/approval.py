"""Interactive approval of tool calls the PermissionEngine asks about.

The agent task runs on the CLI's background event loop while the terminal belongs to another
thread (the cmd2 prompt, or the main thread blocked on the startup task). So the callback
does not read stdin itself: it posts a pending question and waits for whichever thread owns
the terminal to :meth:`ApprovalPrompt.answer` it — cmd2 routes the next input line here, the
blocking startup run calls :meth:`ApprovalPrompt.serve`.

The wait is bounded (``timeout_seconds``, below ``loop.progress_timeout_seconds``) because the
goal watchdog sees no events while a human thinks; no answer in time is a ``deny``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, field

from src.config import get_settings
from src.contracts import ApprovalAnswer, PermissionVerdict, ToolRequest

_ONCE = {"y", "yes", "once", "o", "д", "да"}
_SESSION = {"s", "session", "с", "сессия"}
_DENY = {"n", "no", "deny", "н", "нет"}
_ARGS_PREVIEW_CHARS = 300


@dataclass
class _Pending:
    question: str
    allow_session: bool
    future: Future[ApprovalAnswer] = field(default_factory=Future)


def approval_timeout_seconds() -> float:
    """How long a human may think: well inside the goal watchdog's progress timeout."""

    return get_settings().loop.progress_timeout_seconds * 0.8


def _args_preview(request: ToolRequest) -> str:
    try:
        text = json.dumps(request.get("args") or {}, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(request.get("args"))
    if len(text) > _ARGS_PREVIEW_CHARS:
        text = text[: _ARGS_PREVIEW_CHARS - 3] + "..."
    return text


def approval_question(request: ToolRequest, reason: str, verdict: PermissionVerdict) -> str:
    """Terminal text of one approval question (arguments are shown, never persisted)."""

    tool = str(request.get("name") or "the tool")
    domain = verdict.grant_key[2] if verdict.grant_key else ""
    where = f" on {domain}" if domain else ""
    choices = ["[y] once"]
    if verdict.grant_key is not None and not verdict.always_ask:
        choices.append(f"[s] session for {tool}{where}")
    choices.append("[n] deny")
    rule = f" (rule {verdict.rule_id})" if verdict.rule_id else ""
    return (
        f"Approval needed{rule}: {tool}{where}\n"
        f"  {reason}\n"
        f"  args: {_args_preview(request)}\n"
        f"  {'   '.join(choices)}"
    )


class ApprovalPrompt:
    """:data:`~src.agent_loop.execution.loop.HumanInputCallback` answered from the terminal."""

    def __init__(
        self,
        *,
        output: Callable[[str], None] = print,
        timeout_seconds: float | None = None,
    ) -> None:
        self._output = output
        self._timeout = approval_timeout_seconds() if timeout_seconds is None else timeout_seconds
        self._lock = threading.Lock()
        self._pending: _Pending | None = None

    @property
    def pending(self) -> bool:
        """A question is waiting for an answer."""

        with self._lock:
            return self._pending is not None and not self._pending.future.done()

    async def __call__(
        self,
        request: ToolRequest,
        reason: str,
        verdict: PermissionVerdict,
    ) -> ApprovalAnswer:
        pending = _Pending(
            question=approval_question(request, reason, verdict),
            allow_session=verdict.grant_key is not None and not verdict.always_ask,
        )
        with self._lock:
            self._pending = pending
        self._output(pending.question)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(pending.future), self._timeout)
        except TimeoutError:
            self._output(f"No answer in {self._timeout:g}s: denied.")
            return "deny"
        finally:
            with self._lock:
                if self._pending is pending:
                    self._pending = None

    def answer(self, text: str) -> bool:
        """Answer the pending question; ``False`` (and a hint) when the text is not an answer."""

        with self._lock:
            pending = self._pending
        if pending is None or pending.future.done():
            self._output("No approval is pending.")
            return False
        choice = str(text or "").strip().lower()
        answer: ApprovalAnswer
        if choice in _ONCE:
            answer = "once"
        elif choice in _SESSION and pending.allow_session:
            answer = "session"
        elif choice in _DENY:
            answer = "deny"
        else:
            options = "y / s / n" if pending.allow_session else "y / n"
            self._output(f"Answer {options}.")
            return False
        with self._lock:
            if self._pending is pending and not pending.future.done():
                pending.future.set_result(answer)
        return True

    def serve(self, read_line: Callable[[str], str] = input) -> None:
        """Ask on the calling thread until the pending question is answered (if any)."""

        while self.pending:
            try:
                text = read_line("approve> ")
            except (EOFError, KeyboardInterrupt):
                text = "n"
            self.answer(text)


__all__ = ["ApprovalPrompt", "approval_question", "approval_timeout_seconds"]
