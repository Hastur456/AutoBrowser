"""Interactive approval of tool calls the PermissionEngine asks about, in the cmd2 shell.

The approvals themselves live in :class:`~src.harness.approvals.ApprovalBroker` (tickets, the
bounded wait, ``approval.answered``); :class:`ApprovalPrompt` is the terminal adapter over it.
The agent task runs on the CLI's background event loop while the terminal belongs to another
thread (the cmd2 prompt, or the main thread blocked on the startup task). So the prompt does
not read stdin itself: it prints each new ticket and waits for whichever thread owns the
terminal to :meth:`ApprovalPrompt.answer` it — cmd2 routes the next input line here, the
blocking startup run calls :meth:`ApprovalPrompt.serve`.

The wait is bounded (``permissions.approval_timeout_ratio`` of
``loop.progress_timeout_seconds``) because the goal watchdog sees no events while a human
thinks; no answer in time is a ``deny``.
"""

from __future__ import annotations

from collections.abc import Callable

from src.agent_loop.goals import GoalLimits
from src.config import get_settings
from src.contracts import (
    ApprovalAnswer,
    ApprovalResolution,
    ApprovalTicket,
    ApprovalVia,
    PermissionVerdict,
    ToolRequest,
)
from src.harness.approvals import ApprovalBroker

_ONCE = {"y", "yes", "once", "o", "д", "да"}
_SESSION = {"s", "session", "с", "сессия"}
_DENY = {"n", "no", "deny", "н", "нет"}


def _question(
    *,
    tool: str,
    domain: str,
    reason: str,
    args_preview: str,
    allow_session: bool,
    rule_id: str,
    source: str,
) -> str:
    tool = tool or "the tool"
    where = f" on {domain}" if domain else ""
    choices = ["[y] once"]
    if allow_session:
        choices.append(f"[s] session for {tool}{where}")
    choices.append("[n] deny")
    if rule_id:
        rule = f" (rule {rule_id})"
    elif source in ("model", "classifier"):
        rule = f" (asked by the {source})"
    else:
        rule = ""
    return (
        f"Approval needed{rule}: {tool}{where}\n"
        f"  {reason}\n"
        f"  args: {args_preview}\n"
        f"  {'   '.join(choices)}"
    )


def ticket_question(ticket: ApprovalTicket) -> str:
    """Terminal text of one :class:`ApprovalTicket` (its arguments are already redacted)."""

    return _question(
        tool=ticket.tool,
        domain=ticket.domain,
        reason=ticket.reason,
        args_preview=ticket.args_preview,
        allow_session=ticket.allow_session,
        rule_id=ticket.rule_id,
        source=ticket.source,
    )


class ApprovalPrompt:
    """Terminal adapter over an :class:`ApprovalBroker`; itself a ``HumanInputCallback``.

    Without an explicit ``broker`` it builds one from the settings (``timeout_seconds``
    overrides the derived wait). Calling the prompt delegates to the broker, so it can be
    handed to the session as its human-input callback.
    """

    def __init__(
        self,
        *,
        output: Callable[[str], None] = print,
        timeout_seconds: float | None = None,
        broker: ApprovalBroker | None = None,
        via: ApprovalVia = "classic",
    ) -> None:
        if broker is None:
            settings = get_settings()
            broker = ApprovalBroker(
                settings.permissions,
                # The goal watchdog's own bound: min(progress, task timeout).
                progress_timeout_seconds=GoalLimits.from_settings(
                    settings.loop
                ).progress_timeout_seconds,
                timeout_seconds=timeout_seconds,
            )
        self.broker = broker
        self._output = output
        self._via: ApprovalVia = via
        self._unsubscribe = broker.subscribe(self._on_item)

    @property
    def pending(self) -> bool:
        """A question is waiting for an answer."""

        return bool(self.broker.pending())

    async def __call__(
        self,
        request: ToolRequest,
        reason: str,
        verdict: PermissionVerdict,
    ) -> ApprovalAnswer:
        return await self.broker(request, reason, verdict)

    def answer(self, text: str) -> bool:
        """Answer the oldest pending question; ``False`` (and a hint) when the text is not one."""

        pending = self.broker.pending()
        if not pending:
            self._output("No approval is pending.")
            return False
        ticket = pending[0]
        choice = str(text or "").strip().lower()
        answer: ApprovalAnswer
        if choice in _ONCE:
            answer = "once"
        elif choice in _SESSION and ticket.allow_session:
            answer = "session"
        elif choice in _DENY:
            answer = "deny"
        else:
            options = "y / s / n" if ticket.allow_session else "y / n"
            self._output(f"Answer {options}.")
            return False
        return self.broker.answer(ticket.ticket_id, answer, via=self._via)

    def serve(self, read_line: Callable[[str], str] = input) -> None:
        """Ask on the calling thread until the pending question is answered (if any)."""

        while self.pending:
            try:
                text = read_line("approve> ")
            except (EOFError, KeyboardInterrupt):
                text = "n"
            self.answer(text)

    def close(self) -> None:
        """Stop printing the broker's tickets."""

        self._unsubscribe()

    def _on_item(self, item: ApprovalTicket | ApprovalResolution) -> None:
        if isinstance(item, ApprovalTicket):
            self._output(ticket_question(item))
        elif item.via == "timeout":
            self._output(f"No answer in {self.broker.timeout_seconds:g}s: denied.")


__all__ = ["ApprovalPrompt", "ticket_question"]
