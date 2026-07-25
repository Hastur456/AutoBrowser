# 2026-07-23 Session Runtime Change

This note records the code changes from the long-lived session runtime update.

## What Changed

- Added `src/harness/session.py` with `SessionConfig` and `SessionRuntime`.
- Refactored `main.run_agent()` so it creates a session, resolves an optional
  startup task, and then stays in the session prompt.
- Kept `run_task()` as the per-task CLI output adapter around
  `BrowserHarness.run()` and `BrowserHarness.stream_updates()`.
- Updated CLI tests so a terminal agent result no longer implies process exit.

## Runtime Behavior

- `SessionRuntime.start()` delegates initialization to `SessionContext`, which
  creates the model, optional Chrome/CDP and MCP tools, memory, tool registry,
  telemetry, workspace, and one `BrowserHarness`.
- `SessionRuntime.run_forever()` delegates each user request to the existing
  agent implementation and then prompts again.
- `SessionRuntime.run_task()` records a task-oriented `TaskRecord`, updates
  `current_task`, and records success or failure through the context lifecycle.
- `SessionRuntime.close()` delegates cleanup to `SessionContext.close()` and
  closes the MCP session when browser tools were enabled.

## 2026-07-24 Follow-Up

- Added `SessionContext` as the root object for one process session.
- Added `SessionState`, `SessionMetadata`, `TaskRecord`, `WorkspaceContext`,
  `ArtifactRegistry`, and `SessionEventBus`.
- Added the runtime-local workspace layout:
  `.autobrowser/sessions/<session_id>/workspace/`.
- Added durable session files under `.autobrowser/sessions/<session_id>/`:
  `session.json` for the full session view and `tasks.json` for task history.
- Added per-task `thread_id` assignment on `TaskRecord` and cleanup of
  LangGraph checkpoints for completed or failed task threads through
  `MemoryManager.delete_thread()`.
- Kept `BrowserHarness` as the graph composition boundary and kept LangGraph
  state contracts unchanged.
- Avoided `ConversationContext` and service locator patterns; session activity
  is task-oriented.

## Verification

The implementation was checked with:

```powershell
python -m pytest tests\test_main_cli.py tests\test_harness_runtime.py
python -m pytest
python -m py_compile main.py src\harness\session.py
```

At the time of the 2026-07-23 change, the full suite passed with 91 tests.
After the 2026-07-24 `SessionContext` update, the full suite passed with 97
tests.
After the session persistence and task-memory isolation update, the full suite
passed with 97 tests.

## 2026-07-25 Follow-Up

- Replaced task-scoped LangGraph thread IDs with one session-scoped thread ID
  derived from `SessionContext.session_id`.
- Stopped deleting graph checkpoint memory after each task so follow-up tasks
  can reuse prior observations, snapshots, browser state, and message history.
- Added task-boundary state preparation: carry durable/session-useful fields
  and reset task-local plan, decision, final answer, tool, policy, error, and
  retry fields.
- Added `task_id` to graph state so each user request is appended as a distinct
  human turn in durable message history without duplicating turns.
- Added a harness-owned state override channel in `BrowserHarness`; the
  internal key is removed before LangGraph receives config.

After the session-scoped agent context memory update, the full suite passed
with 100 tests.
