"""Playback of recordings: the step source, its retargeter and the plain-text adapter.

``TextPlaybackAdapter`` serves tool servers without a browser snapshot format: arguments
replay literally (no references to rebind) and a checkpoint is a number-blind multiset of
word shingles. Browser snapshots are :mod:`src.browser.playback`'s business.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from src.agent_loop.recordings import fingerprint_key, multiset_similarity, select_region
from src.config import ReplaySettings
from src.contracts import Binding, BindResult


class TextPlaybackAdapter:
    """:class:`~src.contracts.PlaybackAdapter` for tools whose output is plain text."""

    def __init__(self, settings: ReplaySettings | None = None) -> None:
        self._settings = settings or ReplaySettings()

    def describe(self, args: Mapping[str, Any], page_texts: Sequence[str]) -> list[Binding]:
        return []

    def rebind(
        self,
        args: Mapping[str, Any],
        bindings: Sequence[Binding],
        page_texts: Sequence[str],
        *,
        verify: bool = False,
    ) -> BindResult:
        if bindings:
            return BindResult(None, "plain-text tools have no element references")
        return BindResult(dict(args))

    def checkpoint(self, text: str) -> Mapping[str, Any]:
        words = str(text or "").split()
        size = self._settings.text_shingle_words
        shingles: dict[str, int] = {}
        for start in range(max(1, len(words) - size + 1)):
            chunk = " ".join(words[start : start + size])
            if chunk:
                key = fingerprint_key(chunk)
                shingles[key] = shingles.get(key, 0) + 1
        return {"shingles": shingles}

    def similarity(self, recorded: Mapping[str, Any], current: Mapping[str, Any]) -> float:
        return multiset_similarity(
            dict(recorded.get("shingles") or {}), dict(current.get("shingles") or {})
        )

    def region(self, text: str, select: str | None, ignore: Sequence[str]) -> str:
        return select_region(text, select, ignore)


__all__ = ["TextPlaybackAdapter"]
