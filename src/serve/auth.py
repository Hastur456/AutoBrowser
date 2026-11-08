"""Authentication helpers of the stand's API: constant-time secret checks, a sliding-window
rate limiter and the ``Idempotency-Key`` cache. Secrets never leave these functions."""

from __future__ import annotations

import hashlib
import hmac
from collections import deque
from datetime import datetime, timedelta
from typing import Generic, TypeVar

from pydantic import SecretStr

from src.serve.clock import Clock

#: ``Authorization: Bearer <secret>``.
BEARER_PREFIX = "Bearer "
#: Header carrying the HMAC-SHA256 of a signed webhook body: ``sha256=<hex>``.
SIGNATURE_HEADER = "X-AutoBrowser-Signature"
SIGNATURE_PREFIX = "sha256="
#: The rate limit is per minute (``serve.rate_limit_per_minute``).
RATE_WINDOW = timedelta(minutes=1)

T = TypeVar("T")


def bearer_secret(header: str | None) -> str | None:
    """The secret of an ``Authorization: Bearer`` header; ``None`` when there is none."""

    if not header or not header.startswith(BEARER_PREFIX):
        return None
    return header[len(BEARER_PREFIX):].strip()


def secret_matches(given: str | None, expected: str | SecretStr | None) -> bool:
    """``given == expected`` in constant time; an empty expected secret matches nothing."""

    value = expected.get_secret_value() if isinstance(expected, SecretStr) else expected
    if not value or not value.strip() or given is None:
        return False
    return hmac.compare_digest(given.encode("utf-8"), value.strip().encode("utf-8"))


def signature_matches(body: bytes, header: str | None, secret: str) -> bool:
    """The ``X-AutoBrowser-Signature`` of ``body`` is the HMAC-SHA256 under ``secret``."""

    if not secret or not header or not header.startswith(SIGNATURE_PREFIX):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[len(SIGNATURE_PREFIX):].strip().lower().encode(), expected.encode())


def sign(body: bytes, secret: str) -> str:
    """The header value :func:`signature_matches` accepts (also signs outgoing webhooks)."""

    return SIGNATURE_PREFIX + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class RateLimiter:
    """At most ``per_minute`` hits per key in any sliding minute."""

    def __init__(self, per_minute: int, clock: Clock) -> None:
        self._limit = per_minute
        self._clock = clock
        self._hits: dict[str, deque[datetime]] = {}

    def allow(self, key: str) -> bool:
        """Count a hit on ``key`` unless it is over the limit; ``False`` when it is."""

        self._forget_quiet_keys()
        hits = self._recent(key)
        if len(hits) >= self._limit:
            return False
        hits.append(self._clock.now())
        return True

    def keys(self) -> set[str]:
        return set(self._hits)

    def _forget_quiet_keys(self) -> None:
        """Drop keys without a hit in the last minute (one per client address otherwise)."""

        horizon = self._clock.now() - RATE_WINDOW
        for key in [key for key, hits in self._hits.items() if not hits or hits[-1] <= horizon]:
            del self._hits[key]

    def _recent(self, key: str) -> deque[datetime]:
        hits = self._hits.setdefault(key, deque())
        horizon = self._clock.now() - RATE_WINDOW
        while hits and hits[0] <= horizon:
            hits.popleft()
        return hits


class IdempotencyCache(Generic[T]):
    """``(path, Idempotency-Key)`` -> the first answer, for ``window_seconds``."""

    def __init__(self, window_seconds: float, clock: Clock) -> None:
        self._window = timedelta(seconds=window_seconds)
        self._clock = clock
        self._items: dict[tuple[str, str], tuple[datetime, T]] = {}

    def get(self, path: str, key: str) -> T | None:
        self._expire()
        item = self._items.get((path, key))
        return item[1] if item is not None else None

    def put(self, path: str, key: str, value: T) -> None:
        self._items[(path, key)] = (self._clock.now(), value)

    def _expire(self) -> None:
        horizon = self._clock.now() - self._window
        for key in [key for key, (stamp, _) in self._items.items() if stamp <= horizon]:
            del self._items[key]


__all__ = [
    "BEARER_PREFIX",
    "SIGNATURE_HEADER",
    "IdempotencyCache",
    "RateLimiter",
    "bearer_secret",
    "secret_matches",
    "sign",
    "signature_matches",
]
