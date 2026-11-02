"""Browser side of playback: element references in accessibility snapshots (R2).

The only module that knows the snapshot text format of browser tools for replay
(``docs/development/2026-11-02-serve-mode-design.md`` §5.3). Nothing here names a tool or an
argument:

* a **reference** is an argument value ``V`` for which the page text before the step contains
  ``[ref=V]`` (Playwright MCP prints ``e42`` or a frame-prefixed ``f3e42``);
* the **element line** is the snapshot line carrying that marker:
  ``- button "Buy now" [ref=e4] [cursor=pointer]:``; its role, accessible name, position
  among equal lines and nearest named ancestors describe the element;
* the **page URL** is the ``- Page URL:`` line (:func:`src.browser.pages.page_url`).

A recorded reference is rebound **before** the call to the one current line that matches its
descriptor; none or several is a divergence, never a guess. Frame prefixes change between
page loads, so they only break ties.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from src.agent_loop.recordings import fingerprint_key, multiset_similarity, select_region
from src.browser.pages import page_url, url_domain
from src.config import ReplaySettings
from src.contracts import Binding, BindResult

#: ``[ref=e42]`` / ``[ref=f3e42]`` markers of a snapshot line.
_REF = re.compile(r"\[ref=([^\]\s]+)\]")
#: Frame prefix of a reference (``f3`` in ``f3e42``).
_FRAME = re.compile(r"^(f\d+)e\d+$")
#: ``role "name"`` at the start of a snapshot line (after the ``- `` bullet).
_ROLE_NAME = re.compile(r'^([A-Za-z][\w-]*)(?:\s+"((?:[^"\\]|\\.)*)")?')
#: Bracketed attributes: ``[ref=…]``, ``[cursor=pointer]``, ``[level=2]``, ``[checked]``.
_ATTRIBUTE = re.compile(r"\s*\[[^\]]*\]")
#: Markers an output region drops: references and cursor hints change on every load.
_REGION_DROP = (re.compile(r"\s*\[ref=[^\]]*\]"), re.compile(r"\s*\[cursor=[^\]]*\]"))
#: Lines without a name that only group others (``- generic [ref=e3]:``).
_ANONYMOUS_GROUP_ROLES = frozenset({"generic", "group", "none", "presentation"})


@dataclass(frozen=True)
class _Line:
    """One snapshot line with a reference."""

    ref: str
    role: str
    name: str
    indent: int
    context: tuple[str, ...]
    frame: str

    @property
    def label(self) -> str:
        return f'{self.role} "{self.name}"' if self.name else self.role


def _parse_lines(text: str, context_lines: int) -> list[_Line]:
    """Every snapshot line of ``text`` that carries a reference, in document order."""

    parsed: list[_Line] = []
    # Open ancestors by indent: (indent, label) of the nearest named or meaningful lines.
    stack: list[tuple[int, str]] = []
    for raw in str(text or "").splitlines():
        stripped = raw.lstrip(" ")
        if not stripped.startswith("- "):
            continue
        indent = len(raw) - len(stripped)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        body = stripped[2:]
        role, name = _role_and_name(body)
        match = _REF.search(body)
        if match is not None and role:
            ref = match.group(1)
            frame = _FRAME.match(ref)
            ancestors = tuple(label for _, label in stack)
            parsed.append(
                _Line(
                    ref=ref,
                    role=role,
                    name=name,
                    indent=indent,
                    context=ancestors[-context_lines:] if context_lines else (),
                    frame=frame.group(1) if frame else "",
                )
            )
        if role and (name or role not in _ANONYMOUS_GROUP_ROLES):
            stack.append((indent, f'{role} "{name}"' if name else role))
    return parsed


def _role_and_name(body: str) -> tuple[str, str]:
    """Role and accessible name of a snapshot line body; the inline text after ``:`` stands
    in for a missing quoted name (``- cell [ref=e9]: 6.50``)."""

    match = _ROLE_NAME.match(body)
    if match is None:
        return "", ""
    role, name = match.group(1), match.group(2)
    if name is not None:
        return role, name.replace('\\"', '"')
    rest = _ATTRIBUTE.sub("", body[match.end() :]).strip()
    if rest.startswith(":"):
        return role, rest[1:].strip()
    return role, ""


def _number_blind(name: str) -> str:
    return re.sub(r"\d+", "#", name)


def _walk_strings(value: Any, path: tuple[str | int, ...] = ()) -> Iterator[tuple[tuple[str | int, ...], str]]:
    """Every string value inside nested mappings and lists, with its JSON path."""

    if isinstance(value, str):
        yield path, value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk_strings(item, (*path, str(key)))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk_strings(item, (*path, index))


def _get_path(value: Any, path: Sequence[str | int]) -> Any:
    for part in path:
        if isinstance(value, Mapping) and isinstance(part, str):
            value = value.get(part)
        elif isinstance(value, list) and isinstance(part, int) and 0 <= part < len(value):
            value = value[part]
        else:
            return None
    return value


def _set_path(args: dict[str, Any], path: Sequence[str | int], new: str) -> bool:
    target: Any = args
    for part in path[:-1]:
        target = _get_path(target, [part])
        if target is None:
            return False
    last = path[-1]
    if isinstance(target, dict) and isinstance(last, str) and last in target:
        target[last] = new
        return True
    if isinstance(target, list) and isinstance(last, int) and 0 <= last < len(target):
        target[last] = new
        return True
    return False


class BrowserPlaybackAdapter:
    """:class:`~src.contracts.PlaybackAdapter` for accessibility-snapshot browser tools."""

    def __init__(self, settings: ReplaySettings | None = None) -> None:
        self._settings = settings or ReplaySettings()

    # -- recording ----------------------------------------------------------------------

    def describe(self, args: Mapping[str, Any], page_texts: Sequence[str]) -> list[Binding]:
        lines = self._lines(page_texts)
        by_ref = {line.ref: line for line in lines}
        bindings: list[Binding] = []
        for path, value in _walk_strings(dict(args)):
            line = by_ref.get(value.strip())
            if line is None:
                continue
            equal = [other for other in lines if (other.role, other.name) == (line.role, line.name)]
            bindings.append(
                Binding(
                    path=path,
                    value=value,
                    descriptor={
                        "role": line.role,
                        "name": line.name,
                        "nth": equal.index(line) + 1,
                        "count": len(equal),
                        "context": list(line.context),
                        "frame": line.frame,
                    },
                )
            )
        return bindings

    # -- playback -----------------------------------------------------------------------

    def rebind(
        self,
        args: Mapping[str, Any],
        bindings: Sequence[Binding],
        page_texts: Sequence[str],
        *,
        verify: bool = False,
    ) -> BindResult:
        lines = self._lines(page_texts)
        if verify:
            live = {line.ref for line in lines}
            for binding in bindings:
                value = _get_path(args, binding.path)
                if not isinstance(value, str) or value.strip() not in live:
                    return BindResult(None, f"{_path_text(binding.path)} is not a reference on the page")
            return BindResult(dict(args))
        rebound = copy.deepcopy(dict(args))
        for binding in bindings:
            line, detail = self._match(binding.descriptor, lines)
            if line is None:
                return BindResult(None, f"{_path_text(binding.path)}: {detail}")
            if not _set_path(rebound, binding.path, line.ref):
                return BindResult(None, f"{_path_text(binding.path)}: not in the recorded arguments")
        return BindResult(rebound)

    def checkpoint(self, text: str) -> Mapping[str, Any]:
        url = page_url(text)
        structure: dict[str, int] = {}
        for line in self._lines([text]):
            if not line.name and line.role in _ANONYMOUS_GROUP_ROLES:
                continue
            key = fingerprint_key(line.label)
            structure[key] = structure.get(key, 0) + 1
        return {"url": _url_path(url), "host": url_domain(url), "structure": structure}

    def similarity(self, recorded: Mapping[str, Any], current: Mapping[str, Any]) -> float:
        mode = self._settings.checkpoint_url
        if mode != "off":
            field = "url" if mode == "path" else "host"
            before, after = str(recorded.get(field) or ""), str(current.get(field) or "")
            # A missing URL on either side cannot tell; a different one is another page.
            if before and after and before != after:
                return 0.0
        return multiset_similarity(
            dict(recorded.get("structure") or {}), dict(current.get("structure") or {})
        )

    def region(self, text: str, select: str | None, ignore: Sequence[str]) -> str:
        return select_region(text, select, ignore, drop=_REGION_DROP)

    # -- internals ----------------------------------------------------------------------

    def _lines(self, page_texts: Sequence[str]) -> list[_Line]:
        """Reference lines of every text; a reference seen in an earlier text wins."""

        seen: set[str] = set()
        lines: list[_Line] = []
        for text in page_texts:
            for line in _parse_lines(text, self._settings.describe_context_lines):
                if line.ref not in seen:
                    seen.add(line.ref)
                    lines.append(line)
        return lines

    @staticmethod
    def _match(descriptor: Mapping[str, Any], lines: Sequence[_Line]) -> tuple[_Line | None, str]:
        role = str(descriptor.get("role") or "")
        name = str(descriptor.get("name") or "")
        context = tuple(descriptor.get("context") or ())
        candidates = [line for line in lines if (line.role, line.name) == (role, name)]
        if not candidates:
            # A number in the name changed (a price, a counter): the same element otherwise.
            blind = _number_blind(name)
            candidates = [
                line for line in lines if line.role == role and _number_blind(line.name) == blind
            ]
        label = f'{role} "{name}"' if name else role
        if not candidates:
            return None, f"no {label} on the page"
        if len(candidates) == 1:
            return candidates[0], ""
        in_context = [line for line in candidates if line.context == context]
        if len(in_context) == 1:
            return in_context[0], ""
        nth, count = int(descriptor.get("nth") or 0), int(descriptor.get("count") or 0)
        if count == len(candidates) and 1 <= nth <= count:
            return candidates[nth - 1], ""
        frame = str(descriptor.get("frame") or "")
        in_frame = [line for line in (in_context or candidates) if line.frame == frame]
        if len(in_frame) == 1:
            return in_frame[0], ""
        return None, f"{len(candidates)} lines match {label}"


def _url_path(url: str) -> str:
    """Host and path of ``url`` (no scheme, query, fragment or trailing slash)."""

    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = url_domain(url)
    path = parts.path.rstrip("/")
    return f"{host}{path}" if host else path


def _path_text(path: Sequence[str | int]) -> str:
    return ".".join(str(part) for part in path) or "<args>"


__all__ = ["BrowserPlaybackAdapter"]
