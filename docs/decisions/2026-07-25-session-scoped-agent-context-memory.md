# Session-Scoped Agent Context Memory

Status: Accepted
Date: 2026-07-25

## Context

AutoBrowser runs multiple user tasks inside one process session. The previous
memory model intentionally isolated each task by generating a task-specific
LangGraph `thread_id` and deleting that thread's checkpoints after the task
finished or failed.

That isolation protected the in-memory saver from accumulating completed task
checkpoints, but it also made follow-up tasks lose useful context from the same
interactive session:

- prior tool observations and extracted result lists;
- current browser snapshot and visible refs;
- page state after navigation or tab-opening actions;
- partial progress after tool errors or recursion-boundary recovery.

This broke multi-step scenarios where the user refers to earlier work, such as
"open the first product" after a search task.

## Decision

Use one session-scoped LangGraph thread for all tasks in a `SessionRuntime`
instance.

`SessionRuntime.run_task()` now passes a stable
`configurable.thread_id` derived from `SessionContext.session_id`. The latest
graph checkpoint state is remembered in `SessionContext.state` after a task
finishes and, when available, after a task fails.

At the boundary between tasks, `SessionRuntime` carries only session-useful
state into the next graph invocation:

- durable message history;
- latest observation;
- latest browser snapshot and browser state;
- last browser action metadata needed for snapshot freshness and ineffective
  action checks.

Task-local fields are reset before the next task starts:

- plan and current step;
- decision, final answer, tool request/result, policy state, and errors;
- retry, replan, failure, repeated-action, and unchanged-snapshot counters.

Each task still gets a generated `TaskRecord.task_id`. That ID is added to
graph state as `task_id` so message history can append one distinct human turn
per user request without duplicating the same task message.

`BrowserHarness` accepts an internal state-override config key for harness-owned
session state injection and strips that key before invoking LangGraph.

## Consequences

- Follow-up tasks can use prior search results, URLs, snapshots, refs, and
  browser progress from the same session.
- A failed or partially completed task can leave useful state for the next
  instruction instead of forcing the agent to start from scratch.
- The active in-memory checkpointer retains the session thread for the process
  lifetime. This is intentional; cleanup now happens when the session is reset
  or the process exits, not after each task.
- Task records still provide per-task audit history in `.autobrowser`, but
  task IDs no longer double as LangGraph checkpoint thread IDs.
- The session layer remains a lifecycle and context boundary. It does not solve
  browser tasks; it prepares graph state for the next task execution.

## Alternatives Considered

- Keep per-task thread cleanup and store only summaries in `SessionState`:
  rejected because the agent already uses durable `messages`, snapshots, and
  observation state as its working context.
- Rebuild `BrowserHarness` per task and manually replay history: rejected
  because harness, tools, and MCP resources are intentionally process-scoped.
- Keep all previous task fields unmodified in the next invocation: rejected
  because terminal decisions, stale plans, final answers, and retry counters
  would make the next task look already completed or already failed.

## Related

- `src/harness/session.py`
- `src/harness/runtime.py`
- `src/harness/memory.py`
- `src/agent/state.py`
- `src/agent/subgraphs/planner/state.py`
- `tests/test_harness_session.py`
- `tests/test_harness_runtime.py`
- [Task Memory Isolation and Session Persistence](2026-07-24-task-memory-isolation-and-session-persistence.md)
- [Session Runtime Sequence](../diagrams/session-runtime-sequence.md)
- [Architecture Overview](../architecture/overview.md)
