# Memory Guide

How AutoBrowser remembers: what each memory layer does, how to switch it on, how to write
memory files by hand, how the agent writes them, and how to debug what the model saw. The
decision record is [ADR-2026-10-02: Layered Agent Memory](../decisions/2026-10-02-layered-agent-memory.md);
the background is the [Memory Harness Research](../research/2026-10-02-memory-harness-research.md)
and the [implementation plan](../development/2026-10-02-memory-implementation-plan.md).

## Layers at a Glance

| Layer | What | Where | Switch (default) |
|---|---|---|---|
| L1 working context | snapshot compaction + history budget (`[cleared]` old tool outputs) | `MemoryManager` in `src/harness/memory.py` | `memory.history_budget_chars` (`0` = off) |
| L2 task state | plan, `Action History`, working notes | `LoopState`, `src/agent_loop/execution/notes.py` | `memory.working_notes_max_chars` (`0` = off) |
| L3 session | task digest of older tasks at the task boundary | `MemoryManager.digest_tasks`, called by `SessionRuntime` | `memory.keep_recent_tasks` (`0` = off) |
| L4 persistent | site / procedure files, `Memory` block, memory tools | `src/harness/memory_store.py`, `memory_tool.py`, `memory_consolidation.py` | `memory.persistent_enabled`, `tool_enabled`, `consolidate_on_goal_end` (all `false`) |

With every switch at its default, the prompts are byte-for-byte what they were before memory
existed.

## Turning It On

A profile in `config.yaml` (or the matching `AUTOBROWSER_MEMORY__*` variables):

```yaml
memory:
  history_budget_chars: 60000     # clear old tool outputs past ~60k characters of history
  keep_recent_tool_results: 3
  keep_recent_tasks: 1            # older tasks of the session become one digest each
  persistent_enabled: true        # load .autobrowser/memory/ into a "Memory" block
  tool_enabled: true              # let the agent read/write memory (memory_view/memory_write)
  consolidate_on_goal_end: false  # extra model call after each successful task
  working_notes_max_chars: 1500   # the model may keep task-local notes
permissions:
  rules:
    - {id: memory-write-review, decision: ask, server: memory, tool: memory_write}
```

The review rule is recommended while you get to know what the agent writes: every
`memory_write` then asks you first (`once`/`session`/`deny`). Without it, writes run like any
other allowed tool call. In `read_only` permission mode `memory_view` runs and `memory_write`
is denied.

## Persistent Memory Files

Layout under `<storage.root_dir>/<memory.dir>/` (`.autobrowser/memory/`, git-ignored):

```text
MEMORY.md                 generated index; rewritten after every write, never read back
sites/<domain>.md         one site and its subdomains (sites/ozon.ru.md → seller.ozon.ru too)
procedures/<name>.md      a reusable procedure (scope it to a domain, or "*" in your own files)
```

A file is YAML frontmatter plus a markdown body:

```markdown
---
scope: ozon.ru            # optional for sites/ (taken from the file name)
status: user              # user | verified | unverified | stale
source: user              # user | agent:<task_id>
description: Ozon — search URL and the price-filter fallback   # one line for the index
---
- Direct search: https://www.ozon.ru/search/?text=<url-encoded query>
```

- A file **without frontmatter** is yours (`status: user`, description from the first line).
- **Your files** (`source: user`) are never changed, deleted or demoted by the agent, by
  staged trust or by consolidation.
- A file with broken YAML or an unknown `status`/`kind` is skipped with a `memory.skipped`
  event; startup never fails because of memory.
- Edits are picked up on the next turn (the store re-reads a file when its mtime or size
  changes).
- A ready example is `tests/evals/memory_seed/sites/ozon.ru.md` (the eval seed): copy it to
  `.autobrowser/memory/sites/`.

What to write: URL templates, the visible names and roles of controls ("textbox Search",
"button Show results"), the order of steps that worked, site behavior that costs turns. What
not to write (the agent's writes are refused, see below): element refs, CSS/XPath, form
values, personal data, secrets, instructions to the agent.

## What the Model Sees

When persistent memory is on, every agent turn gets a `Memory` block between `Task` and `Plan`:

```text
Memory:
Persistent memory (hints from earlier sessions; the current snapshot always wins):
<one line about memory_view / memory_write — only when the tools are registered>
Index:
- procedures/search.md [user] — Searching a shop
- sites/ozon.ru.md [verified] — Ozon search
For ozon.ru:
### sites/ozon.ru.md [verified]
<body>
```

- The **index** lists every entry (cut to `index_max_lines` / `index_max_chars`).
- **Bodies** appear only for the current site: the host of the latest `- Page URL:` (tool
  output first, then the snapshot), matched exactly or as a parent domain. `*` entries (your
  files only) appear everywhere. The planner sees the index only, because there is no page yet.
- `unverified` and `stale` bodies start with `[unverified — verify against the current snapshot]`.
- The block is cut to `block_max_chars`: unverified/stale bodies first, then verified, yours
  last. Each body is capped at `file_max_chars`.

## How the Agent Writes

`memory_view` (`readOnlyHint`): no `path` returns the index, a `path` returns one entry
(optionally `view_range: [first, last]` lines). `memory_write`:

| `command` | Arguments | Effect |
|---|---|---|
| `create` | `path`, `description`, `body`, optional `scope` | create or overwrite **its own** entry |
| `str_replace` | `path`, `old_str`, `new_str` | `old_str` must occur exactly once |
| `delete` | `path` | delete its own entry |

The harness stamps the frontmatter (`status: unverified`, `source: agent:<task_id>`); the
model never sees or edits it. A refused write is a tool error the model reads (the turn goes
on): unsafe path (`..`, absolute, `\`, percent-encoding, outside `sites/`/`procedures/`),
your file, body over `file_max_chars`, `*` scope, or a `BrowserMemoryPolicy` violation (refs,
selectors, injection phrases, `password: …`-style secrets).

## Staged Trust

After each task the session updates the entries the task **actually loaded** (rendered into
its `Memory` block):

| Task outcome | Effect on agent entries |
|---|---|
| `done` | `uses += 1`, `failures = 0`; `unverified` → `verified` at `promote_after_successes`; `stale` → `unverified` (starts over); `verified` refreshes `verified_at` |
| `blocked` | `failures += 1`; at `stale_after_failures` → `stale` |
| `cancelled`, failed run | nothing |

A `verified` entry whose `verified_at` is older than `stale_after_days` renders as `stale`
(the file is not rewritten); the next successful task re-verifies it. Changes emit
`memory.outcome`.

## Consolidation

With `consolidate_on_goal_end: true`, after a `done` task the session makes one model call
(prompt `MEMORY_CONSOLIDATION_SYSTEM_PROMPT` in `src/agent_loop/prompts.py`) with the task,
the final answer, the action journal (no raw snapshots), the sites visited and the current
index. The model answers with up to three `{path, description, body}` entries. Each goes
through the same `create` path as `memory_write` (policy, limits, `unverified`); entries that
are yours or already `verified` are left alone. The outcome is `memory.consolidated`
(`written`, `rejected` with reasons) or `memory.consolidation_failed` (error, timeout,
unreadable JSON). The task result is never affected. The call runs inline, bounded by
`consolidation_timeout_seconds`.

## History Budget and Task Digest

- **Budget** (`history_budget_chars`): after snapshot compaction, if the history (message
  contents plus tool-call arguments as JSON) is still over budget, the oldest tool outputs are
  replaced with `[cleared] <tool> output from an earlier step (<n> chars) was removed to fit
  the context budget.` until it fits. The newest `keep_recent_tool_results` outputs, every
  non-tool message, and already compacted or cleared outputs are never touched. The
  `Action History` block still shows every call's outcome.
- **Digest** (`keep_recent_tasks`): when a new task starts, every finished task except the
  newest `keep_recent_tasks` becomes one message:

  ```text
  [harness] Previous task digest:
  - request: find a kettle under 2000 ₽
  - answer: Kettle A, 1990 ₽ …
  - tools used: browser_navigate×2, browser_click×1
  ```

  With `keep_recent_tasks ≥ 1` a follow-up ("open the first one") still sees the previous
  task verbatim. The current snapshot is carried separately anyway.

## Working Notes

With `working_notes_max_chars > 0` every tool the model sees offers an optional `notes`
argument ("facts found so far, what is left"). The loop strips it before the history, hooks,
permissions and the tool see the call, keeps the latest value (cut to the limit) on
`LoopState.working_notes`, and renders it as the `Working Notes` block after `Action History`.
Omitting `notes` keeps the previous notes. Notes are task-local: reset at the task boundary,
never carried across tasks.

## Debugging

- `session.json` → `"memory": {"enabled", "root", "entries", "tools"}`.
- `events.jsonl`: `memory.skipped` (a broken file), `memory.outcome` (staged trust),
  `memory.consolidated` / `memory.consolidation_failed`, plus the normal `tool.started` /
  `tool.finished` / `permission.decided` for `memory_view` / `memory_write`.
- `python main.py --show-state --task "..."` shows the turn state; the `Memory` block is part
  of the per-turn user prompt.
- Compare prompt sizes with and without memory on the scripted scenarios:
  `python scripts/run_evals.py --memory-seed tests/evals/memory_seed`.

## Testing Rules

Tests and evals never read memory or its settings through `get_settings()` (a personal
`config.yaml` would leak in): build `MemoryStore(tmp_path, MemorySettings(...))`, or
`tests.evals.runner.seed_memory()`. The engine must not import `memory_store`/`memory_tool`/
`src.browser.memory` or name the memory tools (`tests/test_memory_boundaries.py`). Tests:
`tests/test_harness_memory.py` (budget, digest, pair validity), `tests/test_memory_store.py`,
`tests/test_memory_tool.py`, `tests/test_memory_consolidation.py`,
`tests/test_working_notes.py`, `tests/test_browser_memory.py`, the memory section of
`tests/test_harness_session.py` and `tests/test_context_assembler.py`.
