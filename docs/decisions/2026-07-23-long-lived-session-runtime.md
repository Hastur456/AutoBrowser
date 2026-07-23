# Long-Lived Session Runtime

Status: Accepted
Date: 2026-07-23

## Context

AutoBrowser previously treated a CLI invocation as the lifetime of one agent
task. When the agent graph reached a terminal state, `main.run_agent()` returned
and process-scoped resources such as MCP connections, browser runtime, tool
registry, memory, context, and telemetry were torn down.

The application now needs to accept many independent user tasks without
restarting the process. The agent should remain focused on solving one task,
while a separate runtime owns the interaction lifecycle.

## Decision

Introduce `SessionRuntime` in `src/harness/session.py` as the long-lived
application lifecycle boundary. `SessionRuntime` initializes process-scoped
resources once, builds one `BrowserHarness`, delegates each user request through
the existing task runner, and returns to the prompt after each terminal task
state.

`BrowserHarness` remains the per-task graph composition boundary. It continues
to inject context, memory, tools, policy, telemetry, and the compiled LangGraph
agent without taking ownership of CLI prompting or process lifetime.

## Consequences

- CLI startup with an initial task now runs that task and then continues
  accepting new tasks.
- `--loop` remains accepted for compatibility, but the session loop is now the
  default CLI behavior.
- MCP and browser resources are opened once per process session and closed when
  the session exits.
- Future session-level features such as queued tasks, history views, resume, or
  alternate frontends can extend the session boundary without changing the
  single-task agent workflow.

## Alternatives Considered

- Keep loop logic in `main.run_agent()`: rejected because it couples CLI
  prompting, MCP ownership, harness construction, and task execution in one
  function.
- Make the LangGraph agent handle multiple tasks: rejected because it would mix
  application lifecycle with task-solving state and make terminal agent states
  ambiguous.

## Related

- `src/harness/session.py`
- `src/harness/runtime.py`
- `main.py`
- [Architecture Overview](../architecture/overview.md)
- [Session Runtime Sequence](../diagrams/session-runtime-sequence.md)
