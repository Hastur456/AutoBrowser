"""``SkillStore`` / ``SkillCatalog``: discovery, validation, capabilities, file reads."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from src.config import SkillsSettings, StorageSettings
from src.harness.skills import (
    CATALOG_HEADER,
    SkillCatalog,
    SkillFileError,
    SkillStore,
    builtin_skills_root,
    skill_roots,
    user_skills_root,
)

ENABLED = SkillsSettings(enabled=True)


def write_skill(
    root: Path,
    name: str,
    *,
    description: str = "Does a thing. Use when the task needs the thing.",
    body: str = "# Steps\n\n1. Do the thing.\n",
    metadata: str = "",
    frontmatter: str | None = None,
    files: dict[str, str] | None = None,
) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    if frontmatter is None:
        frontmatter = f"name: {name}\ndescription: {description}\n{metadata}"
    (folder / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n{body}", encoding="utf-8")
    for rel, text in (files or {}).items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text, encoding="utf-8")
    return folder


def load(root: Path, settings: SkillsSettings = ENABLED, **kwargs: Any) -> SkillStore:
    return SkillStore.load(settings, roots=[("user", root)], **kwargs)


# -- discovery and format ----------------------------------------------------------------


def test_a_skill_is_a_folder_with_a_skill_md(tmp_path: Path) -> None:
    write_skill(tmp_path, "pdf-forms", body="Fill the form.\n")
    (tmp_path / "notes").mkdir()  # no SKILL.md: not a skill, not a problem
    (tmp_path / "loose.md").write_text("x", encoding="utf-8")

    store = load(tmp_path)

    (skill,) = store.skills()
    assert (skill.name, skill.source, skill.body) == ("pdf-forms", "user", "Fill the form.\n")
    assert skill.path == tmp_path / "pdf-forms"
    assert skill.always is False and skill.requires == frozenset()
    assert store.problems() == ()


def test_skills_are_sorted_by_name(tmp_path: Path) -> None:
    for name in ("zeta", "alpha", "mid-one"):
        write_skill(tmp_path, name)

    assert [skill.name for skill in load(tmp_path).skills()] == ["alpha", "mid-one", "zeta"]


def test_a_missing_root_yields_no_skills(tmp_path: Path) -> None:
    store = load(tmp_path / "absent")

    assert store.skills() == () and store.problems() == ()


def test_the_description_is_folded_to_one_line(tmp_path: Path) -> None:
    write_skill(tmp_path, "multi", frontmatter="name: multi\ndescription: >-\n  First line\n  second line")

    (skill,) = load(tmp_path).skills()
    assert skill.description == "First line second line"


def test_metadata_extensions_are_read(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "rules",
        metadata='metadata:\n  always: "true"\n  requires: browser\n  title: Browser Rules\n  author: me',
    )

    (skill,) = load(tmp_path, capabilities={"browser"}).skills()
    assert skill.always is True
    assert skill.requires == frozenset({"browser"})
    assert skill.heading == "Browser Rules"


def test_a_yaml_boolean_always_is_accepted(tmp_path: Path) -> None:
    write_skill(tmp_path, "lit", metadata="metadata:\n  always: true")

    assert load(tmp_path).always()[0].name == "lit"


def test_the_heading_falls_back_to_the_name(tmp_path: Path) -> None:
    write_skill(tmp_path, "plain")

    assert load(tmp_path).skills()[0].heading == "plain"


def test_unknown_top_level_fields_are_kept_for_doctor(tmp_path: Path) -> None:
    write_skill(tmp_path, "extra", metadata="license: MIT\nversion: 2\nallowed-tools: Read")

    assert load(tmp_path).skills()[0].unknown_fields == ("version",)


@pytest.mark.parametrize(
    "frontmatter, reason",
    [
        ("description: no name", "name must be"),
        ("name: Bad_Name\ndescription: x", "name must be"),
        ("name: other\ndescription: x", "differs from the folder name"),
        ("name: broken", "description is missing"),
        ("name: broken\ndescription: '   '", "description is missing"),
        ("name: broken\ndescription: x\nmetadata: [1, 2]", "metadata is not a mapping"),
        ("name: broken\ndescription: x\nmetadata:\n  always: maybe", "metadata.always"),
        ("name: broken\ndescription: x\nmetadata:\n  requires: browser, gpu", "unknown capability gpu"),
        ("name: broken\ndescription: x\nmetadata:\n  title: [x]", "metadata.title"),
        ("[a, b]", "not a mapping"),
        ("name: [unclosed", "while parsing"),
    ],
)
def test_an_invalid_skill_is_skipped_with_a_reason(tmp_path: Path, frontmatter: str, reason: str) -> None:
    write_skill(tmp_path, "broken", frontmatter=frontmatter)
    write_skill(tmp_path, "good")
    events: list[tuple[str, dict[str, Any]]] = []

    store = load(tmp_path, on_event=lambda kind, payload: events.append((kind, payload)))

    assert [skill.name for skill in store.skills()] == ["good"]
    (problem,) = store.problems()
    assert (problem.source, problem.path) == ("user", "broken")
    assert reason in problem.reason
    assert events == [("skills.invalid", {"source": "user", "path": "broken", "reason": problem.reason})]


def test_a_skill_without_frontmatter_is_invalid(tmp_path: Path) -> None:
    folder = tmp_path / "bare"
    folder.mkdir()
    (folder / "SKILL.md").write_text("# Just a body\n", encoding="utf-8")

    (problem,) = load(tmp_path).problems()
    assert "no YAML frontmatter" in problem.reason


def test_a_non_utf8_skill_md_is_invalid(tmp_path: Path) -> None:
    folder = tmp_path / "latin"
    folder.mkdir()
    (folder / "SKILL.md").write_bytes(b"---\nname: latin\ndescription: caf\xe9\n---\n")

    assert load(tmp_path).skills() == ()
    assert len(load(tmp_path).problems()) == 1


def test_a_skill_md_longer_than_file_max_chars_is_invalid(tmp_path: Path) -> None:
    write_skill(tmp_path, "huge", body="x" * 200)

    store = load(tmp_path, SkillsSettings(enabled=True, file_max_chars=100))

    assert store.skills() == ()
    assert "skills.file_max_chars" in store.problems()[0].reason


def test_an_always_on_body_longer_than_always_max_chars_is_invalid(tmp_path: Path) -> None:
    write_skill(tmp_path, "long", body="x" * 50, metadata='metadata:\n  always: "true"')
    write_skill(tmp_path, "lazy", body="x" * 50)

    store = load(tmp_path, SkillsSettings(enabled=True, always_max_chars=10))

    assert [skill.name for skill in store.skills()] == ["lazy"]
    assert "skills.always_max_chars" in store.problems()[0].reason


def test_max_skills_caps_each_source(tmp_path: Path) -> None:
    builtin, user = tmp_path / "builtin", tmp_path / "user"
    for name in ("a", "b", "c"):
        write_skill(builtin, f"builtin-{name}")
        write_skill(user, f"user-{name}")

    store = SkillStore.load(
        SkillsSettings(enabled=True, max_skills=2),
        roots=[("builtin", builtin), ("user", user)],
    )

    assert [skill.name for skill in store.skills()] == ["builtin-a", "builtin-b", "user-a", "user-b"]
    assert [(problem.source, problem.path) for problem in store.problems()] == [
        ("builtin", "builtin-c"),
        ("user", "user-c"),
    ]


# -- capabilities and sources ------------------------------------------------------------


def test_a_skill_needing_a_missing_capability_is_hidden_not_invalid(tmp_path: Path) -> None:
    write_skill(tmp_path, "web", metadata="metadata:\n  requires: browser")

    without = load(tmp_path)
    with_browser = load(tmp_path, capabilities={"browser"})

    assert without.skills() == () and without.problems() == ()
    assert [skill.name for skill in with_browser.skills()] == ["web"]


def test_a_user_skill_shadows_a_builtin_of_the_same_name(tmp_path: Path) -> None:
    write_skill(tmp_path / "builtin", "rules", body="built-in\n", metadata='metadata:\n  always: "true"')
    write_skill(tmp_path / "builtin", "other")
    write_skill(tmp_path / "user", "rules", body="mine\n")
    events: list[tuple[str, dict[str, Any]]] = []

    store = SkillStore.load(
        ENABLED,
        roots=[("builtin", tmp_path / "builtin"), ("user", tmp_path / "user")],
        on_event=lambda kind, payload: events.append((kind, payload)),
    )

    rules = store.get("rules")
    assert rules is not None and (rules.source, rules.body, rules.always) == ("user", "mine\n", False)
    assert store.shadowed() == ("rules",)
    assert store.always() == ()
    assert events == [("skills.shadowed", {"name": "rules", "source": "user"})]


def test_the_user_root_is_read_only_when_skills_are_enabled(tmp_path: Path) -> None:
    storage = StorageSettings(root_dir=tmp_path)

    assert skill_roots(SkillsSettings(), storage) == [("builtin", builtin_skills_root())]
    assert skill_roots(ENABLED, storage) == [
        ("builtin", builtin_skills_root()),
        ("user", tmp_path / "skills"),
    ]
    assert user_skills_root(SkillsSettings(dir=str(tmp_path / "abs")), storage) == tmp_path / "abs"


def test_the_builtin_root_is_inside_the_package() -> None:
    root = builtin_skills_root()

    assert root.parts[-3:] == ("agent_loop", "resources", "skills")


# -- file reads --------------------------------------------------------------------------


def test_a_reference_file_is_read_from_the_skill_folder(tmp_path: Path) -> None:
    write_skill(tmp_path, "docs", files={"reference.md": "ref\n", "examples/one.md": "one\n"})
    store = load(tmp_path)

    assert store.read_file("docs", "reference.md") == ("ref\n", False)
    assert store.read_file("docs", "examples/one.md") == ("one\n", False)
    assert store.read_file("docs", "examples\\one.md") == ("one\n", False)


def test_a_long_file_is_truncated(tmp_path: Path) -> None:
    write_skill(tmp_path, "docs", files={"big.md": "y" * 50})

    store = load(tmp_path)
    # A SKILL.md never fits a 10-character budget, so reuse the loaded skills with one.
    tight = SkillStore(store.skills(), SkillsSettings(enabled=True, file_max_chars=10))

    assert store.read_file("docs", "big.md") == ("y" * 50, False)
    assert tight.read_file("docs", "big.md") == ("y" * 10, True)


@pytest.mark.parametrize("path", ["../other/SKILL.md", "../../secret.txt", "sub/../../x"])
def test_paths_outside_the_skill_are_refused(tmp_path: Path, path: str) -> None:
    write_skill(tmp_path, "docs")
    write_skill(tmp_path, "other")

    with pytest.raises(SkillFileError, match="outside the skill"):
        load(tmp_path).read_file("docs", path)


@pytest.mark.parametrize("path", ["", "   "])
def test_an_empty_path_is_refused(tmp_path: Path, path: str) -> None:
    write_skill(tmp_path, "docs")

    with pytest.raises(SkillFileError, match="relative"):
        load(tmp_path).read_file("docs", path)


def test_an_absolute_path_is_refused(tmp_path: Path) -> None:
    write_skill(tmp_path, "docs", files={"a.md": "a"})

    with pytest.raises(SkillFileError, match="relative"):
        load(tmp_path).read_file("docs", str((tmp_path / "docs" / "a.md").resolve()))


def test_missing_binary_and_non_utf8_files_are_refused(tmp_path: Path) -> None:
    folder = write_skill(tmp_path, "docs")
    (folder / "blob.bin").write_bytes(b"\x00\x01\x02")
    (folder / "latin.txt").write_bytes(b"caf\xe9")
    store = load(tmp_path)

    with pytest.raises(SkillFileError, match="does not exist"):
        store.read_file("docs", "nope.md")
    with pytest.raises(SkillFileError, match="does not exist"):
        store.read_file("docs", "examples")  # not a file
    with pytest.raises(SkillFileError, match="not a text file"):
        store.read_file("docs", "blob.bin")
    with pytest.raises(SkillFileError, match="not a UTF-8"):
        store.read_file("docs", "latin.txt")


def test_an_unknown_skill_lists_the_known_ones(tmp_path: Path) -> None:
    write_skill(tmp_path, "docs")

    with pytest.raises(LookupError, match="Skills: docs"):
        load(tmp_path).read_file("nope", "a.md")


def test_a_link_out_of_the_skill_is_refused(tmp_path: Path) -> None:
    folder = write_skill(tmp_path, "docs")
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    try:
        os.symlink(secret, folder / "link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available")

    with pytest.raises(SkillFileError, match="outside the skill"):
        load(tmp_path).read_file("docs", "link.txt")


# -- catalog -----------------------------------------------------------------------------


def test_the_catalog_lists_name_and_description(tmp_path: Path) -> None:
    write_skill(tmp_path, "b-skill", description="Second.")
    write_skill(tmp_path, "a-skill", description="First.")

    text = SkillCatalog(load(tmp_path)).render()

    assert text == f"{CATALOG_HEADER}\n- a-skill: First.\n- b-skill: Second."
    assert "skill_view" in text


def test_the_catalog_is_empty_without_skills(tmp_path: Path) -> None:
    assert SkillCatalog(load(tmp_path)).render() == ""


def test_a_long_description_is_cut_in_the_catalog(tmp_path: Path) -> None:
    write_skill(tmp_path, "wordy", description="word " * 50)

    text = SkillCatalog(load(tmp_path, SkillsSettings(enabled=True, description_max_chars=20))).render()

    line = text.splitlines()[-1]
    assert line.startswith("- wordy: ") and line.endswith("...")
    assert len(line.removeprefix("- wordy: ")) <= 20


def test_the_catalog_fits_its_budget_and_says_what_it_left_out(tmp_path: Path) -> None:
    for index in range(10):
        write_skill(tmp_path, f"skill-{index}", description="d" * 30)
    budget = len(CATALOG_HEADER) + 3 * 45

    text = SkillCatalog(load(tmp_path, SkillsSettings(enabled=True, catalog_max_chars=budget))).render()

    assert len(text) <= budget
    shown = [line for line in text.splitlines() if line.startswith("- ")]
    assert 0 < len(shown) < 10
    assert text.endswith(f"... {10 - len(shown)} more skills not shown")
