# 2026-08-01 GoalRunner Branch Plan

Branch: `feat/goal-runner`
Source plan: [Codex-Claude Runtime Migration Plan](../research/2026-07-26-codex-claude-runtime-migration-plan.md)
Status: Implemented through Slice 6

## Goal

Add `GoalRunner` as the component responsible for the lifecycle of one user
task.

`GoalRunner` becomes the execution boundary between `SessionRuntime` and the
current engine. In this branch it must not change agent behavior. It should
take over execution coordination, latest-state loader invocation, terminal
event emission, and typed terminal result construction while still delegating
actual agent work to the existing `BrowserHarness`/LangGraph path.
`SessionRuntime` remains the owner of session start, task id allocation,
task-scoped config construction, state persistence into `SessionContext`, and
task record completion or failure.

Target shape for this branch:

```text
SessionRuntime
  -> GoalRunner
      -> existing task_runner
          -> BrowserHarness
              -> LangGraph
```

Future target shape from the research plan:

```text
SessionRuntime
  -> GoalRunner
      -> AgentLoopEngine or LangGraphEngine
```

This branch introduces the boundary needed for that future swap, but keeps the
current engine as the only implementation.

## Migration Plan Analysis

The research plan proposes a broad migration from a graph-owned runtime to an
AutoBrowser-owned runtime:

- explicit session and goal lifecycle;
- durable event stream and traces;
- context assembly from named blocks;
- action, permission, tool, hook, skill, subagent, and eval boundaries;
- eventual replacement of graph orchestration with `AgentLoopEngine`.

Several early migration pieces already exist on the current branch line:

| Migration concern | Current state |
| --- | --- |
| Durable events | `src/agent_loop/events.py` provides `EventRecord`, sinks, and `EventEmitter`. |
| Trace projection | `src/agent_loop/tracing.py` and session JSONL files exist. |
| Scenario/replay foundation | `src/agent_loop/evals.py`, `replay.py`, `metrics.py`, and `export.py` exist. |
| Context assembler | `src/agent_loop/context.py`, `prompts.py`, and `skills.py` exist and are wired through `src/harness/context.py`. |
| Session lifecycle | `SessionRuntime` and `SessionContext` own process lifetime and durable session state. |
| Current execution engine | `BrowserHarness` still composes and invokes the LangGraph agent loop. |

Because eventing and context assembly are already present, `GoalRunner` should
not restart Phase 1 or Phase 3. It should be the missing lifecycle boundary
that lets later branches move action parsing, tool brokering, permissions,
hooks, and engine selection out of `SessionRuntime` without changing the CLI
surface each time.

The important adjustment to the research plan is sequencing: `GoalRunner`
should be introduced before the explicit `AgentLoopEngine`, but after the event
and context foundations. That makes it a compatibility shell around the current
graph and a stable home for per-goal lifecycle rules.

## Current Boundary

Before this branch, `SessionRuntime.run_task()` owned too many per-task
responsibilities:

- starts the session if needed;
- creates the task id;
- calls `SessionContext.reset_task()`;
- builds LangGraph config and session thread id;
- injects carried session state and task-local resets;
- writes event metadata with `session_id`, `task_id`, and `goal_id`;
- emits `goal.started`;
- delegates to the configured task runner;
- captures latest harness state after success or failure;
- marks the task record finished or failed;
- emits `goal.completed` or `goal.failed`;
- returns the raw task runner result.

Implemented boundary after Slice 6:

```text
SessionRuntime
  -> GoalRunner
      -> existing task_runner
          -> BrowserHarness
              -> LangGraph
```

`SessionRuntime.run_task()` now starts the session, allocates `task_id` and
`goal_id`, builds `GoalRunRequest`, prepares task config and carried state
overrides, calls `GoalRunner.run()`, writes returned latest state into
`SessionContext.state`, and marks the `TaskRecord` finished or failed.

`GoalRunner` now emits `goal.started`, delegates to the configured task runner,
invokes the injected `LatestStateLoader`, emits `goal.completed` or
`goal.failed`, and returns or internally constructs `GoalRunResult` with an
explicit terminal `status`.

`GoalRunner` does not consume graph stream chunks. Streaming remains owned by
the task runner adapter in `src/cli/task_runner.py`, which streams from
`BrowserHarness.stream_updates()`.

`BrowserHarness` should continue to own engine invocation:

- preparing initial graph state;
- stripping harness-internal config;
- invoking or streaming the graph;
- emitting graph node events;
- recovering completed state after recursion limit when possible;
- exposing latest checkpoint state through `get_state_values()`.

`SessionRuntime` should remain responsible for process/session lifetime:

- initializing LLM, tools, browser provider, workspace, memory, telemetry, and
  event sinks;
- owning `SessionContext`;
- running the interactive loop;
- closing process-lifetime external resources.

## Target Design

Add `src/agent_loop/goals.py` with a small compatibility runner.

Minimum public shape:

```python
@dataclass(frozen=True)
class GoalRunRequest:
    task: str
    task_id: str
    goal_id: str
    thread_id: str
    config: Mapping[str, Any]
    state_overrides: Mapping[str, object]


@dataclass(frozen=True)
class GoalRunResult:
    task: str
    task_id: str
    goal_id: str
    result: Any
    latest_state: Mapping[str, object] | None
    status: Literal["completed", "failed", "cancelled", "blocked"]
```

Suggested runner shape:

```python
class GoalRunner:
    def __init__(
        self,
        *,
        harness: BrowserHarness,
        task_runner: TaskRunner,
        event_emitter: EventEmitter,
        latest_state_loader: LatestStateLoader,
    ) -> None: ...

    async def run(self, request: GoalRunRequest) -> GoalRunResult: ...
```

The implementation can use project-specific types instead of these exact names
when that better fits imports and test ergonomics. The boundary matters more
than the final dataclass spelling.

Implemented names:

- `GoalRunRequest`, `GoalRunResult`, `GoalRunner`, and `GoalStatus` live in
  `src/agent_loop/goals.py`.
- `TaskRunner` is the callable port used by `GoalRunner` to delegate current
  execution.
- `LatestStateLoader` is the callable port used by `GoalRunner` to capture
  latest graph state.
- `HarnessLatestStateLoader` lives in `src/harness/session.py` and adapts
  `BrowserHarness.get_state_values()` plus result-state fallback conversion to
  the `LatestStateLoader` port.

## Ownership

### SessionRuntime

After this branch, `SessionRuntime.run_task()` should:

1. call `start()`;
2. allocate `task_id` and `goal_id`;
3. create the `TaskRecord` through `SessionContext.reset_task()`;
4. build a `GoalRunRequest`;
5. call `GoalRunner.run()`;
6. copy `latest_state` into `SessionContext.state`;
7. mark the task record finished or failed;
8. return the result or re-raise the original exception.

`SessionRuntime` may still build the request during the first slice to keep the
diff small. A later slice can move request construction into a factory helper.

### GoalRunner

`GoalRunner` should own one-task lifecycle execution:

- emit `goal.started`;
- call the existing task runner with the harness, task text, session config, and
  task config;
- catch failures only to emit `goal.failed`, capture latest state, and preserve
  exception behavior;
- capture latest state after success;
- emit `goal.completed`;
- return a typed result object.

For this branch, `GoalRunner` must not:

- decide whether the task is complete;
- inspect model messages to infer semantic status;
- change graph routing;
- change retry or replan counters;
- change browser snapshot/ref policy;
- call tools directly;
- introduce a new prompt or model loop.

### BrowserHarness

`BrowserHarness` remains the engine adapter. No graph behavior should move into
`GoalRunner` in this branch.

### SessionContext

`SessionContext` remains the durable session state holder. It should not become
the execution coordinator.

## Proposed Files

New source files:

- `src/agent_loop/goals.py`

Changed source files:

- `src/agent_loop/__init__.py`
- `src/harness/session.py`

New test files:

- `tests/test_goal_runner.py`

Changed test files:

- `tests/test_harness_session.py`
- `tests/test_harness_runtime.py` only if helper types or imports require it.

Optional docs follow-up:

- `docs/architecture/overview.md` updated in Slice 6;
- `docs/glossary.md` updated in Slice 6 with stable `GoalRunner`,
  `GoalRunRequest`, `GoalRunResult`, and `LatestStateLoader` names.

## Compatibility Requirements

The branch is correct only if default behavior is preserved:

- same CLI entry points;
- same task result returned by `SessionRuntime.run_task()`;
- same session thread id shape;
- same carried session state and task-local reset behavior;
- same graph config metadata;
- same event types in the same high-level order;
- same task history persistence;
- same exception type and traceback behavior from failing tasks;
- same behavior for `--no-mcp`, `--show-state`, `--hide-snapshot`,
  `--compress-tools`, and streaming task execution.

Event payload shape may gain small additive metadata if tests prove existing
consumers are not broken. Do not rename current event types in this branch.

## Implementation Slices

### Slice 1: Characterization Tests

Deliverables:

- add or tighten tests around the current `SessionRuntime.run_task()` contract;
- assert the task config passed to the task runner contains:
  - session-scoped `thread_id`;
  - harness event metadata with `session_id`, `task_id`, and `goal_id`;
  - harness state overrides from `_task_state_overrides()`;
- assert emitted goal events for success and failure;
- assert latest state is remembered on success and failure when available;
- assert the result and exception behavior remain unchanged.

Exit gate:

- tests fail if `run_task()` lifecycle semantics drift.

### Slice 2: Add GoalRunner Shell

Deliverables:

- create `src/agent_loop/goals.py`;
- add `GoalRunRequest`, `GoalRunResult`, and `GoalRunner`;
- move the task runner call and goal event emission from
  `SessionRuntime.run_task()` into `GoalRunner.run()`;
- keep `_task_state_overrides()` and `_session_thread_id()` where they are unless
  moving them reduces coupling without broad churn.

Exit gate:

- `SessionRuntime.run_task()` still returns the same result;
- success and failure tests pass through `GoalRunner`;
- no graph or prompt tests need expectation changes.

### Slice 3: Move Latest-State Capture Behind A Small Port

Deliverables:

- make latest-state capture explicit as a callable or protocol passed into
  `GoalRunner`;
- keep the actual implementation using `BrowserHarness.get_state_values()`;
- preserve fallback conversion from task result state when no checkpoint is
  available.

Possible shape:

```python
LatestStateLoader = Callable[
    [Mapping[str, Any], Any | None],
    Awaitable[dict[str, object] | None],
]
```

Exit gate:

- `GoalRunner` can be unit-tested with a fake latest-state loader;
- `SessionRuntime` still owns copying the returned state into
  `SessionContext.state`.

### Slice 4: Make Terminal Status Explicit

Deliverables:

- return `GoalRunResult.status == "completed"` on success;
- return or internally construct `status == "failed"` before re-raising on
  failure;
- do not introduce `blocked` or `cancelled` behavior until existing code has a
  real source of those states;
- reserve status literals for future branches without changing behavior now.

Exit gate:

- tests document that failed runs still re-raise;
- terminal event emission remains identical from the user's perspective.

### Slice 5: Preserve Streaming Path

Deliverables:

- verify `src/cli/task_runner.py` still streams from `BrowserHarness` exactly as
  before;
- do not make `GoalRunner` consume graph chunks unless the task runner already
  returns them;
- add one test that proves a streaming task runner can be used unchanged.

Exit gate:

- CLI task execution remains compatible with the existing task runner adapter.

### Slice 6: Documentation And Architecture Notes

Deliverables:

- update this plan with implemented names if they differ;
- add a short architecture note after the code lands, not before;
- optionally add glossary entries for stable names.

Exit gate:

- docs match the implemented boundary and do not claim `GoalRunner` owns the
  model/action loop yet.

Implemented notes:

- this plan records the implemented boundary and stable names;
- `docs/architecture/overview.md` describes the current
  `SessionRuntime -> GoalRunner -> task_runner -> BrowserHarness -> LangGraph`
  path;
- `docs/glossary.md` defines the stable goal runner terms.

## Test Plan

Run focused tests during implementation:

```powershell
python -m pytest tests\test_goal_runner.py tests\test_harness_session.py
python -m pytest tests\test_harness_runtime.py
```

Run broader checks before merging:

```powershell
python -m pytest
git diff --check -- docs
```

If eval tests are stable on the branch, also run:

```powershell
python -m pytest tests\test_agent_loop_evals.py
```

## Acceptance Criteria

The branch is complete when:

- `GoalRunner` exists in `src/agent_loop/goals.py`;
- `SessionRuntime.run_task()` delegates one-task execution to `GoalRunner`;
- the existing LangGraph engine remains the only behavior path;
- durable session and goal events are still emitted;
- latest graph state is still carried into the next task;
- task-local state is still reset between tasks;
- success and failure behavior is covered by focused tests;
- the default CLI behavior is unchanged.

## Risks

- Moving too much into `GoalRunner` can accidentally make it a second harness.
  Keep engine invocation inside `BrowserHarness`.
- Capturing latest state in the wrong layer can break session carry-forward.
  Return latest state from `GoalRunner`; let `SessionRuntime` write it into
  `SessionContext`.
- Renaming `task_id` or `goal_id` semantics can break trace and task history
  consumers. Keep `goal_id == task_id` in this branch.
- Treating graph recursion recovery as goal status logic can change behavior.
  Leave recursion handling in `BrowserHarness`.

## Rollback

Rollback should be simple:

1. route `SessionRuntime.run_task()` back to the previous inline implementation;
2. leave `GoalRunner` unused or remove it if no other branch depends on it;
3. keep tests that characterize session lifecycle behavior.

No data migration is required because this branch should not change event file
locations, session metadata format, or task record format.

## Follow-Up Branches

After `GoalRunner` is stable, later branches can move additional runtime
concepts behind it:

1. engine selection: `LangGraphEngine` vs future `AgentLoopEngine`;
2. typed goal cancellation and blocked status;
3. approval queue coordination;
4. tool broker and permission engine integration;
5. hooks around `GoalStart`, `PreCompletion`, and `GoalEnd`;
6. parent/child goal relationships for bounded subagents.

These should remain follow-ups. The `feat/goal-runner` branch is a boundary
extraction branch, not a behavior migration branch.
