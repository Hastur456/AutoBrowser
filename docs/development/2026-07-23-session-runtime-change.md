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
