# Task Memory Isolation and Session Persistence

Status: Superseded by [Session-Scoped Agent Context Memory](2026-07-25-session-scoped-agent-context-memory.md)
Date: 2026-07-24

Supersession note, 2026-07-25: task-specific checkpoint cleanup was replaced
by a session-scoped LangGraph thread so follow-up tasks can use previous
observations, snapshots, browser state, and partial progress from the same
interactive session. This record is preserved as historical context.

## Context

`SessionRuntime` keeps AutoBrowser alive across multiple user tasks in one
process. That is useful for MCP/browser lifecycle, but it also means
session-owned infrastructure lives longer than a single graph execution.

`BrowserHarness` uses `MemoryManager` to provide a LangGraph checkpointer. Even
when each task uses a distinct `thread_id`, an in-memory saver can retain
checkpoints, messages, snapshots, and tool observations for completed task
threads until the process exits. At the same time, `.autobrowser` already
creates a session workspace, but without durable session records the runtime
folders are hard to inspect after a run.

## Decision

Keep the process-long session model, but isolate task graph memory from session
records:

- Generate a task-specific `thread_id` for each `SessionRuntime.run_task()` call.
- Store that ID on the task's `TaskRecord`.
- Pass the task ID into LangGraph config as `configurable.thread_id`.
- After task success or failure, call `MemoryManager.delete_thread(task_id)` so
  supported checkpointers can delete task checkpoints.
- Persist session metadata under `.autobrowser/sessions/<session_id>/`:
  `session.json` for the full session view and `tasks.json` for task history.

The persistent session files are runtime artifacts, not source files.

## Consequences

- A long-lived session can keep MCP/browser resources and one harness alive
  without retaining completed task checkpoints in the active in-memory saver.
- Debugging has a durable session trail in `.autobrowser` even after in-memory
  task checkpoints are removed.
- Checkpoint cleanup is best-effort: custom savers without `delete_thread()` or
  `adelete_thread()` simply keep their own retention behavior.
- Task records now carry task IDs, so external tooling can correlate persisted
  task history with LangGraph thread IDs during a run.

## Alternatives Considered

- Rebuild `BrowserHarness` and `MemoryManager` for every task: rejected because
  it would weaken the long-lived session boundary and reload resources that are
  intentionally process-scoped.
- Keep all checkpoints for the full session: rejected because completed task
  message/snapshot history can accumulate and make session memory behavior
  harder to reason about.
- Persist full LangGraph checkpoints under `.autobrowser`: deferred until resume
  semantics are designed. Current persistence records session/task metadata
  only.

## Related

- `src/harness/session.py`
- `src/harness/memory.py`
- `tests/test_harness_session.py`
- `tests/test_harness_runtime.py`
- [SessionContext Root Object](2026-07-24-session-context-root-object.md)
- [Architecture Overview](../architecture/overview.md)
- [Session Runtime Sequence](../diagrams/session-runtime-sequence.md)
