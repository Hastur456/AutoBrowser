# SessionContext Root Object

Status: Accepted
Date: 2026-07-24

## Context

`SessionRuntime` already made AutoBrowser a long-lived process session, but it
still owned session resources as separate fields. As more session-level state
appears, spreading task history, workspace paths, telemetry, tools, and runtime
handles across the runtime class makes lifecycle ownership harder to test and
extend.

AutoBrowser is task-oriented, not a chat runtime. The session needs to track
current and completed tasks without introducing conversation turns or a service
locator.

## Decision

Introduce `SessionContext` in `src/harness/session.py` as the root object for
one process-scoped session. `SessionRuntime` now creates one context and
delegates session lifecycle to it.

`SessionContext` owns:

- `SessionConfig`, `session_id`, `SessionMetadata`, and `SessionState`.
- `TaskRecord` history plus `current_task`.
- Session workspace under `.autobrowser/sessions/<session_id>/workspace/`.
- `ArtifactRegistry` and `SessionEventBus`.
- Runtime handles for LLM, `BrowserHarness`, `MemoryManager`, `ToolRegistry`,
  and `TelemetryObserver`.

`BrowserHarness` remains the graph composition boundary. The compiled LangGraph
agent state and node contracts are unchanged.

## Consequences

- Session lifecycle now has explicit `initialize()`, `reset_task()`,
  `finish_task()`, `fail_task()`, and `close()` methods.
- Runtime-local files have a stable workspace layout with `downloads/`,
  `screenshots/`, `temp/`, and `artifacts/`.
- Future tools can register artifacts and listen to session events without
  searching through unrelated runtime state.
- `SessionState` wraps a mapping so the implementation can change without
  exposing a raw `dict[Any]` contract.
- `SessionConfig.task_config()` remains derived config and is not stored in
  session metadata.

## Alternatives Considered

- Keep fields directly on `SessionRuntime`: rejected because it keeps growing
  the runtime class as a resource bag instead of a lifecycle coordinator.
- Add `ConversationContext`: rejected because AutoBrowser executes tasks rather
  than modeling chat turns.
- Add a service locator: rejected because hidden dependencies would make tests
  and runtime boundaries less explicit.
- Add capabilities, browser resource registry, or cancellation token now:
  deferred until there are concrete consumers.

## Related

- `src/harness/session.py`
- `tests/test_harness_session.py`
- [Long-Lived Session Runtime](2026-07-23-long-lived-session-runtime.md)
- [Architecture Overview](../architecture/overview.md)
- [Session Runtime Sequence](../diagrams/session-runtime-sequence.md)
