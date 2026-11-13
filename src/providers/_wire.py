"""Wire helpers shared by the provider adapters.

Both protocols use the same OpenAI-style tool envelope and the same forgiving reading of
tool-call arguments, so they live here once. Nothing in this module talks to a network or
reads settings.
"""

from __future__ import annotations

import errno
import json
import re
import socket
import ssl
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from src.contracts import ToolDef
from src.llm import ConnectCause, ModelProviderError, ProviderErrorKind, TimeoutPhase

#: Statuses a server sends when it refused the request before processing it.
_UNAVAILABLE_STATUSES = frozenset({408, 409, 503, 529})

#: Windows Sockets codes of the same failures (``WSAECONNREFUSED`` and friends).
_WSAECONNREFUSED, _WSAECONNRESET, _WSAENETUNREACH, _WSAEHOSTUNREACH = 10061, 10054, 10051, 10065

#: Connection causes by ``errno``, for an ``OSError`` that is not a typed subclass.
_ERRNO_CAUSES: dict[int, ConnectCause] = {
    errno.ECONNREFUSED: "refused",
    _WSAECONNREFUSED: "refused",
    errno.ECONNRESET: "reset",
    _WSAECONNRESET: "reset",
    errno.ENETUNREACH: "unreachable",
    errno.EHOSTUNREACH: "unreachable",
    _WSAENETUNREACH: "unreachable",
    _WSAEHOSTUNREACH: "unreachable",
}

#: Response headers that carry the provider's request id, in order of preference.
_REQUEST_ID_HEADERS = ("x-request-id", "request-id")

#: What a request id looks like; anything else (a long or odd value) is not kept.
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:\-]{1,128}")


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
    provider: str,
    status_code: int,
    detail: Any,
    limit: int,
    *,
    headers: Mapping[str, str] | None = None,
) -> ModelProviderError:
    """A :class:`ModelProviderError` for an HTTP error status.

    ``detail`` is the provider's own error text (the ``error.message`` of the body,
    never a header). Authentication failures quote nothing: some servers echo the
    rejected key back. Of the response ``headers`` only ``Retry-After`` and the request id
    are read.
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
        kind=status_kind(status_code),
        retry_after_seconds=retry_after_seconds(headers),
        request_id=request_id(headers),
    )


def status_kind(status_code: int) -> ProviderErrorKind:
    """The error kind of an HTTP error status."""

    if status_code in (401, 403):
        return "auth"
    if status_code == 402:
        return "payment"
    if status_code == 404:
        return "not_found"
    if status_code == 429:
        return "rate_limited"
    if status_code in _UNAVAILABLE_STATUSES:
        return "unavailable"
    if status_code >= 500:
        return "server_error"
    return "bad_request"


def connection_error(provider: str, exc: BaseException) -> ModelProviderError:
    """A :class:`ModelProviderError` for a failed connection (DNS, TLS, refused, ...).

    Names the exception types and the ``cause`` read from them, never their text (the SDK
    wraps the real cause, and a message may quote a URL or a header).
    """

    cause = connect_cause(exc)
    chained = exc.__cause__ or exc.__context__
    kind = type(exc).__name__ + (f" <- {type(chained).__name__}" if chained is not None else "")
    reason = kind if cause == "unknown" else f"{cause}; {kind}"
    return ModelProviderError(
        f"{provider}: connection failed ({reason})",
        provider=provider,
        kind="connect",
        cause=cause,
    )


def interrupted_error(provider: str, exc: BaseException) -> ModelProviderError:
    """A :class:`ModelProviderError` for a connection that dropped after the request went
    out: the model may have generated (``retry_safety`` ``ambiguous``)."""

    chained = exc.__cause__ or exc.__context__
    kind = type(exc).__name__ + (f" <- {type(chained).__name__}" if chained is not None else "")
    return ModelProviderError(
        f"{provider}: response interrupted ({kind})", provider=provider, kind="interrupted"
    )


def timeout_error(
    provider: str, seconds: float | None, *, phase: TimeoutPhase | None = None
) -> ModelProviderError:
    """A :class:`ModelProviderError` for a call that ran out of time; ``seconds`` is the limit
    of the ``phase`` that expired (all of them when the phase is unknown)."""

    after = f" after {seconds:g} s" if seconds is not None else ""
    what = f"{phase} timed out" if phase is not None else "timed out"
    return ModelProviderError(
        f"{provider}: {what}{after}", provider=provider, kind="timeout", phase=phase
    )


def empty_response_error(provider: str) -> ModelProviderError:
    """A :class:`ModelProviderError` for a reply with no usable choice."""

    return ModelProviderError(
        f"{provider}: empty response", provider=provider, kind="empty_response"
    )


def is_interrupted(exc: BaseException) -> bool:
    """Did the connection drop after the request was sent (a broken or cut response)?"""

    import httpx

    return any(
        isinstance(item, (httpx.RemoteProtocolError, httpx.ReadError)) for item in _chain(exc)
    )


def timeout_phase(exc: BaseException) -> TimeoutPhase | None:
    """The HTTP phase of an ``httpx`` timeout found in the exception chain of ``exc``."""

    import httpx

    phases: tuple[tuple[type[BaseException], TimeoutPhase], ...] = (
        (httpx.ConnectTimeout, "connect"),
        (httpx.ReadTimeout, "read"),
        (httpx.WriteTimeout, "write"),
        (httpx.PoolTimeout, "pool"),
    )
    for item in _chain(exc):
        for error_type, phase in phases:
            if isinstance(item, error_type):
                return phase
    return None


def connect_cause(exc: BaseException) -> ConnectCause:
    """Why a connection failed, from the types and ``errno`` of the exception chain."""

    import httpx

    for item in _chain(exc):
        if isinstance(item, socket.gaierror):
            return "dns"
        if isinstance(item, ssl.SSLError):
            return "tls"
        if isinstance(item, httpx.ProxyError):
            return "proxy"
        if isinstance(item, ConnectionRefusedError):
            return "refused"
        if isinstance(item, (ConnectionResetError, ConnectionAbortedError)):
            return "reset"
        if isinstance(item, OSError) and item.errno is not None:
            by_errno = _ERRNO_CAUSES.get(item.errno)
            if by_errno is not None:
                return by_errno
    return "unknown"


def retry_after_seconds(headers: Mapping[str, str] | None) -> float | None:
    """The wait a ``Retry-After`` (seconds or an HTTP date) or ``retry-after-ms`` asks for."""

    if not headers:
        return None
    millis = _header(headers, "retry-after-ms")
    if millis is not None:
        try:
            return max(float(millis) / 1000.0, 0.0)
        except ValueError:
            pass
    value = _header(headers, "retry-after")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max((when - datetime.now(timezone.utc)).total_seconds(), 0.0)


def request_id(headers: Mapping[str, str] | None) -> str | None:
    """The provider's request id from the response ``headers``, when it looks like one."""

    if not headers:
        return None
    for name in _REQUEST_ID_HEADERS:
        value = safe_request_id(_header(headers, name))
        if value is not None:
            return value
    return None


def safe_request_id(value: Any) -> str | None:
    """``value`` as a request id, or ``None`` when it does not look like one."""

    text = str(value or "").strip()
    return text if text and _REQUEST_ID.fullmatch(text) else None


def public_url(url: str | None) -> str | None:
    """``url`` without userinfo, query and fragment, which may carry credentials."""

    if not url:
        return url
    parts = urlsplit(url)
    if not parts.netloc:
        return url.split("?", 1)[0].split("#", 1)[0]
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """A header by its lower-case ``name``; ``httpx.Headers`` and plain dicts alike."""

    value = headers.get(name)
    if value is None:  # a plain dict is case-sensitive
        value = next((v for k, v in headers.items() if str(k).lower() == name), None)
    return None if value is None else str(value)


def _chain(exc: BaseException) -> list[BaseException]:
    """``exc`` and its ``__cause__``/``__context__`` ancestors, each once."""

    seen: list[BaseException] = []
    pending: list[BaseException] = [exc]
    while pending:
        item = pending.pop(0)
        if any(item is known for known in seen):
            continue
        seen.append(item)
        pending.extend(link for link in (item.__cause__, item.__context__) if link is not None)
    return seen


__all__ = [
    "arguments_to_dict",
    "connect_cause",
    "connection_error",
    "empty_response_error",
    "interrupted_error",
    "is_interrupted",
    "new_call_id",
    "optional_int",
    "preview",
    "public_url",
    "request_id",
    "retry_after_seconds",
    "safe_request_id",
    "status_error",
    "status_kind",
    "timeout_error",
    "timeout_phase",
    "tool_to_wire",
]
