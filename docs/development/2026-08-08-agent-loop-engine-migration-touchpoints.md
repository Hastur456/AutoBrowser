# 2026-08-08 Agent Loop Engine Migration Touchpoints

Status: Migration warning note

## Purpose

This note records code that still couples the current runtime to the legacy
LangGraph Agent Loop or to `AgentState`-shaped browser execution. Use it as a
checklist before switching the active runtime to a new `AgentLoopEngine`.

The new engine migration is not just a replacement for
`src/agent_loop/outcomes.py` or `src/agent_loop/adapters/langgraph.py`. Large
parts of `src/harness/` and `src/browser/` currently exist to feed, execute, or
recover the old graph shape, and they need to be reviewed or rewritten around
the new engine contracts.

## Must Rewrite Or Remove

### `src/agent_loop/outcomes.py`

This file is transitional compatibility debt. It adapts nested
`AgentState`-shaped results into `GoalState` by searching for `final_answer`
and reading `decision`.

Migration action:

- replace `LegacyAgentStateObservationCompiler` with the new engine's explicit
  terminal result;
- remove `CompletionGuard` and `GoalStateCompletionGuard` if terminal status is
  already part of the engine result;
- stop inferring `completed`, `blocked`, or `continue` from legacy graph fields.

### `src/agent_loop/adapters/langgraph.py`

This adapter converts `ProposedAction` back into legacy LangGraph state
updates by calling helpers from `src/agent/utils.py`.

Migration action:

- remove `proposed_action_to_legacy_update()` from the active model/action
  path;
- have the new engine consume `ProposedAction` directly;
- replace `_done_response()`, `_replan_response()`, and
  `_tool_request_update()` dependencies with engine-native state transitions.

### `src/agent/nodes.py`

The current reasoning node is already partly crossed with `agent_loop` because
it parses model output into proposed actions, then immediately converts those
actions back to legacy state through `proposed_action_to_legacy_update()`.

Migration action:

- move action selection into the new engine;
- keep `src/agent/` only as the LangGraph compatibility implementation or
  remove it from the active path;
- do not keep the model loop split across a new engine and legacy graph node.

## Harness Touchpoints

### `src/harness/runtime.py`

`BrowserHarness` is currently a LangGraph adapter. It compiles a graph builder,
injects checkpointer, context, tool registry, policy node, history builder, and
observer options, then calls `graph.ainvoke()`, `graph.astream()`, and
`graph.aget_state()`.

Legacy assumptions to replace:

- `GraphBuilder` returns a LangGraph-compatible object;
- checkpoint state is available through `aget_state()`;
- streaming chunks are LangGraph update dictionaries;
- completion recovery checks `decision == "done"` and `final_answer`;
- state overrides are injected as initial `AgentState`;
- event projection uses graph chunk shapes.

Migration action:

- turn `BrowserHarness` into an engine adapter or replace it with an
  `AgentLoopEngine` runtime wrapper;
- define engine-native invoke, stream, latest-state, and progress contracts;
- move recursion-limit recovery to an engine-level failure/progress policy or
  delete it if the new engine has explicit terminal status.

### `src/harness/session.py`

`SessionRuntime` already delegates one task to `GoalRunner`, but it still
builds LangGraph-style task config and carries `AgentState` fields between
tasks.

Legacy assumptions to replace:

- session thread id is stored as LangGraph `configurable.thread_id`;
- carried state is passed through `HARNESS_STATE_OVERRIDES_CONFIG_KEY`;
- task-local reset lists are legacy graph fields such as `plan`, `decision`,
  `tool_request`, `policy_decision`, retry counters, and `final_answer`;
- latest state is loaded through `BrowserHarness.get_state_values()`.

Migration action:

- define a session-to-engine context handoff contract;
- keep only session-useful memory in `SessionContext.state`;
- replace LangGraph config construction with engine run options;
- update `HarnessLatestStateLoader` or remove it if the new engine returns
  latest state directly.

### `src/harness/context.py`

`ContextBuilder` still builds legacy `AgentState` initial state and defaults to
legacy prompt rendering. The assembled context path is available, but not yet
the default.

Migration action:

- make `ContextAssembler` the primary prompt/context boundary for the new
  engine;
- stop returning `AgentState` from `build_initial_state()`;
- keep `AUTOBROWSER_CONTEXT_MODE=legacy` only as rollback while the LangGraph
  implementation remains available.

### `src/harness/memory.py`

Memory helpers are built around LangChain messages and `AgentState.messages`.
`MemoryManager` also owns LangGraph checkpoint saver lifecycle.

Migration action:

- separate durable conversation history from LangGraph checkpoint memory;
- define how the new engine stores and retrieves message history;
- remove checkpoint saver ownership from the active path if the new engine does
  not use LangGraph checkpoints.

### `src/harness/policy.py`

`PolicyEngine` currently reads and writes `AgentState` fields and returns
legacy state updates for graph routing. It also appends tool messages when it
blocks a request.

Migration action:

- make policy consume engine-native proposed actions or tool requests;
- return typed policy decisions/events instead of graph state patches;
- move retry counter updates and message appends into engine-owned state
  handling.

### `src/harness/tools.py`

`ToolRegistry` is mostly reusable, but its browser-provider integration assumes
the current `BrowserProvider` protocol and LangChain-compatible tools.

Migration action:

- keep lazy tool loading if the new engine still binds LangChain/MCP tools;
- otherwise introduce an engine-native tool broker;
- update browser provider discovery after `BrowserProvider` is decoupled from
  `AgentState`.

### `src/harness/telemetry.py` and `src/agent_loop/tracing.py`

Telemetry and trace projection are currently tied to graph lifecycle events and
LangGraph update chunks.

Migration action:

- keep durable `EventRecord` as the stable event envelope if possible;
- replace chunk projection with engine-native events such as model turn,
  action proposed, policy decision, tool started/finished, observation, and
  terminal goal status.

## Browser Touchpoints

### `src/browser/provider.py`

`BrowserProvider.normalize_request()` currently accepts legacy `ToolRequest`
and `AgentState`; `normalize_result()` returns legacy `ToolResult`.

Migration action:

- change providers to consume engine-native action/request and browser context
  contracts;
- pass only the browser state needed for normalization, not the full agent
  state;
- define a provider-neutral browser result that the new engine can observe
  without going through legacy executor state.

### `src/browser/adapters/playwright_mcp.py`

The Playwright adapter reads `state["snapshot"]` to fill `element`, maps
canonical names to Playwright MCP names, and normalizes invalid refs into the
old `ToolResult` shape.

Migration action:

- keep canonical-to-Playwright name mapping if Playwright MCP remains the
  production backend;
- move snapshot-derived element lookup to an explicit browser context object;
- return engine-native browser errors and observations instead of legacy tool
  result dictionaries.

### `src/browser/fake.py`

`FakeBrowserProvider` is useful for deterministic tests, but it implements the
legacy provider protocol and result shape.

Migration action:

- update fake browser actions/results to the new engine contract;
- keep deterministic snapshot replay for scenario evals;
- ensure stale-ref, unchanged-action, and snapshot-depth cases still exist in
  tests after the provider rewrite.

## Executor, Observer, And Policy Coupling

The legacy executor and observer live under `src/agent/subgraphs/`, not under
`src/agent_loop/`, but they still define behavior the new engine must replace:

- executor resolves tools through `ToolRegistry`;
- executor calls browser provider request/result normalizers;
- observer stores snapshots, clears stale snapshots after browser actions,
  tracks invalid-ref recovery, and detects ineffective browser actions;
- policy blocks sensitive tools, redundant snapshots, and repeated ineffective
  browser actions.

Migration action:

- port these behaviors into explicit new engine components before disabling
  the LangGraph graph;
- do not assume replacing model action parsing is enough;
- preserve the snapshot/ref safety behavior in browser eval scenarios before
  swapping engines.

## Eval, Batch, CLI, And Script Touchpoints

### `src/agent_loop/evals.py`

Scenario evals still construct `BrowserHarness(build_agent_graph, ...)` and
stream LangGraph chunks. They validate the current engine, not a future one.

Migration action:

- add an engine selection point for evals;
- run the same fake-browser scenarios against both legacy LangGraph and the new
  engine until parity is proven;
- update `EvalResult.final_state` if terminal state is no longer a graph state
  dictionary.

### `src/cli/task_runner.py`

The CLI task runner streams from `BrowserHarness.stream_updates()`. User-facing
state output and snapshot hiding depend on chunk/state shape.

Migration action:

- define an engine-neutral streaming event format for CLI output;
- keep `--show-state`, `--hide-snapshot`, `--json`, and `--compress-tools`
  behavior covered by tests during the switch.

### `scripts/run_batch.py`, `scripts/run_evals.py`, and exporters

Batch runs go through `SessionRuntime`, while evals go directly through the
current harness. Exporters derive metrics from durable events and task records.

Migration action:

- keep event names and terminal status semantics stable or document a new event
  contract;
- update metrics/export extraction if final answers move out of legacy
  `final_answer` fields;
- keep old and new runs distinguishable in batch metadata.

## Migration Gates

Do not make the new Agent Loop engine the default until:

- `GoalRunner` consumes explicit engine terminal results without
  `LegacyAgentStateObservationCompiler`;
- `BrowserHarness` is either replaced or made engine-neutral;
- `SessionRuntime` no longer injects carried context as legacy `AgentState`;
- browser providers no longer require full `AgentState`;
- policy returns typed decisions instead of graph state patches;
- observer behavior is ported or replaced with equivalent engine-owned
  observation logic;
- evals can run the same scenarios against the new engine;
- exporters know where terminal status and final answer live;
- docs and `AGENTS.md` no longer describe LangGraph as the active agent loop
  after the switch.

## Related Documents

- [Architecture Overview](../architecture/overview.md)
- [Agent Loop Legacy Outcomes Cleanup](2026-08-05-agent-loop-legacy-outcomes.md)
- [GoalRunner Branch Plan](2026-08-01-goal-runner-branch-plan.md)
- [Context Assembler And Prompt Split Plan](2026-08-01-context-assembler-prompt-split.md)
- [Batch And Export Data Contracts](2026-07-30-batch-export-data-contracts.md)
