"""Persistent memory (L4): markdown files with harness-owned frontmatter.

Layout under ``<storage.root_dir>/<memory.dir>/`` (``.autobrowser/memory/`` by default)::

    MEMORY.md                 generated index, rewritten after every write (for humans)
    sites/<domain>.md         what was learned about one site (and its subdomains)
    procedures/<name>.md      reusable procedures

A file is YAML frontmatter plus a markdown body::

    ---
    scope: ozon.ru
    status: user              # user | verified | unverified | stale
    source: user              # user | agent:<task_id>
    description: Ozon — search URL and filters
    ---
    The search results page is https://www.ozon.ru/search/?text=<query>.

A file without frontmatter is the human's (``status: user``). Broken frontmatter or an unknown
``kind``/``status`` skips the file with a ``memory.skipped`` event: memory is data, not
configuration, so it never fails startup.

* :class:`MemoryStore` — reads (cached by ``mtime``/size, so hand edits are picked up), the
  generated index, per-scope lookup, path safety, writes (agent entries only, every one
  through the :class:`~src.contracts.MemoryContentPolicy`) and staged trust
  (:meth:`MemoryStore.record_outcome`).
* :class:`MemoryContext` / :class:`NullMemoryContext` — the ``render(state) -> str`` the engine
  calls for the ``Memory`` context block (``EngineResources.memory``). The engine never opens
  a file, knows a domain or a memory tool name.

Server-neutral: what the current scope is and what must never be stored come from injected
``MemoryScopeResolver`` / ``MemoryContentPolicy`` implementations (``src/browser/memory.py``).
"""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any, get_args
from urllib.parse import unquote

import yaml

from src.config import MemorySettings, StorageSettings, normalize_domain
from src.contracts import (
    CompletionStatus,
    MemoryContentPolicy,
    MemoryEntry,
    MemoryKind,
    MemoryScopeResolver,
    MemoryStatus,
)
from src.harness.memory import NullMemoryContext

#: ``(event_type, payload)``; the session forwards it to its ``EventEmitter``.
MemoryEventCallback = Callable[[str, dict[str, Any]], None]

INDEX_FILE = "MEMORY.md"
KIND_DIRS: dict[str, MemoryKind] = {"sites": "site", "procedures": "procedure"}
ANY_SCOPE = "*"
USER_SOURCE = "user"
AGENT_SOURCE_PREFIX = "agent:"

MEMORY_HEADER = (
    "Persistent memory (hints from earlier sessions; the current snapshot always wins):"
)
UNVERIFIED_PREFIX = "[unverified — verify against the current snapshot]"
TOOLS_HINT = (
    "Use memory_view to read an entry and memory_write to save what a later task on this "
    "site would need: URL templates, the visible names of controls, the steps that worked. "
    "Never save element refs, selectors, form values or personal data."
)
_INDEX_HEADER = (
    "# Memory index\n\n"
    "Generated from the frontmatter of the files below; edit the files, not this index.\n"
)
_TRUNCATED = "\n... [truncated]"
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_SAFE_NAME = re.compile(r"^[\w][\w.-]*\.md$")
_STATUSES = frozenset(get_args(MemoryStatus))
_KINDS = frozenset(get_args(MemoryKind))
#: Order in which bodies are shown, and the reverse of the order they are cut in.
_TRUST_ORDER: dict[str, int] = {"user": 0, "verified": 1, "unverified": 2, "stale": 3}


class MemoryPathError(ValueError):
    """A memory path outside ``sites/`` / ``procedures/`` or not a plain ``*.md`` name."""


class MemoryWriteError(ValueError):
    """A refused write; the message is the reason the model reads."""


class MemoryStore:
    """Files of persistent memory under one root.

    ``policy`` checks every write; without one, writes are refused (a store without a
    policy is read-only). ``today`` is injectable for the TTL and ``verified_at`` dates.
    """

    def __init__(
        self,
        root: Path | str,
        settings: MemorySettings,
        *,
        policy: MemoryContentPolicy | None = None,
        on_event: MemoryEventCallback | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._root = Path(root)
        self._settings = settings
        self._policy = policy
        self._on_event = on_event
        self._today = today
        self._cache: dict[str, tuple[tuple[int, int], MemoryEntry | None]] = {}
        self._task_id = ""
        self._loaded: dict[str, set[str]] = {}

    @classmethod
    def from_settings(
        cls,
        memory: MemorySettings,
        storage: StorageSettings,
        **kwargs: Any,
    ) -> MemoryStore:
        """The store at ``storage.root_dir / memory.dir``; nothing is created until a write."""

        return cls(Path(storage.root_dir) / memory.dir, memory, **kwargs)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def settings(self) -> MemorySettings:
        return self._settings

    @property
    def writable(self) -> bool:
        return self._policy is not None

    # -- task binding ------------------------------------------------------------

    def bind_task(self, task_id: str) -> None:
        """The task agent writes are attributed to (``source: agent:<task_id>``)."""

        self._task_id = str(task_id or "")

    @property
    def task_id(self) -> str:
        return self._task_id

    # -- paths ---------------------------------------------------------------------

    def resolve(self, rel: str) -> Path:
        """The file for ``rel`` (``sites/<name>.md`` / ``procedures/<name>.md``) under the root.

        The only way a caller-supplied path reaches the filesystem: absolute paths, ``..``,
        backslashes, percent-encoding, hidden names and anything outside the two kind
        directories are refused with :class:`MemoryPathError`.
        """

        text = str(rel or "").strip()
        if not text:
            raise MemoryPathError("A memory path is required, e.g. sites/example.com.md.")
        if "\\" in text or "\x00" in text or unquote(text) != text:
            raise MemoryPathError(f"Invalid memory path {text!r}: use plain forward slashes.")
        if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
            raise MemoryPathError(f"Invalid memory path {text!r}: it must be relative.")
        parts = text.split("/")
        if len(parts) != 2 or parts[0] not in KIND_DIRS or not _SAFE_NAME.match(parts[1]):
            raise MemoryPathError(
                f"Invalid memory path {text!r}: use sites/<domain>.md or procedures/<name>.md."
            )
        if parts[1].startswith(".") or ".." in parts[1]:
            raise MemoryPathError(f"Invalid memory path {text!r}.")
        root = self._root.resolve()
        path = (root / parts[0] / parts[1]).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise MemoryPathError(f"Invalid memory path {text!r}: outside memory.") from exc
        return path

    # -- reads -----------------------------------------------------------------------

    def entries(self) -> tuple[MemoryEntry, ...]:
        """Every readable entry, sorted by path, with the TTL applied to ``status``."""

        return tuple(self._with_ttl(entry) for entry in self._raw_entries())

    def get(self, rel: str) -> MemoryEntry | None:
        """The entry at ``rel`` (TTL applied), ``None`` when it does not exist or is unreadable."""

        path = self.resolve(rel)
        rel_posix = self._rel(path)
        entry = self._load(rel_posix, path)
        return self._with_ttl(entry) if entry is not None else None

    def entries_for_scope(self, domain: str) -> tuple[MemoryEntry, ...]:
        """Entries for ``domain``: same host or a parent domain; ``*`` only for the human's."""

        domain = str(domain or "").strip().lower()
        selected = []
        for entry in self.entries():
            if entry.scope == ANY_SCOPE:
                if entry.source == USER_SOURCE:
                    selected.append(entry)
                continue
            if domain and entry.scope and (
                domain == entry.scope or domain.endswith(f".{entry.scope}")
            ):
                selected.append(entry)
        return tuple(selected)

    def render_index(self) -> str:
        """The index generated from frontmatter, cut to ``index_max_lines``/``index_max_chars``."""

        lines = [_index_line(entry) for entry in self.entries()]
        limit = self._settings.index_max_lines
        if len(lines) > limit:
            hidden = len(lines) - limit
            lines = [*lines[:limit], f"... ({hidden} more entries; use memory_view to list)"]
        text = "\n".join(lines)
        return _cut(text, self._settings.index_max_chars)

    # -- writes ----------------------------------------------------------------------

    def create(
        self,
        rel: str,
        *,
        description: str,
        body: str,
        scope: str | None = None,
        keep_trust: bool = False,
    ) -> MemoryEntry:
        """Create or overwrite an agent entry as ``unverified``; the human's files are read-only.

        An overwrite starts the staged trust over (``uses``/``failures`` = 0) unless
        ``keep_trust`` is set: consolidation merges what a task learned into the current body,
        so the successes already counted for that entry still apply.
        """

        path = self.resolve(rel)
        rel_posix = self._rel(path)
        existing = self._load(rel_posix, path) if path.exists() else None
        if path.exists() and existing is None:
            raise MemoryWriteError(f"{rel_posix} is unreadable; only the user can fix it.")
        if existing is not None and existing.source == USER_SOURCE:
            raise MemoryWriteError(
                f"{rel_posix} was written by the user and can only be changed by the user."
            )
        description = " ".join(str(description or "").split())
        if not description:
            raise MemoryWriteError("A one-line description is required.")
        entry = MemoryEntry(
            path=rel_posix,
            kind=KIND_DIRS[rel_posix.split("/", 1)[0]],
            scope=self._write_scope(rel_posix, scope),
            status="unverified",
            source=self._agent_source(),
            description=description,
            body=str(body or "").strip(),
        )
        if keep_trust and existing is not None:
            entry = dataclasses.replace(entry, uses=existing.uses, failures=existing.failures)
        self._check(entry)
        self._write(path, entry)
        return entry

    def str_replace(self, rel: str, old: str, new: str) -> MemoryEntry:
        """Replace the single occurrence of ``old`` in an agent entry's body."""

        path = self.resolve(rel)
        rel_posix = self._rel(path)
        entry = self._load(rel_posix, path) if path.exists() else None
        if entry is None:
            raise MemoryWriteError(f"{rel_posix} does not exist; create it first.")
        if entry.source == USER_SOURCE:
            raise MemoryWriteError(
                f"{rel_posix} was written by the user and can only be changed by the user."
            )
        old = str(old or "")
        count = entry.body.count(old) if old else 0
        if count != 1:
            raise MemoryWriteError(
                f"old_str must occur exactly once in {rel_posix} (found {count})."
            )
        updated = dataclasses.replace(
            entry,
            body=entry.body.replace(old, str(new or ""), 1).strip(),
            status="unverified",
            source=self._agent_source(),
            verified_at="",
            uses=0,
            failures=0,
        )
        self._check(updated)
        self._write(path, updated)
        return updated

    def delete(self, rel: str) -> None:
        """Delete an agent entry; the human's files are never deleted by the agent."""

        path = self.resolve(rel)
        rel_posix = self._rel(path)
        entry = self._load(rel_posix, path) if path.exists() else None
        if entry is None:
            raise MemoryWriteError(f"{rel_posix} does not exist.")
        if entry.source == USER_SOURCE:
            raise MemoryWriteError(
                f"{rel_posix} was written by the user and can only be deleted by the user."
            )
        if not self.writable:
            raise MemoryWriteError("Persistent memory is read-only in this session.")
        path.unlink()
        self._cache.pop(rel_posix, None)
        self._write_index()

    # -- staged trust ------------------------------------------------------------------

    def note_loaded(self, task_id: str, paths: Iterable[str]) -> None:
        """Remember which entries were shown to the model during ``task_id``."""

        if task_id:
            self._loaded.setdefault(str(task_id), set()).update(paths)

    def loaded(self, task_id: str) -> frozenset[str]:
        return frozenset(self._loaded.get(str(task_id), ()))

    def record_outcome(self, task_id: str, status: CompletionStatus | str) -> list[str]:
        """Update the trust of every entry the task loaded; return the paths that changed.

        ``done``: ``uses += 1`` and ``failures = 0``; an ``unverified`` entry with ``uses`` of at
        least ``promote_after_successes`` becomes ``verified`` (``verified_at`` = today); a
        ``stale`` one starts over as ``unverified``; a ``verified`` one refreshes
        ``verified_at``. ``blocked``: ``failures += 1``; at ``stale_after_failures`` the entry
        is ``stale``. ``cancelled`` (and anything else) changes nothing; neither do the
        human's entries.
        """

        paths = self._loaded.pop(str(task_id), set())
        if status not in {"done", "blocked"}:
            return []
        changed = []
        for rel_posix in sorted(paths):
            try:
                path = self.resolve(rel_posix)
            except MemoryPathError:
                continue
            entry = self._load(rel_posix, path) if path.exists() else None
            if entry is None or entry.source == USER_SOURCE or entry.status == "user":
                continue
            updated = self._outcome(entry, status)
            if updated != entry:
                self._write(path, updated)
                changed.append(rel_posix)
        if changed:
            self._emit("memory.outcome", {"task_id": task_id, "status": status, "paths": changed})
        return changed

    def _outcome(self, entry: MemoryEntry, status: str) -> MemoryEntry:
        today = self._today().isoformat()
        if status == "done":
            uses = entry.uses + 1
            if entry.status == "stale":
                return dataclasses.replace(entry, status="unverified", uses=1, failures=0)
            if entry.status == "unverified" and uses >= self._settings.promote_after_successes:
                return dataclasses.replace(
                    entry, status="verified", verified_at=today, uses=uses, failures=0
                )
            if entry.status == "verified":
                return dataclasses.replace(entry, verified_at=today, uses=uses, failures=0)
            return dataclasses.replace(entry, uses=uses, failures=0)
        failures = entry.failures + 1
        if failures >= self._settings.stale_after_failures:
            return dataclasses.replace(entry, status="stale", failures=failures)
        return dataclasses.replace(entry, failures=failures)

    # -- internals -----------------------------------------------------------------------

    def _raw_entries(self) -> list[MemoryEntry]:
        found: list[MemoryEntry] = []
        seen: set[str] = set()
        for directory in KIND_DIRS:
            folder = self._root / directory
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.md")):
                rel_posix = f"{directory}/{path.name}"
                if not _SAFE_NAME.match(path.name) or not path.is_file():
                    continue
                seen.add(rel_posix)
                entry = self._load(rel_posix, path)
                if entry is not None:
                    found.append(entry)
        for stale_key in set(self._cache) - seen:
            self._cache.pop(stale_key, None)
        return sorted(found, key=lambda entry: entry.path)

    def _load(self, rel_posix: str, path: Path) -> MemoryEntry | None:
        try:
            stat = path.stat()
        except OSError:
            self._cache.pop(rel_posix, None)
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(rel_posix)
        if cached is not None and cached[0] == key:
            return cached[1]
        entry: MemoryEntry | None
        try:
            entry = _parse(
                rel_posix,
                path.read_text(encoding="utf-8"),
                description_chars=self._settings.index_description_chars,
            )
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
            entry = None
            reason = str(exc)[: self._settings.event_reason_chars]
            self._emit("memory.skipped", {"path": rel_posix, "reason": reason})
        self._cache[rel_posix] = (key, entry)
        return entry

    def _with_ttl(self, entry: MemoryEntry) -> MemoryEntry:
        days = self._settings.stale_after_days
        if entry.status != "verified" or days <= 0 or not entry.verified_at:
            return entry
        try:
            verified = date.fromisoformat(entry.verified_at[:10])
        except ValueError:
            return entry
        if (self._today() - verified).days > days:
            return dataclasses.replace(entry, status="stale")
        return entry

    def _write_scope(self, rel_posix: str, scope: str | None) -> str:
        directory, name = rel_posix.split("/", 1)
        raw = str(scope or "").strip()
        if not raw and directory == "sites":
            raw = name.removesuffix(".md")
        if raw == ANY_SCOPE:
            raise MemoryWriteError(
                "Agent memory must be scoped to a site; only the user can write '*' entries."
            )
        try:
            return normalize_domain(raw)
        except ValueError as exc:
            raise MemoryWriteError(str(exc)) from exc

    def _agent_source(self) -> str:
        return f"{AGENT_SOURCE_PREFIX}{self._task_id or 'unknown'}"

    def _check(self, entry: MemoryEntry) -> None:
        if self._policy is None:
            raise MemoryWriteError("Persistent memory is read-only in this session.")
        if len(entry.body) > self._settings.file_max_chars:
            raise MemoryWriteError(
                f"The entry is {len(entry.body)} characters; the limit is "
                f"{self._settings.file_max_chars}. Keep only what a later task needs."
            )
        reason = self._policy.violation(f"{entry.description}\n{entry.body}")
        if reason:
            raise MemoryWriteError(reason)

    def _write(self, path: Path, entry: MemoryEntry) -> None:
        _atomic_write(path, _serialize(entry))
        self._cache.pop(entry.path, None)
        self._write_index()

    def _write_index(self) -> None:
        lines = [_index_line(entry) for entry in self.entries()]
        _atomic_write(self._root / INDEX_FILE, _INDEX_HEADER + "\n" + "\n".join(lines) + "\n")

    def _rel(self, path: Path) -> str:
        return path.relative_to(self._root.resolve()).as_posix()

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(event_type, payload)


class MemoryContext:
    """Renders the ``Memory`` context block for one turn (``EngineResources.memory``).

    ``tools_enabled`` adds one line about ``memory_view``/``memory_write`` — only when the
    session registered those tools.
    """

    def __init__(
        self,
        store: MemoryStore,
        scope_resolver: MemoryScopeResolver,
        *,
        tools_enabled: bool = False,
    ) -> None:
        self._store = store
        self._scope = scope_resolver
        self._tools_enabled = tools_enabled
        self._visited: dict[str, set[str]] = {}

    @property
    def store(self) -> MemoryStore:
        return self._store

    @property
    def tools_enabled(self) -> bool:
        return self._tools_enabled

    def visited(self, task_id: str) -> frozenset[str]:
        """The scopes (sites) the task's turns were rendered on."""

        return frozenset(self._visited.get(str(task_id), ()))

    def forget(self, task_id: str) -> None:
        self._visited.pop(str(task_id), None)

    def render(self, state: Mapping[str, Any]) -> str:
        settings = self._store.settings
        entries = self._store.entries()
        if not entries and not self._tools_enabled:
            return ""

        head = [MEMORY_HEADER]
        if self._tools_enabled:
            head.append(TOOLS_HINT)
        head.append("Index:")
        head.append(self._store.render_index() or "(no entries yet)")

        domain = self._scope.scope(state)
        task_id = str(state.get("task_id", "") or "")
        if domain and task_id:
            self._visited.setdefault(task_id, set()).add(domain)
        scoped = sorted(
            self._store.entries_for_scope(domain),
            key=lambda entry: (_TRUST_ORDER.get(entry.status, 9), entry.path),
        )
        sections = [_body_section(entry, settings.file_max_chars) for entry in scoped]
        fixed = "\n".join(head)
        title = f"For {domain or 'every site'}:"
        budget = settings.block_max_chars - len(fixed) - len(title) - 4
        sections = _fit_sections(sections, scoped, budget, settings.block_min_section_chars)

        shown = [entry.path for entry, section in zip(scoped, sections) if section]
        self._store.note_loaded(task_id, shown)
        text = fixed
        if shown:
            text = f"{fixed}\n\n{title}\n" + "\n\n".join(section for section in sections if section)
        return _cut(text, settings.block_max_chars)


# -- file format -------------------------------------------------------------------------


def _parse(rel_posix: str, text: str, *, description_chars: int) -> MemoryEntry:
    directory = rel_posix.split("/", 1)[0]
    match = _FRONTMATTER.match(text)
    if match is None:
        meta: dict[str, Any] = {}
        body = text
    else:
        loaded = yaml.safe_load(match.group(1)) or {}
        if not isinstance(loaded, dict):
            raise ValueError("frontmatter is not a mapping")
        meta = loaded
        body = text[match.end():]
    kind = str(meta.get("kind") or KIND_DIRS[directory])
    if kind not in _KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    source = str(meta.get("source") or USER_SOURCE)
    status = str(meta.get("status") or ("user" if source == USER_SOURCE else "unverified"))
    if status not in _STATUSES:
        raise ValueError(f"unknown status {status!r}")
    scope = str(meta.get("scope") or "").strip()
    if not scope and directory == "sites":
        scope = rel_posix.split("/", 1)[1].removesuffix(".md")
    if scope != ANY_SCOPE:
        scope = normalize_domain(scope)
    body = body.strip()
    description = " ".join(
        str(meta.get("description") or _first_line(body, description_chars)).split()
    )
    return MemoryEntry(
        path=rel_posix,
        kind=kind,  # type: ignore[arg-type]
        scope=scope,
        status=status,  # type: ignore[arg-type]
        source=source,
        description=description,
        body=body,
        verified_at=str(meta.get("verified_at") or ""),
        uses=_count(meta.get("uses")),
        failures=_count(meta.get("failures")),
    )


def _serialize(entry: MemoryEntry) -> str:
    meta = {
        "kind": entry.kind,
        "scope": entry.scope,
        "status": entry.status,
        "source": entry.source,
        "description": entry.description,
        "verified_at": entry.verified_at,
        "uses": entry.uses,
        "failures": entry.failures,
    }
    frontmatter = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{frontmatter}\n---\n{entry.body}\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _first_line(body: str, limit: int) -> str:
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:limit]
    return ""


def _index_line(entry: MemoryEntry) -> str:
    description = f" — {entry.description}" if entry.description else ""
    return f"- {entry.path} [{entry.status}]{description}"


def _body_section(entry: MemoryEntry, file_max_chars: int) -> str:
    body = _cut(entry.body, file_max_chars)
    if entry.status in {"unverified", "stale"}:
        body = f"{UNVERIFIED_PREFIX}\n{body}"
    return f"### {entry.path} [{entry.status}]\n{body}"


def _fit_sections(
    sections: list[str],
    entries: list[MemoryEntry],
    budget: int,
    min_section_chars: int,
) -> list[str]:
    """Cut bodies to ``budget``: unverified/stale first, then verified, the human's last."""

    sections = list(sections)
    overflow = sum(len(section) + 2 for section in sections) - budget
    order = sorted(
        range(len(sections)),
        key=lambda index: (-_TRUST_ORDER.get(entries[index].status, 9), -index),
    )
    for index in order:
        if overflow <= 0:
            break
        section = sections[index]
        keep = len(section) - overflow - len(_TRUNCATED)
        header_end = section.find("\n") + 1
        if keep <= header_end + min_section_chars:
            overflow -= len(section) + 2
            sections[index] = ""
        else:
            sections[index] = section[:keep].rstrip() + _TRUNCATED
            overflow -= len(section) - len(sections[index])
    return sections


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(_TRUNCATED))].rstrip() + _TRUNCATED


__all__ = [
    "ANY_SCOPE",
    "INDEX_FILE",
    "MEMORY_HEADER",
    "MemoryContext",
    "MemoryEventCallback",
    "MemoryPathError",
    "MemoryStore",
    "MemoryWriteError",
    "NullMemoryContext",
    "TOOLS_HINT",
    "UNVERIFIED_PREFIX",
]
