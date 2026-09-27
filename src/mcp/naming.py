"""Server-name validation and collision-free qualified tool/prompt names.

Qualified name = ``f"{server}__{local}"``. The separator is a double underscore because
many LLM tool-calling APIs validate function names against ``^[a-zA-Z0-9_-]{1,64}$``
(``:`` and ``.`` are rejected). MCP itself allows tool names of up to 128 chars from
``[A-Za-z0-9_.-]``, so the local part is sanitized and, when too long or colliding,
truncated and suffixed with a deterministic hash of ``(server, local)``.

Routing never parses qualified names back (a server name containing ``__`` would break
``split("__")``); the manager keeps an explicit ``qualified -> (server, local)`` index.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Container

MAX_TOOL_NAME = 64
MAX_SERVER_NAME = 32

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_SERVER_NAME = re.compile(r"^[A-Za-z0-9-]+(?:_[A-Za-z0-9-]+)*$")
_PROVIDER_SAFE = re.compile(r"^[A-Za-z0-9_-]+$")
_HASH_LEN = 8


def validate_server_name(name: str) -> None:
    """Reject names that would make qualified names ambiguous or too long.

    Allowed: ``[A-Za-z0-9-]`` groups joined by *single* underscores, at most 32 chars
    (leaves room for the tool part within the 64-char provider limit).
    """

    if not isinstance(name, str) or len(name) > MAX_SERVER_NAME or not _SERVER_NAME.fullmatch(name):
        raise ValueError(
            f"invalid MCP server name {name!r}: use [A-Za-z0-9-] groups joined by single "
            f"underscores, at most {MAX_SERVER_NAME} characters"
        )


def sanitize(local_name: str) -> str:
    """Replace characters LLM providers reject with ``_``."""

    return _UNSAFE.sub("_", local_name) or "_"


def is_provider_safe(name: str, max_len: int = MAX_TOOL_NAME) -> bool:
    return 0 < len(name) <= max_len and bool(_PROVIDER_SAFE.fullmatch(name))


def _digest(server: str, local_name: str) -> str:
    return hashlib.sha1(f"{server}\x00{local_name}".encode()).hexdigest()[:_HASH_LEN]


def qualify(
    server: str,
    local_name: str,
    taken: Container[str],
    *,
    max_len: int = MAX_TOOL_NAME,
) -> str:
    """Return a provider-safe, unique qualified name for ``(server, local_name)``.

    Deterministic for a given ``taken`` set, so names stay stable across index rebuilds
    as long as the catalog does (the LLM keeps these names in its conversation context).
    """

    base = f"{server}__{sanitize(local_name)}"
    if len(base) <= max_len and base not in taken:
        return base
    digest = _digest(server, local_name)
    candidate = f"{base[: max_len - _HASH_LEN - 1]}_{digest}"
    salt = 0
    while candidate in taken:  # practically unreachable; keeps the result unique anyway
        salt += 1
        digest = _digest(server, f"{local_name}\x00{salt}")
        candidate = f"{base[: max_len - _HASH_LEN - 1]}_{digest}"
    return candidate


__all__ = [
    "MAX_SERVER_NAME",
    "MAX_TOOL_NAME",
    "is_provider_safe",
    "qualify",
    "sanitize",
    "validate_server_name",
]
