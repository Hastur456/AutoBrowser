"""Command hooks: an external process as a lifecycle hook handler.

The protocol follows Claude Code and Codex command hooks. The process is started through
the shell in the repository root and gets:

* **stdin** — the :class:`~src.contracts.HookEvent` as one JSON object
  (``dataclasses.asdict``, ASCII-escaped), then EOF;
* **environment** — the parent environment plus ``AUTOBROWSER_PROJECT_DIR`` (the
  repository root) and ``AUTOBROWSER_HOOK_EVENT`` (the event name).

It answers with its exit code:

* ``0`` — success. Empty stdout is "no opinion". Stdout that is a JSON object is read as a
  :class:`~src.contracts.HookResult` (``decision``, ``reason``, ``updated_input``,
  ``updated_output``, ``additional_context``, ``user_message``; ``"block"`` is accepted
  as an alias of ``"deny"``). Plain text becomes ``additional_context`` on ``goal_start``
  and is ignored on every other event.
* ``2`` — blocking. Stderr is the reason: a ``deny`` on decision events; on
  ``post_tool_use*`` (the tool already ran) it is fed to the model as
  ``additional_context`` instead.
* anything else — the handler failed, exactly like a Python handler raising: the
  per-event failure decision (or the spec's ``fail_closed``) applies.

On timeout the whole process tree is killed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from src.config import ROOT_PATH
from src.contracts import HookEvent, HookEventName, HookResult

#: Exit code that blocks, as in Claude Code and Codex.
BLOCKING_EXIT_CODE = 2

_POST_TOOL_EVENTS = frozenset({"post_tool_use", "post_tool_use_failure"})
_DECISION_ALIASES: dict[Any, str | None] = {
    None: None,
    "allow": "allow",
    "ask": "ask",
    "deny": "deny",
    "block": "deny",
}
_STRING_FIELDS = ("reason", "additional_context", "user_message")
_STDERR_TAIL_CHARS = 300


class CommandHook:
    """Async hook handler that runs ``command`` once per event."""

    def __init__(self, command: str, *, cwd: Path = ROOT_PATH) -> None:
        self.command = command
        self.cwd = cwd

    async def __call__(self, event: HookEvent) -> HookResult | None:
        payload = json.dumps(asdict(event)).encode("ascii")
        env = {
            **os.environ,
            "AUTOBROWSER_PROJECT_DIR": str(self.cwd),
            "AUTOBROWSER_HOOK_EVENT": event.name,
        }
        process = await asyncio.create_subprocess_shell(
            self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=env,
            start_new_session=sys.platform != "win32",
        )
        try:
            stdout, stderr = await process.communicate(payload)
        except BaseException:
            _kill_tree(process)
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(process.wait(), timeout=2.0)
            raise
        return parse_command_output(
            event.name,
            process.returncode if process.returncode is not None else -1,
            _decode(stdout),
            _decode(stderr),
        )

    def __repr__(self) -> str:
        return f"CommandHook({self.command!r})"


def parse_command_output(
    event_name: HookEventName,
    returncode: int,
    stdout: str,
    stderr: str,
) -> HookResult | None:
    """Translate one finished command run into a :class:`HookResult` (see the module doc).

    Raises ``RuntimeError``/``ValueError``/``TypeError`` for a failed run or malformed
    output, so the engine records the error and applies the per-event failure decision.
    """

    if returncode == BLOCKING_EXIT_CODE:
        reason = stderr.strip() or "Blocked by a hook command (exit code 2)."
        if event_name in _POST_TOOL_EVENTS:
            return HookResult(additional_context=reason)
        return HookResult(decision="deny", reason=reason)
    if returncode != 0:
        tail = stderr.strip()[-_STDERR_TAIL_CHARS:]
        raise RuntimeError(f"exit code {returncode}" + (f": {tail}" if tail else ""))

    text = stdout.strip()
    if not text:
        return None
    if text.startswith("{"):
        return _result_from_json(text)
    if event_name == "goal_start":
        return HookResult(additional_context=text)
    return None


def _result_from_json(text: str) -> HookResult:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"stdout is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TypeError("stdout JSON must be an object.")

    unknown = sorted(set(data) - {"decision", "updated_input", "updated_output", *_STRING_FIELDS})
    if unknown:
        raise ValueError(f"unknown output keys: {', '.join(unknown)}")
    decision = data.get("decision")
    if not isinstance(decision, str | None) or decision not in _DECISION_ALIASES:
        raise ValueError(f"unknown decision {decision!r}")
    for key in _STRING_FIELDS:
        if not isinstance(data.get(key, ""), str):
            raise TypeError(f"{key!r} must be a string.")
    updated_input = data.get("updated_input")
    if updated_input is not None and not isinstance(updated_input, dict):
        raise TypeError("'updated_input' must be an object.")
    updated_output = data.get("updated_output")
    if updated_output is not None and not isinstance(updated_output, str):
        raise TypeError("'updated_output' must be a string.")

    return HookResult(
        decision=_DECISION_ALIASES[decision],  # type: ignore[arg-type]
        reason=data.get("reason", ""),
        updated_input=updated_input,
        updated_output=updated_output,
        additional_context=data.get("additional_context", ""),
        user_message=data.get("user_message", ""),
    )


def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """Kill the shell and everything it started (a timed-out hook must not linger)."""

    if process.returncode is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        process.kill()


__all__ = ["BLOCKING_EXIT_CODE", "CommandHook", "parse_command_output"]
