# Memory Layers

Where each memory layer acts: on every history build and turn prompt (L1, L2, L4 read), at the
task boundary (L3) and after `goal_end` (staged trust, consolidation). See the
[Memory guide](../development/memory.md) and
[ADR-2026-10-02: Layered Agent Memory](../decisions/2026-10-02-layered-agent-memory.md).

## Context Assembly (Every Turn)

```mermaid
flowchart TD
  State[LoopState] --> History["_history: MemoryManager.ensure_history"]
  History --> Compact[compact_snapshot_history: superseded outputs -> compacted]
  Compact --> Budget["apply_history_budget: oldest tool outputs -> cleared (L1)"]
  State --> Mapping[_prompt_mapping]
  Mapping --> Render["resources.memory.render(mapping)"]
  Render -->|NullMemoryContext| Empty["(no block)"]
  Render -->|MemoryContext| Scope[BrowserMemoryScope: host of Page URL]
  Scope --> Store[MemoryStore: entries, index, entries_for_scope]
  Store --> Block["Memory block: header, index, bodies for the site (L4)"]
  Mapping --> Notes["Working Notes block (L2)"]
  Mapping --> Journal[Action History block]
  Block --> Prompt[ContextAssembler.user_turn_prompt]
  Notes --> Prompt
  Journal --> Prompt
  Budget --> Model[model call]
  Prompt --> Model
  Model -->|"tool call + notes arg"| Split[split_notes -> LoopState.working_notes]
  Model -->|memory_view / memory_write| Pipeline[normal tool pipeline: hooks, PermissionEngine, broker]
  Pipeline --> Write["MemoryStore.create / str_replace / delete: policy, limits, unverified"]
```

## Task Boundary and Goal End

```mermaid
sequenceDiagram
  participant Session as SessionRuntime
  participant Store as MemoryStore
  participant Runner as GoalRunner / engine
  participant LLM as ChatModel

  Session->>Store: bind_task(task_id)
  Session->>Session: _task_state_overrides: MemoryManager.digest_tasks(messages) (L3)
  Session->>Runner: run(task, overrides, resources.memory)
  Runner-->>Store: note_loaded(task_id, rendered paths) on every turn
  Runner-->>Session: GoalRunResult(status, result)
  Session->>Store: record_outcome(task_id, done | blocked | cancelled)
  Note over Store: promote / stale / refresh, user entries untouched
  opt consolidate_on_goal_end and done
    Session->>LLM: MEMORY_CONSOLIDATION prompt (task, answer, journal, sites, index)
    LLM-->>Session: {"entries": [...]} (max 3)
    Session->>Store: create(...) each, policy-checked, unverified
  end
```
