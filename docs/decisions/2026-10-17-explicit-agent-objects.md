# ADR-2026-10-17: Explicit Agent Objects Along the Ownership Chain

Status: Proposed
Date: 2026-10-17

## Context

The ownership chain `SessionRuntime -> GoalRunner -> native_task_runner -> AgentLoopEngine`
still passes data through the shapes of the deleted compiled-graph runtime:

- **Duplicated task inputs.** `SessionRuntime.run_task` builds a `GoalRunRequest` with
  explicit `task_id`, `goal_id`, `thread_id` and `state_overrides`, and *also* stuffs the same
  values into a `task_config` dict under `HARNESS_EVENT_METADATA_CONFIG_KEY` /
  `HARNESS_STATE_OVERRIDES_CONFIG_KEY` (plus a `configurable.thread_id`). The runner reads them
  back from the dict, not from the request.
- **A graph-era runner signature.** `TaskRunner = Callable[[Any, str, Any, dict], ...]` takes
  `(harness, task, session_config, task_config)`; the native runner ignores `harness` (the
  resources are already bound) and reads `turn_cap` / `compress_tools` with
  `getattr(session_config, ...)`.
- **Untyped collaborators.** `EngineResources.llm`, `context`, `events`, `hooks` and `memory`
  are `Any`; `GoalRunner` takes `harness: Any` and `session_config: Any` and reads its timeouts
  with `getattr`.
- **Leftover indirection.** `native_latest_state_loader` and the session's
  `load_latest_state` closure only unwrap `AgentLoopResult.session_state`, which is always
  there; `src/agent_loop/engine.py` is a re-export shim kept "so existing imports keep
  working"; `SessionRuntime` reads `final_answer` / `state` from the typed `AgentLoopResult`
  with `getattr`.

The [deepagents](https://habr.com/ru/companies/sberbank/articles/1071036/) design is the
reference for the target feel: one factory assembles the agent from explicit objects (model,
tools, backends, middleware, interrupt points), and each layer receives exactly what it uses.
The engine-native loop stays; only the seams change.

## Decision (proposed)

1. **`TaskRequest` replaces `task_config` plumbing.** A frozen dataclass (in
   `src/agent_loop/goals.py`, next to `GoalRunRequest`, which it absorbs):

   ```python
   @dataclass(frozen=True)
   class TaskRequest:
       task: str
       task_id: str              # == goal_id (kept equal by construction)
       session_id: str
       thread_id: str
       state_overrides: Mapping[str, object]
       turn_cap: int
       compress_tools: bool
       metadata: Mapping[str, object]   # model/temperature for events, if still needed
   ```

   `HARNESS_STATE_OVERRIDES_CONFIG_KEY`, `HARNESS_EVENT_METADATA_CONFIG_KEY` and
   `SessionConfig.task_config()` go away once nothing reads them.

2. **`Agent` replaces the `native_task_runner` closure.** An explicit object built once per
   task from `EngineResources`:

   ```python
   class Agent:
       def __init__(self, resources: EngineResources, *, human_input: HumanInputCallback | None = None): ...
       async def run(self, request: TaskRequest) -> AgentLoopResult: ...

   TaskRunner = Callable[[TaskRequest], Awaitable[AgentLoopResult]]   # Agent.run fits
   ```

3. **One composition function for every path.** `build_agent(...)` (in
   `src/harness/`, since it composes harness and browser objects) assembles `EngineResources`
   — tools, normalizers, context, events, hooks, permissions, approval, memory, tool roles —
   and returns an `Agent`. `SessionRuntime`, `src/agent_loop/evals.py` and the batch runner
   call it instead of each calling `EngineResources.from_harness` with their own subset; the
   browser defaults (`BrowserToolRoles`, `BrowserResourceResolver`) are decided in one place.

4. **`GoalRunner` gets typed inputs.** `GoalRunner(agent: TaskRunner, events: EventEmitter,
   limits: GoalLimits)` where `GoalLimits` is a frozen dataclass of the three timeouts
   resolved from settings and `SessionConfig` by the session. No `harness`, no
   `session_config`, no `getattr`. The carried state comes from
   `AgentLoopResult.session_state` directly; `LatestStateLoader`,
   `native_latest_state_loader` and the session closure are deleted. On a failure path the
   session keeps its previous state (today the loader returns `None` there anyway — to be
   verified against the timeout tests).

5. **Protocols instead of `Any` in `EngineResources`.** In `src/contracts.py` (or next to
   their implementations when they need non-leaf types): `llm: ChatModel` (exists in
   `src/llm.py`), `context: PromptContext` (the `ContextAssembler` methods the engine calls),
   `events: EventSink` (`emit`, `sequence`), `hooks: HookRunner` (`has`, `run`),
   `memory: MemoryRenderer` (`render`). Only the methods the engine actually calls.

6. **Dead shims go.** `src/agent_loop/engine.py` (imports move to
   `src.agent_loop.execution.loop` or the package root), `getattr` on `AgentLoopResult` in
   `src/harness/session.py`.

## Consequences

- Each layer's constructor lists exactly its collaborators; a reader sees the data flow
  without following dict keys. `goal_id == task_id` holds by construction.
- Evals, batch and the session share one composition path, which also closes the
  "forgot the tool roles" risk from the [tool roles ADR](2026-10-17-injected-tool-roles.md).
- Large test churn: `tests/test_goal_runner.py` and `tests/test_harness_session.py` build
  runners with the old four-argument signature (about 30 call sites). The change is split into
  commits — protocols; `TaskRequest` + `Agent`; `GoalRunner`/`GoalLimits` and loader removal;
  `build_agent`; shim removal — each with a full test and eval-baseline run.
- Event payloads and `events.jsonl` must stay byte-compatible (`session_id`, `task_id`,
  `goal_id` on every record); replay and exporters are read-only consumers.
- `--show-state` / CLI output that reads `task_config` metadata needs checking.

## Alternatives Considered

- Keep the dict and add typed accessors: hides the duplication instead of removing it.
- Adopt LangGraph/deepagents directly: the native engine was chosen deliberately
  ([2026-08-31](2026-08-31-native-agent-loop-engine.md),
  [2026-09-03](2026-09-03-drop-langchain-stack-provider-neutral-model.md)); only the
  composition style is borrowed.
- A middleware stack replacing hooks/permissions: they already sit at fixed loop points with
  their own ADRs; renaming them is churn without a behavioural gain.

## Related

- [Explicit agent objects plan](../development/2026-10-17-explicit-agent-objects-plan.md),
  commit 6.
- [2026-10-17 Injected Tool Roles](2026-10-17-injected-tool-roles.md)
- [2026-07-24 SessionContext Root Object](2026-07-24-session-context-root-object.md)
- Code: `src/harness/session.py` (`run_task`), `src/agent_loop/goals.py`,
  `src/agent_loop/execution/loop.py` (`native_task_runner`),
  `src/agent_loop/execution/resources.py`, `src/agent_loop/execution/completion.py`,
  `src/agent_loop/engine.py`, `src/harness/runtime.py` (`HARNESS_*_CONFIG_KEY`).
