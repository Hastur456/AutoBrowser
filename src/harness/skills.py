"""Agent skills: ``SKILL.md`` folders in the Agent Skills format, read once per session.

A skill is a folder with a ``SKILL.md`` — YAML frontmatter plus a markdown body — and any
reference files next to it::

    <root>/pdf-forms/
        SKILL.md
        reference.md

    ---
    name: pdf-forms                    # = the folder name; lowercase, digits, hyphens
    description: Fill and check PDF forms. Use when a task involves a PDF form.
    metadata:                          # AutoBrowser extensions, strings per the spec
      always: "true"                   # the body is a fixed system block on every turn
      requires: browser                # session capabilities, comma-separated
      title: PDF Forms                 # heading of the always-on block (default: name)
    ---
    ...instructions...

Roots are the built-in skills in the package and the user's folder
(``<storage.root_dir>/<skills.dir>/``); a user skill shadows a built-in one of the same name.
A broken skill is skipped with a ``skills.invalid`` event and never fails startup. Skills are
the human's text: the agent never writes them, and no content policy applies.

* :class:`SkillStore` — discovery, validation, the capability filter and file reads confined to
  the skill folder.
* :class:`SkillCatalog` — the text of the ``Skills`` context block (name and description per
  skill); the model loads a body itself with the ``skill_view`` tool
  (``src/harness/skill_tool.py``).

The engine never imports this module or names the tool (``tests/test_skill_boundaries.py``).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml

from src.agent_loop.context import PromptSection
from src.config import SkillsSettings, StorageSettings

#: ``(event_type, payload)``; the session forwards it to its ``EventEmitter``.
SkillEventCallback = Callable[[str, dict[str, Any]], None]
SkillSource = Literal["builtin", "user"]

#: Capabilities a session can declare; ``metadata.requires`` may name only these.
BROWSER_CAPABILITY = "browser"
SESSION_CAPABILITIES = frozenset({BROWSER_CAPABILITY})

SKILL_FILE = "SKILL.md"
SKILLS_SERVER = "skills"
VIEW_TOOL = "skill_view"
#: Package (and directory under it) holding the built-in skills.
BUILTIN_PACKAGE = "src.agent_loop"
BUILTIN_DIR = ("resources", "skills")

CATALOG_BLOCK_TITLE = "Skills"
CATALOG_HEADER = (
    f"Skills are instructions for kinds of work. When one fits the task, read it with "
    f"{VIEW_TOOL} before acting; it may point to further files in the skill."
)
_MORE_SKILLS = "... {count} more skills not shown"
_TRUNCATED = "..."

#: Frontmatter keys of the Agent Skills format; others are kept for ``doctor`` only.
_SPEC_FIELDS = frozenset({"name", "description", "license", "compatibility", "metadata", "allowed-tools"})
_NAME = re.compile(r"^(?=.{1,64}$)[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
_TRUE = frozenset({"true", "yes", "1"})
_FALSE = frozenset({"false", "no", "0", ""})


class SkillFileError(ValueError):
    """A refused skill file read; the message is the reason the model reads."""


@dataclass(frozen=True)
class Skill:
    """One valid skill."""

    name: str
    description: str
    source: SkillSource
    #: The skill folder.
    path: Path
    body: str
    always: bool = False
    requires: frozenset[str] = frozenset()
    #: Heading of the always-on block; :attr:`heading` falls back to the name.
    title: str = ""
    #: Top-level frontmatter keys outside the Agent Skills format (reported by ``doctor``).
    unknown_fields: tuple[str, ...] = ()

    @property
    def heading(self) -> str:
        return self.title or self.name


@dataclass(frozen=True)
class SkillProblem:
    """A skill that was skipped, and why."""

    source: SkillSource
    #: The skill folder relative to its root (POSIX).
    path: str
    reason: str


class SkillStore:
    """The session's skills: built once at session start, read-only afterwards."""

    def __init__(
        self,
        skills: Iterable[Skill],
        settings: SkillsSettings,
        *,
        problems: Sequence[SkillProblem] = (),
        shadowed: Sequence[str] = (),
    ) -> None:
        self._skills = {skill.name: skill for skill in sorted(skills, key=lambda skill: skill.name)}
        self._settings = settings
        self._problems = tuple(problems)
        self._shadowed = tuple(shadowed)

    @classmethod
    def load(
        cls,
        settings: SkillsSettings,
        *,
        roots: Sequence[tuple[SkillSource, Path]],
        capabilities: Iterable[str] = (),
        on_event: SkillEventCallback | None = None,
    ) -> SkillStore:
        """Read every root in order; a later root's skill replaces an earlier one's.

        Skills whose ``requires`` the session's ``capabilities`` do not cover are hidden
        (not a problem: the same files serve sessions with and without a browser).
        """

        available = frozenset(capabilities)
        found: dict[str, Skill] = {}
        problems: list[SkillProblem] = []
        shadowed: list[str] = []

        def skip(problem: SkillProblem) -> None:
            problems.append(problem)
            if on_event is not None:
                on_event(
                    "skills.invalid",
                    {"source": problem.source, "path": problem.path, "reason": problem.reason},
                )

        for source, root in roots:
            try:
                folders = _skill_folders(root)
            except OSError as exc:
                skip(SkillProblem(source, root.name, f"cannot read the skills folder: {exc}"))
                continue
            for read, folder in enumerate(folders):
                rel = folder.name
                if read >= settings.max_skills:
                    skip(SkillProblem(source, rel, f"more than skills.max_skills ({settings.max_skills}) skills"))
                    continue
                try:
                    skill = _read_skill(folder, source, settings)
                except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
                    skip(SkillProblem(source, rel, str(exc) or type(exc).__name__))
                    continue
                if not skill.requires <= available:
                    continue
                previous = found.get(skill.name)
                if previous is not None and previous.source != source:
                    shadowed.append(skill.name)
                    if on_event is not None:
                        on_event("skills.shadowed", {"name": skill.name, "source": source})
                found[skill.name] = skill
        return cls(found.values(), settings, problems=problems, shadowed=shadowed)

    @property
    def settings(self) -> SkillsSettings:
        return self._settings

    def skills(self) -> tuple[Skill, ...]:
        """Every visible skill, sorted by name."""

        return tuple(self._skills.values())

    def always(self) -> tuple[Skill, ...]:
        """The always-on skills, sorted by name."""

        return tuple(skill for skill in self._skills.values() if skill.always)

    def get(self, name: str) -> Skill | None:
        return self._skills.get(str(name or "").strip())

    def problems(self) -> tuple[SkillProblem, ...]:
        return self._problems

    def shadowed(self) -> tuple[str, ...]:
        """Built-in skill names a user skill replaced."""

        return self._shadowed

    def read_file(self, name: str, path: str) -> tuple[str, bool]:
        """``(text, truncated)`` of a file inside the skill folder, at most
        ``skills.file_max_chars`` characters.

        Absolute paths, ``..`` and links out of the folder, missing, binary and non-UTF-8
        files raise :class:`SkillFileError` (``LookupError`` for an unknown skill).
        """

        skill = self.get(name)
        if skill is None:
            raise LookupError(f"Unknown skill {name!r}. Skills: {', '.join(self._skills) or 'none'}.")
        relative = str(path or "").strip().replace("\\", "/")
        if not relative or Path(relative).is_absolute() or Path(relative).drive:
            raise SkillFileError("path must be a file path relative to the skill folder.")
        base = skill.path.resolve()
        target = (base / relative).resolve()
        if not target.is_relative_to(base):
            raise SkillFileError(f"{relative} is outside the skill {skill.name}.")
        if not target.is_file():
            raise SkillFileError(f"{relative} does not exist in the skill {skill.name}.")
        limit = self._settings.file_max_chars
        try:
            with target.open(encoding="utf-8-sig", errors="strict") as handle:
                text = handle.read(limit + 1)
        except UnicodeDecodeError as exc:
            raise SkillFileError(f"{relative} is not a UTF-8 text file.") from exc
        if "\x00" in text:
            raise SkillFileError(f"{relative} is not a text file.")
        if len(text) > limit:
            return text[:limit], True
        return text, False


class SkillCatalog:
    """Renders the ``Skills`` context block: one ``- name: description`` line per skill."""

    def __init__(self, store: SkillStore) -> None:
        self._store = store

    def render(self) -> str:
        """The block text; ``""`` when no skill is visible."""

        skills = self._store.skills()
        if not skills:
            return ""
        settings = self._store.settings
        lines = [_catalog_line(skill, settings.description_max_chars) for skill in skills]
        budget = settings.catalog_max_chars
        text = CATALOG_HEADER
        for index, line in enumerate(lines):
            rest = len(lines) - index - 1
            tail = f"\n{_MORE_SKILLS.format(count=rest)}" if rest else ""
            if len(text) + 1 + len(line) + len(tail) > budget:
                return f"{text}\n{_MORE_SKILLS.format(count=len(lines) - index)}"[:budget]
            text = f"{text}\n{line}"
        return text[:budget]


def always_sections(store: SkillStore) -> list[PromptSection]:
    """One fixed system block per always-on skill (body under its heading), by name."""

    return [
        PromptSection(name=skill.name, title=skill.heading, text=skill.body)
        for skill in store.always()
    ]


def catalog_section(store: SkillStore) -> list[PromptSection]:
    """The ``Skills`` catalog block, or nothing when no skill is visible."""

    text = SkillCatalog(store).render()
    if not text:
        return []
    # After the Tool Inventory (40), before the always-on bodies (45).
    return [PromptSection(name=SKILLS_SERVER, title=CATALOG_BLOCK_TITLE, text=text, priority=44)]


def builtin_skills_root() -> Path:
    """The package directory of the built-in skills (found outside the checkout too)."""

    return Path(str(resources.files(BUILTIN_PACKAGE).joinpath(*BUILTIN_DIR)))


def user_skills_root(skills: SkillsSettings, storage: StorageSettings) -> Path:
    """``<storage.root_dir>/<skills.dir>`` (an absolute ``skills.dir`` stands alone)."""

    return Path(storage.root_dir) / skills.dir


def skill_roots(
    skills: SkillsSettings,
    storage: StorageSettings,
) -> list[tuple[SkillSource, Path]]:
    """The built-in root, plus the user's when skills are enabled."""

    roots: list[tuple[SkillSource, Path]] = [("builtin", builtin_skills_root())]
    if skills.enabled:
        roots.append(("user", user_skills_root(skills, storage)))
    return roots


# -- file format -------------------------------------------------------------------------


def _skill_folders(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        (child for child in root.iterdir() if child.is_dir() and (child / SKILL_FILE).is_file()),
        key=lambda child: child.name,
    )


def _read_skill(folder: Path, source: SkillSource, settings: SkillsSettings) -> Skill:
    path = folder / SKILL_FILE
    # utf-8-sig: Windows editors and PowerShell 5.1 write a BOM before the frontmatter.
    with path.open(encoding="utf-8-sig", errors="strict") as handle:
        text = handle.read(settings.file_max_chars + 1)
    if len(text) > settings.file_max_chars:
        raise ValueError(f"{SKILL_FILE} is longer than skills.file_max_chars ({settings.file_max_chars})")
    match = _FRONTMATTER.match(text)
    if match is None:
        raise ValueError(f"{SKILL_FILE} has no YAML frontmatter")
    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, Mapping):
        raise ValueError("frontmatter is not a mapping")
    name = meta.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise ValueError("name must be 1-64 lowercase letters, digits and single hyphens")
    if name != folder.name:
        raise ValueError(f"name {name!r} differs from the folder name {folder.name!r}")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description is missing")
    extensions = meta.get("metadata") or {}
    if not isinstance(extensions, Mapping):
        raise ValueError("metadata is not a mapping")
    always = _flag(extensions.get("always"))
    requires = _capabilities(extensions.get("requires"))
    unknown = requires - SESSION_CAPABILITIES
    if unknown:
        raise ValueError(
            f"unknown capability {', '.join(sorted(unknown))} in metadata.requires "
            f"(known: {', '.join(sorted(SESSION_CAPABILITIES))})"
        )
    title = extensions.get("title")
    if title is not None and not isinstance(title, str):
        raise ValueError("metadata.title is not a string")
    body = text[match.end():]
    if always and len(body.strip()) > settings.always_max_chars:
        raise ValueError(
            f"the always-on body is longer than skills.always_max_chars ({settings.always_max_chars})"
        )
    return Skill(
        name=name,
        description=" ".join(description.split()),
        source=source,
        path=folder,
        body=body,
        always=always,
        requires=requires,
        title=(title or "").strip(),
        unknown_fields=tuple(sorted(str(key) for key in meta if key not in _SPEC_FIELDS)),
    )


def _flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"metadata.always must be \"true\" or \"false\", got {value!r}")


def _capabilities(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    items = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return frozenset(str(item).strip().lower() for item in items if str(item).strip())


def _catalog_line(skill: Skill, description_chars: int) -> str:
    description = skill.description
    if len(description) > description_chars:
        keep = max(0, description_chars - len(_TRUNCATED))
        description = description[:keep].rstrip() + _TRUNCATED
    return f"- {skill.name}: {description}"


__all__ = [
    "BROWSER_CAPABILITY",
    "CATALOG_BLOCK_TITLE",
    "SESSION_CAPABILITIES",
    "SKILLS_SERVER",
    "SKILL_FILE",
    "VIEW_TOOL",
    "always_sections",
    "catalog_section",
    "Skill",
    "SkillCatalog",
    "SkillEventCallback",
    "SkillFileError",
    "SkillProblem",
    "SkillSource",
    "SkillStore",
    "builtin_skills_root",
    "skill_roots",
    "user_skills_root",
]
