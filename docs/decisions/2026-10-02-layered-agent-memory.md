# ADR: Layered Agent Memory

Status: Accepted
Date: 2026-10-02

## Context

AutoBrowser's memory was one durable `list[Message]` on `LoopState.messages`, shaped by the
functional `MemoryManager` (`src/harness/memory.py`) and carried across the tasks of a REPL
session (ADR 2026-07-25). Superseded snapshots were compacted, but nothing else bounded the
history: short tool results, tool calls and every earlier task of the session stayed in the
prompt verbatim. Knowledge that would help the next session on the same site (a search URL
template, which control applies a filter) was hard-coded in prompts (Ozon in four places) or
lost when the process exited. `memory.max_tool_message_refs` was declared but never read.

The [Memory Harness Research](../research/2026-10-02-memory-harness-research.md) compared the
designs of Claude Code (CLAUDE.md + auto memory), the Claude API memory tool and context
editing, Codex, Letta/MemGPT and Mem0, and proposed four layers. The
[implementation plan](../development/2026-10-02-memory-implementation-plan.md) turned it into
phases.

## Decision

Memory has four layers. Everything past the existing snapshot compaction is **off by
default** and switched on in the `memory` settings section.

1. **L1 working context: history budget.** `MemoryManager.apply_history_budget` runs after
   compaction on every history build. Past `memory.history_budget_chars` it replaces the
   oldest tool outputs (never the newest `keep_recent_tool_results`, never a non-tool message)
   with a `[cleared]` placeholder that keeps the `tool_call_id`. It is deterministic,
   idempotent and server-neutral, with no LLM summarization.
2. **L2 task state: working notes.** With `memory.working_notes_max_chars > 0`, every tool
   offers an optional `notes` argument. It is stripped at parse time (like `approval_request`),
   kept on the task-local `LoopState.working_notes` and rendered as the `Working Notes` block.
   The plan sketched `notes` in the JSON decision; it became a tool argument because the
   model acts through native tool calls, which have no decision JSON.
3. **L3 session: task digest.** At the task boundary the session calls
   `MemoryManager.digest_tasks`. Every finished task older than the newest
   `memory.keep_recent_tasks` becomes one `[harness] Previous task digest` message (request,
   final answer, tool-call counts), so tool-call pairs stay whole and follow-ups still see the
   last task verbatim.
4. **L4 persistent memory.** Markdown files with YAML frontmatter under
   `<storage.root_dir>/<memory.dir>/` (`sites/<domain>.md`, `procedures/<name>.md`), read by
   `MemoryStore` (`src/harness/memory_store.py`) and rendered by `MemoryContext` into a
   `Memory` context block (user role, priority 15, cut to `memory.block_max_chars`).
   - The **index is generated** from frontmatter (`MEMORY.md` is rewritten for humans after
     each write), never written by the model.
   - **Scope** is the normalized host of the current page with suffix matching (no Public
     Suffix List). `*` is allowed only in the user's own files.
   - **Writes** go through two harness-native tools, `memory_view` (`readOnlyHint`) and
     `memory_write` (`create`/`str_replace`/`delete`), on `server: memory`, so permissions,
     hooks and events treat them like any MCP tool. The harness owns the frontmatter: agent
     entries are `unverified` with `source: agent:<task_id>`, and the user's files are
     read-only for the agent. Every write, consolidation included, passes a
     `MemoryContentPolicy` (browser implementation: no refs, no selectors, no injection
     phrases, no secret-like pairs) and the `file_max_chars` limit. Human review is a
     user-configured permission rule, never a shipped one.
   - **Staged trust**: the session records the outcome of each task for the entries that task
     loaded. `done` promotes `unverified` to `verified` after `promote_after_successes`;
     `blocked` marks an entry `stale` after `stale_after_failures`; a `verified` entry older
     than `stale_after_days` renders as `stale`. The user's entries never change.
   - **Consolidation** (opt-in `consolidate_on_goal_end`): after a `done` task, the
     *session* makes one model call that proposes at most three entries, written as
     `unverified` through the same policy. Failures emit `memory.consolidation_failed` and
     never affect the task.

Layering: the engine calls only `resources.memory.render(state) -> str`
(`EngineResources.memory`, defaulting to `NullMemoryContext`). It never opens a memory file,
knows a domain or names a memory tool. A test enforces this. Browser specifics (scope from
the `Page URL`, the content policy) live in `src/browser/memory.py` behind the
`MemoryScopeResolver`/`MemoryContentPolicy` protocols in `src/contracts.py`. The page-URL
resolver is shared with permissions through `src/browser/pages.py`.

`memory.max_tool_message_refs` is removed. An old `.env`/`config.yaml` that still names it
keeps starting: the key is dropped with a `FutureWarning`.

## Consequences

- With every flag at its default, prompts are byte-for-byte unchanged (regression-tested
  against the previous `ContextAssembler`), and scripted evals match the baseline with or
  without the seed memory (`scripts/run_evals.py --memory-seed tests/evals/memory_seed`).
- Persistent memory is data, not configuration: a broken file is skipped with
  `memory.skipped` and never fails startup.
- Memory is a hint layer. The snapshot stays the only source of truth, and unverified or stale
  bodies carry an explicit "verify against the current snapshot" prefix.
- New event types: `memory.skipped`, `memory.outcome`, `memory.consolidated`,
  `memory.consolidation_failed`. Writes are visible through the existing `tool.*` and
  `permission.decided` events.
- Consolidation runs inline after the task (bounded by `consolidation_timeout_seconds`), so
  with it enabled the REPL prompt returns up to that much later.
- Follow-ups: enable the history budget and the task digest by default once live evals on
  the real model justify it (plan commit 6); move the Ozon hints from the prompts into site
  memory only if evals with the seed memory are no worse (plan commit 6b); the `system`-role
  blocks (`Tool Inventory`, `Browser Rules`) still never reach the model (found while
  planning, out of scope here).

## Alternatives Considered

- LLM summarization of the history: non-deterministic, costs a model call per turn, and can
  drop the exact evidence (failed attempts) the progress guard relies on. Rejected for L1/L3.
- Vector / semantic retrieval, external memory servers (Mem0): unnecessary at the scale of
  per-site notes and an extra dependency. Out of scope.
- A model-written `MEMORY.md` index (Claude Code style): it grows without bound and drifts
  from the files. The index is generated instead.
- Built-in review rules for `memory_write`: contradicts the name-free permission defaults
  ADR; shipped as a commented example instead.

## Related

- [Implementation plan](../development/2026-10-02-memory-implementation-plan.md),
  [Memory guide](../development/memory.md), [diagram](../diagrams/memory-layers.md)
- [Session-Scoped Agent Context Memory](2026-07-25-session-scoped-agent-context-memory.md)
  (extended: the carry-forward is now digested at the task boundary)
- [Server-Neutral Progress Journal](2026-09-28-server-neutral-progress-journal.md)
- [Name-Free Permission Defaults](2026-10-01-name-free-permission-defaults.md)
- Code: `src/harness/memory.py`, `src/harness/memory_store.py`, `src/harness/memory_tool.py`,
  `src/harness/memory_consolidation.py`, `src/browser/memory.py`, `src/browser/pages.py`,
  `src/agent_loop/execution/notes.py`
