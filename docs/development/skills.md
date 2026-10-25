# Agent Skills Guide

Skills are folders of instructions in the Agent Skills `SKILL.md` format. The model sees a
catalog of them (name and description) and loads a skill's body, and the files it points to,
itself with the `skill_view` tool when a task fits. Design:
[2026-10-26-agent-skills-design.md](2026-10-26-agent-skills-design.md); decision:
[ADR-2026-10-26](../decisions/2026-10-26-agent-skills.md).

## What a skill is

```text
<storage.root_dir>/skills/          # your skills (default: ~/.autobrowser/skills/)
  pdf-forms/
    SKILL.md                        # required
    reference.md                    # optional files the body points to
    examples/invoice.md
```

```markdown
---
name: pdf-forms
description: Fill and check PDF forms. Use when a task involves a PDF form.
---
# PDF forms

1. Read reference.md for the field names.
2. ...
```

- `name` — 1–64 lowercase letters, digits and single hyphens; must equal the folder name.
- `description` — what the skill does and when to use it. This is all the model sees until it
  loads the skill, so write it for that choice.
- `license`, `compatibility`, `allowed-tools` are accepted and ignored (`allowed-tools` grants
  nothing: authorization stays with the [permission rules](permissions.md)).
- AutoBrowser extensions are string keys under `metadata`:

| Key | Meaning |
| --- | --- |
| `always: "true"` | The body goes into every system prompt (no loading). Limited by `skills.always_max_chars`. |
| `requires: browser` | Only sessions with a browser MCP server see the skill. `browser` is the only capability. |
| `title: …` | Heading of an always-on block (default: the name). |

Skills from other Agent Skills clients work unchanged. Scripts in a skill folder can be read
as text but are never executed.

## Built-in skills

They live in the package, `src/agent_loop/resources/skills/`. The first one is
`browser-agent-rules` (`always`, `requires: browser`, title `Browser Rules`) — the browser
rules every browser session sends to the model. Its reference copy is
[browser-agent-rules.md](browser-agent-rules.md); both files must stay byte-identical
(`tests/test_packaging.py`).

A skill of yours with the same name replaces the built-in one, so you can keep your own
browser rules in `~/.autobrowser/skills/browser-agent-rules/SKILL.md` (keep its `always` and
`title` metadata, or it becomes an on-demand skill). Delete the folder to go back.

## Turning skills on

Off by default. Always-on built-in skills work either way; enabling adds the catalog, the
`skill_view` tool and your folder:

```yaml
skills:
  enabled: true
  dir: skills            # relative to storage.root_dir; an absolute path works too
```

Every limit (`max_skills`, `catalog_max_chars`, `description_max_chars`, `body_max_chars`,
`file_max_chars`, `always_max_chars`) is in the `skills` section; see `config.example.yaml`.
Skills are read once at session start — restart the session after editing one.

## What the model gets

- System prompt: the agent prompt, then a `Skills` block (with skills on), then each always-on
  body under its title.
- `skill_view(name)` — the numbered body; `skill_view(name, path="reference.md")` — a file in
  the skill folder; `view_range: [first, last]` — a line range. Paths outside the folder,
  missing, binary and non-UTF-8 files are tool errors the model reads.
- `skill_view` is `readOnlyHint` (allowed in `read_only` mode) on server `skills`, so a rule can
  address it, e.g. `{id: no-skills, decision: deny, server: skills}`.

Nothing picks a skill for the model: no matching on task words, URLs or tool names.

## Skills vs memory procedures

Skills are written by you, trusted and never changed by the agent. Memory procedures
(`memory/procedures/*.md`) are written by the agent and go through staged trust
([memory guide](memory.md)). No content policy applies to skills: a skill copied from the
internet is text you chose to trust — read it first.

## Debugging

- `autobrowser doctor` prints a `skills` line (on/off, counts, your folder, which built-ins you
  replaced) and one warning per skipped skill with the reason, or per unknown frontmatter key.
- Events: `skills.invalid` (a skipped skill and why), `skills.shadowed` (yours replaced a
  built-in); `session.started` and `session.json` carry a `skills` snapshot. A loaded body is
  an ordinary `tool.started`/`tool.finished` pair for `skill_view`.
- A YAML gotcha: unquoted `on`, `off`, `yes`, `no` are booleans, so `name: on` is invalid.

## Tests

```powershell
uv run pytest tests\test_skills.py tests\test_skill_tool.py tests\test_session_skills.py tests\test_skill_boundaries.py
```

Tests build `SkillStore.load(SkillsSettings(...), roots=[...])` over `tmp_path`, never
`get_settings()`. `tests/test_skill_boundaries.py` keeps the engine and the assembler free of
skill modules and the tool name, keeps limits in the settings and checks every built-in skill.
