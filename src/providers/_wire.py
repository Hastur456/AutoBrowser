"""Wire helpers shared by the provider adapters.

Both protocols use the same OpenAI-style tool envelope and the same forgiving reading of
tool-call arguments, so they live here once. Nothing in this module talks to a network or
reads settings.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from src.contracts import ToolDef
from src.llm import ModelProviderError


def tool_to_wire(tool_def: ToolDef) -> dict[str, Any]:
    """Serialize one ``ToolDef`` into the ``{"type": "function", ...}`` tool envelope.

    ``strict`` is never set: MCP tool schemas are not required to satisfy strict mode.
    """

    return {
        "type": "function",
        "function": {
            "name": tool_def.name,
            "description": tool_def.description,
            "parameters": tool_def.input_schema,
        },
    }


def arguments_to_dict(arguments: Any) -> dict[str, Any]:
    """Read tool-call arguments given as an object or a JSON string.

    Invalid JSON or a non-object becomes ``{}`` on purpose: the tool then rejects the
    empty arguments and the model reads that error and fixes its call next turn, where
    an exception here would fail the whole goal over one malformed reply.
    """

    if isinstance(arguments, dict):
        return dict(arguments)
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def new_call_id() -> str:
    """A fresh tool-call id for replies that carry none (or a duplicate one)."""

    return f"call_{uuid4().hex}"


def preview(text: Any, limit: int) -> str:
    """One-line, at most ``limit`` characters of ``text`` for an error message."""

    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 0)] + "…"


def optional_int(value: Any) -> int | None:
    """``value`` as an ``int`` when it is a real number, else ``None``."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def status_error(
    provider: str, status_code: int, detail: Any, limit: int
) -> ModelProviderError:
    """A :class:`ModelProviderError` for an HTTP error status.

    ``detail`` is the provider's own error text (the ``error.message`` of the body,
    never a header). Authentication failures quote nothing: some servers echo the
    rejected key back.
    """

    if status_code in (401, 403):
        message = f"{provider} {status_code}: authentication failed"
    else:
        label = {
            402: "payment required / insufficient balance",
            404: "model or endpoint not found",
        }.get(status_code)
        quoted = preview(detail, limit)
        parts = [part for part in (label, quoted) if part]
        message = f"{provider} {status_code}: " + (": ".join(parts) or "request failed")
    return ModelProviderError(
        message,
        provider=provider,
        status_code=status_code,
        retryable=status_code in (408, 409, 429) or status_code >= 500,
    )


def connection_error(provider: str, exc: BaseException) -> ModelProviderError:
    """A retryable :class:`ModelProviderError` for a failed connection (DNS, TLS, refused).

    Names the exception types only (the SDK wraps the real cause), never their text.
    """

    cause = exc.__cause__ or exc.__context__
    kind = type(exc).__name__ + (f" <- {type(cause).__name__}" if cause is not None else "")
    return ModelProviderError(
        f"{provider}: connection failed ({kind})",
        provider=provider,
        retryable=True,
    )


def timeout_error(provider: str, seconds: float | None) -> ModelProviderError:
    """A retryable :class:`ModelProviderError` for a call that ran out of time."""

    after = f" after {seconds:g} s" if seconds is not None else ""
    return ModelProviderError(f"{provider}: timed out{after}", provider=provider, retryable=True)


def empty_response_error(provider: str) -> ModelProviderError:
    """A :class:`ModelProviderError` for a reply with no usable choice."""

    return ModelProviderError(f"{provider}: empty response", provider=provider)


__all__ = [
    "arguments_to_dict",
    "connection_error",
    "empty_response_error",
    "new_call_id",
    "optional_int",
    "preview",
    "status_error",
    "timeout_error",
    "tool_to_wire",
]
