# Codex-Claude Runtime Migration Plan

Status: Research
Date: 2026-07-26

## Purpose

This plan describes how to migrate AutoBrowser from a LangGraph-centered
browser agent into an AutoBrowser-owned agent runtime inspired by the structure
of Codex and Claude Code.

The goal is not to copy their specialization. The goal is to adopt the runtime
architecture patterns that make those agents reliable:

- explicit session and goal lifecycle;
- structured model/action loop;
- durable event stream and transcripts;
- context assembly from layered sources;
- tool broker with permissions around every action;
- hooks for deterministic lifecycle behavior;
- skills for reusable workflows;
- bounded subagents for noisy parallel work;
- evals and rollback gates before increasing autonomy.

## Baseline

AutoBrowser currently has a working graph-first loop:

```text
SessionRuntime
  -> BrowserHarness
      -> LangGraph
          plan -> agent -> policy -> executor -> observe -> agent
```

This is useful and should not be thrown away. The migration should wrap and
then replace pieces incrementally.

Current strengths to preserve:

- `SessionRuntime` owns process-long sessions.
- `BrowserHarness` injects model, memory, tools, policy, and telemetry.
- `ToolRegistry` provides a provider boundary for tools.
- `PolicyEngine` already gates tool requests.
- `human_input_node` already models human approval.
- `observe_node` already compiles tool results into useful state.
- `FakeBrowserProvider` enables deterministic browser tests.

Current limits to remove:

- the graph is the primary runtime abstraction;
- `AgentState` mixes session, goal, browser, retry, policy, and tool fields;
- the main prompt carries too many hard rules;
- policy is marker-based rather than scope/config based;
- streamed updates are not a durable event log;
- skills, hooks, subagents, and tool permissions are not first-class runtime
  concepts;
- browser behavior is tested mostly through unit tests, not replayable
  scenario traces.

## Target Runtime Shape

Introduce an explicit runtime package and move graph execution behind it:

```text
src/agent_loop/
  actions.py
  engine.py
  events.py
  goals.py
  context.py
  model.py
  tools.py
  permissions.py
  approvals.py
  hooks.py
  memory.py
  skills.py
  subagents.py
  evals.py
```

Target flow:

```text
SessionRuntime
  -> GoalRunner
      -> ContextAssembler
      -> ModelDriver
      -> ActionParser
      -> PermissionEngine
      -> ApprovalQueue
      -> ToolBroker
      -> ObservationCompiler
      -> MemoryStore
      -> EventStream
      -> CompletionGuard
```

The model proposes actions. The runtime owns state transitions.

```text
model output -> ProposedAction
ProposedAction -> PermissionDecision
approved action -> ToolResult
ToolResult -> Observation
Observation -> GoalState update
GoalState -> continue, done, blocked, cancelled, or delegated
```

## Migration Rules

1. Keep the existing LangGraph path working until the new runtime beats it on
   scenario evals.
2. Add the new runtime as a shell around the current graph first.
3. Move invariants out of prompts only when they have policy, hook, observer, or
   eval coverage.
4. Put every behavior change behind a feature flag or explicit CLI option until
   it is verified.
5. Treat traces as the source of truth for debugging and eval generation.
6. Do not introduce subagents before eventing, permissions, and evals exist.
7. Prefer additive files and adapters over broad rewrites.

## Phase 0: Baseline And Architecture Fence

Objective: freeze the current behavior enough to compare old and new loops.

Deliverables:

- document current graph contracts and state ownership;
- add a short architecture note naming LangGraph as the current engine, not the
  long-term runtime boundary;
- define the first scenario eval list;
- add a feature flag name for the future path, for example
  `AUTOBROWSER_AGENT_LOOP=v2`;
- make sure current tests stay green.

Suggested files:

- `docs/research/2026-07-26-agent-loop-runtime-research.md`
- `docs/architecture/overview.md`
- `src/cli/parser.py`
- `tests/test_agent_graph.py`

Exit gate:

- `python -m pytest` passes;
- the project has an explicit migration flag name and no runtime behavior has
  changed by default.

## Phase 1: Durable Event Stream Around The Existing Graph

Objective: create Codex/Claude-style lifecycle messages without replacing the
graph.

Add `src/agent_loop/events.py` with typed event records:

- `session.started`
- `goal.started`
- `graph.node_started`
- `graph.node_finished`
- `model.requested`
- `model.responded`
- `action.proposed`
- `policy.decided`
- `approval.requested`
- `tool.started`
- `tool.finished`
- `observation.compiled`
- `goal.completed`
- `goal.blocked`
- `goal.failed`
- `goal.cancelled`

Add event sinks:

- in-memory sink for tests;
- JSONL sink under `.autobrowser/sessions/<session_id>/events.jsonl`;
- CLI streaming adapter that can still print the current output.

Suggested files:

- `src/agent_loop/events.py`
- `src/agent_loop/tracing.py`
- `src/harness/runtime.py`
- `src/harness/session.py`
- `src/cli/task_runner.py`
- `tests/test_agent_loop_events.py`

Exit gate:

- every current task emits start, node/tool/policy, and terminal events;
- JSONL events are redacted and JSON-serializable;
- existing CLI output still works;
- trace files can be read after process exit.

Rollback:

- disable the event sink and keep the current `stream_updates()` behavior.

## Phase 2: Scenario Evals And Trace Replay

Objective: stop relying only on prompt intuition and unit tests.

Create replayable evals before changing the loop:

```text
tests/evals/
  scenarios/
    search_basic.yaml
    stale_ref_recovery.yaml
    unchanged_search_click.yaml
    dynamic_search_input.yaml
    filter_requires_apply.yaml
  runner.py
```

Each scenario should define:

- task text;
- fake browser snapshots or tool script;
- allowed maximum tool calls;
- required final answer assertions;
- forbidden repeated action patterns;
- expected terminal status.

Metrics:

- success/failure;
- number of model turns;
- number of tool calls;
- repeated action count;
- policy block count;
- whether a final answer was produced;
- trace completeness.

Suggested files:

- `src/agent_loop/evals.py`
- `tests/evals/runner.py`
- `tests/evals/scenarios/*.yaml`
- `tests/test_agent_loop_evals.py`

Exit gate:

- at least five browser scenarios pass on the current graph;
- every eval can export a trace;
- failing evals print the action sequence compactly.

Rollback:

- eval harness is additive; no production rollback needed.

## Phase 3: Context Assembler And Prompt Split

Objective: replace the single large prompt with layered context.

Add `ContextAssembler` that builds context blocks:

- core runtime instructions;
- user task;
- plan or todo state;
- recent transcript summary;
- current observation;
- current tool inventory;
- active skills;
- policy hints;
- browser state block when browser tools are relevant.

Move browser-specific rules out of `AGENT_SYSTEM_PROMPT` into a browser skill
resource, but keep hard invariants enforced by policy and observer before
removing them from the prompt.

Suggested files:

- `src/agent_loop/context.py`
- `src/agent_loop/prompts.py`
- `src/agent_loop/skills.py`
- `src/agent/prompts.py`
- `docs/development/browser-agent-rules.md`
- `tests/test_context_assembler.py`
- `tests/test_prompts.py`

Exit gate:

- prompt tests confirm required browser constraints still appear when needed;
- non-browser tasks do not receive browser-only context;
- eval baseline does not regress.

Rollback:

- switch `ContextAssembler` to render the old prompt verbatim.

## Phase 4: ProposedAction Contract And ModelDriver

Objective: make model output an explicit action, not direct state mutation.

Add typed action models:

- `AnswerAction`
- `ToolCallAction`
- `AskUserAction`
- `UpdatePlanAction`
- `DelegateAction`
- `CompactMemoryAction`
- `StopAction`

Add `ModelDriver` and `ActionParser`:

- bind tools when native tool calling is available;
- parse JSON fallback when native tool calls are unavailable;
- normalize one model response into one or more `ProposedAction` records;
- attach model metadata and raw response references to events.

Initially, adapt the current `create_agent_node()` output into actions. Do not
replace the graph yet.

Suggested files:

- `src/agent_loop/actions.py`
- `src/agent_loop/model.py`
- `src/agent_loop/adapters/langgraph.py`
- `tests/test_agent_loop_actions.py`
- `tests/test_agent_loop_model_driver.py`

Exit gate:

- all tool calls, replans, and final answers can be represented as actions;
- invalid model output becomes a typed blocked/retry event;
- eval baseline does not regress.

Rollback:

- bypass action parsing and return current graph updates.

## Phase 5: ToolBroker And PermissionEngine

Objective: move from marker-based policy to scoped runtime permissions.

Introduce tool metadata:

```text
ToolSpec
  name
  provider
  description
  input_schema
  scopes
  risk_level
  requires_approval
```

Suggested scopes:

- `read`
- `write`
- `browser.read`
- `browser.action`
- `browser.navigation`
- `network`
- `filesystem.read`
- `filesystem.write`
- `credential`
- `payment`
- `destructive`
- `external_post`

Add configurable policy:

```yaml
default: ask
allow:
  - browser.read
  - browser.navigation
ask:
  - browser.action
  - external_post
deny:
  - payment
  - credential
domains:
  "*.bank.example": deny
```

`ToolBroker` should:

- resolve tools through `ToolRegistry`;
- classify tool scopes before execution;
- call `PermissionEngine`;
- emit `tool.started` and `tool.finished`;
- normalize provider results;
- redact sensitive results before persistence.

Suggested files:

- `src/agent_loop/tools.py`
- `src/agent_loop/permissions.py`
- `src/agent_loop/approvals.py`
- `src/harness/tools.py`
- `src/harness/policy.py`
- `tests/test_agent_loop_permissions.py`
- `tests/test_agent_loop_tool_broker.py`

Exit gate:

- current policy tests pass through the new engine;
- sensitive actions become durable approval events;
- denied actions never reach tool invocation;
- eval baseline does not regress.

Rollback:

- route permission decisions back to current `PolicyEngine`.

## Phase 6: Hooks

Objective: add deterministic lifecycle behavior without prompt bloat.

Hook points:

- `SessionStart`
- `GoalStart`
- `PreContext`
- `PreModel`
- `PostModel`
- `PreTool`
- `PostTool`
- `OnObservation`
- `PreCompletion`
- `GoalEnd`

Start with Python callable hooks only. Add shell/HTTP hooks later after trust
and sandbox rules are designed.

Initial hooks:

- redact secrets before writing events;
- validate action schema after model output;
- block stale browser refs before tool execution;
- write trace summary at goal end;
- run eval assertions in test mode.

Suggested files:

- `src/agent_loop/hooks.py`
- `src/agent_loop/config.py`
- `tests/test_agent_loop_hooks.py`

Exit gate:

- hooks can observe, modify, or block actions with auditable decisions;
- hook failures are contained and visible in events;
- hooks are disabled by default unless configured.

Rollback:

- disable hook execution by config.

## Phase 7: Skills

Objective: move repeatable procedures out of the core prompt.

Add a skill format compatible with the local project style:

```text
skills/
  browser-task/
    SKILL.md
    references/
      snapshot-rules.md
      search-flow.md
      extraction.md
  trace-analysis/
    SKILL.md
```

Skill metadata:

- name;
- description;
- trigger hints;
- allowed scopes;
- context mode: inline, reference-only, or subagent;
- supporting files.

Initial skills:

- `browser-task`: snapshot/ref semantics, search, extraction, fallback.
- `trace-analysis`: summarize failed traces and classify loop failures.
- `browser-eval-author`: turn traces into eval scenarios.

Suggested files:

- `src/agent_loop/skills.py`
- `skills/browser-task/SKILL.md`
- `skills/trace-analysis/SKILL.md`
- `tests/test_agent_loop_skills.py`

Exit gate:

- skills load only when relevant or explicitly requested;
- browser evals pass with browser rules supplied by skill context;
- the core prompt becomes materially shorter.

Rollback:

- keep skill loader disabled and use old prompt.

## Phase 8: Subagents

Objective: add bounded parallel work without polluting the main context.

Start with read-only child goals:

- `research`;
- `trace_analyzer`;
- `eval_author`;
- `reviewer`.

Subagent contract:

```text
SubagentRequest
  role
  objective
  input_artifacts
  allowed_scopes
  max_turns
  max_tokens
  expected_output_schema
```

Rules:

- subagents get separate context and events;
- subagents return summaries, not raw logs;
- subagents cannot mutate parent state directly;
- parent runtime decides whether to accept returned findings;
- write-enabled subagents remain disabled until read-only subagents are stable.

Suggested files:

- `src/agent_loop/subagents.py`
- `src/agent_loop/goals.py`
- `tests/test_agent_loop_subagents.py`

Exit gate:

- parent traces show child goal lifecycle;
- child summaries are schema-validated;
- child failures do not fail the parent unless configured;
- no write-capable subagent exists yet.

Rollback:

- disable `DelegateAction`.

## Phase 9: Explicit AgentLoop Engine

Objective: replace graph orchestration only after the runtime shell is proven.

Add `AgentLoopEngine` behind `AUTOBROWSER_AGENT_LOOP=v2`:

```python
while not goal.terminal:
    context = context_assembler.build(goal)
    model_response = model_driver.invoke(context)
    actions = action_parser.parse(model_response)
    for action in actions:
        decision = permission_engine.classify(action)
        if decision.needs_approval:
            action = approval_queue.request(action, decision)
        result = tool_broker.execute(action, decision)
        observation = observation_compiler.compile(action, result)
        memory_store.record(goal, action, result, observation)
        completion_guard.update(goal, observation)
```

Keep LangGraph as `v1`:

```text
AUTOBROWSER_AGENT_LOOP=v1  # current default
AUTOBROWSER_AGENT_LOOP=v2  # explicit loop
```

Suggested files:

- `src/agent_loop/engine.py`
- `src/agent_loop/completion.py`
- `src/cli/parser.py`
- `src/harness/runtime.py`
- `tests/test_agent_loop_engine.py`
- `tests/evals/test_v1_v2_parity.py`

Exit gate:

- v2 passes all v1 scenario evals;
- v2 has equal or lower repeated-action failures;
- v2 event traces are complete;
- v2 can be stopped, resumed, and blocked cleanly;
- v1 remains available for rollback.

Rollback:

- set `AUTOBROWSER_AGENT_LOOP=v1`.

## Phase 10: Service Surface And Control Plane

Objective: expose the runtime through multiple surfaces, as Codex and Claude
Code do, without duplicating loop logic.

Add a service API after the engine boundary exists:

- `POST /sessions`
- `GET /sessions/{id}`
- `POST /sessions/{id}/goals`
- `GET /sessions/{id}/goals/{goal_id}/events`
- `POST /approvals/{approval_id}`
- `GET /tools`
- `GET /skills`
- `GET /health`

CLI should call the same runtime service or the same in-process `GoalRunner`.
Future UI should consume the event stream, not inspect internal graph state.

Suggested files:

- `src/api/`
- `src/agent_loop/service.py`
- `tests/test_agent_loop_service.py`

Exit gate:

- CLI and API produce the same event schema;
- approvals work through CLI and API;
- active goals can be inspected without breaking execution.

Rollback:

- keep CLI on in-process runtime and disable API server.

## Cross-Phase Acceptance Criteria

The migration is complete when:

- the default runtime is `AgentLoopEngine`, not the LangGraph graph;
- LangGraph can be removed or kept as a compatibility engine without owning
  architecture;
- every action has a durable event trail;
- context is assembled from explicit blocks and skills;
- hard safety rules live in permissions, hooks, or observer code;
- scenario evals protect search, stale refs, unchanged actions, and extraction;
- approvals are durable and resumable;
- subagents are bounded, auditable, and cannot mutate parent state directly;
- CLI/API/UI surfaces can share the same event stream and goal state.

## Recommended Implementation Order

1. Event stream and JSONL traces.
2. Scenario eval harness and baseline scenarios.
3. Context assembler and prompt split.
4. ProposedAction contract.
5. ToolBroker and PermissionEngine.
6. Hooks.
7. Skills.
8. Read-only subagents.
9. Explicit `AgentLoopEngine`.
10. API/control-plane surface.

This order is intentionally conservative. It gives the project observability
and evals before increasing autonomy.

## First Sprint

The first sprint should be small and measurable:

1. Create `src/agent_loop/events.py`.
2. Add `EventRecord`, `EventSink`, `InMemoryEventSink`, and `JsonlEventSink`.
3. Emit events from `SessionRuntime.run_task()` and
   `BrowserHarness.stream_updates()`.
4. Persist `.autobrowser/sessions/<session_id>/events.jsonl`.
5. Add tests for event ordering, JSON safety, and redaction.
6. Add one trace replay helper that prints action sequences.

Do not change the model prompt, policy, or graph routing in the first sprint.
The first sprint is successful when it improves observability without changing
agent behavior.

## Risks

- Rewriting the loop too early can regress browser reliability.
- Moving rules out of prompts without hard guards can reintroduce stale-ref
  loops.
- Adding subagents before permissions can create uncontrolled side effects.
- Adding hooks before trust/config can create hidden execution paths.
- Building API/UI before event schema stabilizes can freeze the wrong
  abstraction.

## Decision Still Needed

This research note does not decide whether LangGraph should be removed. The
decision should be made after v2 passes the scenario suite and the trace data
shows the explicit loop is easier to debug.
