# ADR: Native Agent Loop Engine Becomes the Sole Runtime

Status: Accepted
Date: 2026-08-31

> **Update (2026-09-03):** [Drop LangChain/LangGraph/LangSmith Stack](2026-09-03-drop-langchain-stack-provider-neutral-model.md)
> extends this ADR. The engine-native decision stands, but the LangGraph
> checkpoint-saver, `MemoryManager`-on-harness, `ChatOllama`, and LangSmith
> tracing remnants this record mentions were removed by that later change.

## Context

AutoBrowser's control flow was historically owned by a compiled LangGraph graph
(`START -> plan -> agent -> policy -> executor -> observe -> agent`) assembled
by `build_agent_graph` in `src/agent/agent.py`. A parallel engine-native loop
was built incrementally under `src/agent_loop/` — provider-neutral contracts
(`ProposedAction`/`ModelDriver`), events/tracing/replay/metrics,
`ContextAssembler`, `GoalRunner`, and finally the explicit `AgentLoopEngine`
control-flow shell. The migration proceeded behind the `AUTOBROWSER_AGENT_LOOP`
feature flag with LangGraph as the default and rollback path.

The two engines coexisting created sustained drift: the graph and the native
loop duplicated browser-ref invariants across prompts, `PolicyEngine`, the
observer, and provider tests, and transitional debt kept accumulating
(`src/agent_loop/adapters/langgraph.py`, the legacy `LegacyAgentStateObservationCompiler`
in `src/agent_loop/outcomes.py`, and a CLI task-runner adapter that streamed
LangGraph update chunks). Scenario evals proved the native engine reached the
same terminal outcomes as the LangGraph v1 baseline (4/5 scenarios exact match;
`stale_ref_recovery` one model turn more efficient because the native loop
replans from visible refs without a wasted graph round-trip).

Decision: complete the migration. Remove all legacy control-flow code, make
`AgentLoopEngine` the sole execution path, and delete `src/agent/`.

## Decision

`AgentLoopEngine` in `src/agent_loop/execution/loop.py` is the only execution
engine. No LangGraph graph owns the agent loop anymore.

- **Control-flow chain:**
  `SessionRuntime -> GoalRunner -> native_task_runner -> AgentLoopEngine ->
  TurnController`. `SessionRuntime` builds task config and carried state;
  `GoalRunner` owns the one-task lifecycle (timeouts, watchdog, latest-state
  loading, goal events); `native_task_runner` composes `EngineResources` from
  `BrowserHarness`; `AgentLoopEngine` builds the initial plan (model call #0),
  then drives a bounded `while` loop of `TurnController` turns, replanning or
  terminating as each `TurnResult` directs, and returns a terminal
  `AgentLoopResult`.
- **State:** the frozen `LoopState` dataclass (`src/agent_loop/execution/state.py`)
  replaces the `AgentState` TypedDict as the mutable loop state. `LoopState.apply`
  is strict — it rejects unknown keys. `LoopState.to_session_state()` produces the
  `SESSION_STATE_KEYS` dict carried across tasks.
- **Terminal result:** `AgentLoopResult(status, final_answer, session_state, state,
  turns)`. `status` is always terminal (`done`/`blocked`/`cancelled`); the engine
  derives it via `CompletionController.status_from_state`.
- **Provider-neutral contracts:** the typed tool/plan/observation contracts and
  control-loop thresholds live in `src/contracts.py`, which imports nothing from
  `src/agent/`, `src/agent_loop/`, `src/harness/`, or `src/browser/`.
- **Type-only state shapes:** `AgentState`/`BrowserState` remain in `src/state.py`
  as TypedDicts for harness/browser layers that still annotate collaborators with
  the full loop-state shape; nothing constructs them for control flow.
- **Prompts:** planner, reasoning, and observer prompts are consolidated in
  `src/agent_loop/prompts.py` (there is no separate `src/agent/` prompt tree).
- **Model defaults:** `DEFAULT_OLLAMA_MODEL` and the `ChatOllama` factory default
  now live in `src/llm.py`, free of any `src/agent/` import.
- **Removed legacy code:**
  - `src/agent/` — graph assembly, `nodes.py`/`routers.py`/`prompts.py`/`state.py`,
    and the `planner`/`executor`/`observer` subgraphs.
  - `src/agent_loop/adapters/langgraph.py` — the `ProposedAction -> AgentState`
    bridge (ported natively into `TurnController._classify_action`).
  - `src/cli/task_runner.py` — the legacy graph-streaming CLI adapter.
  - The legacy `LegacyAgentStateObservationCompiler`; `src/agent_loop/outcomes.py`
    now contains only the stable provider-neutral `GoalState` types and guards.
  - `BrowserHarness` graph responsibilities — it is now a pure composition root
    (`context`, `memory`, `tools`, `policy`, `telemetry`, `events`, `llm`) with no
    `run`/`stream_updates`/`get_state_values`/recursion-limit recovery.
- **CLI output:** final-answer printing was restored in both CLI paths
  (`SessionRuntime.run_forever` prints the terminal `final_answer`; the cmd2
  `AgentCli._on_task_done` prints it too).
- **Feature flags:** `AUTOBROWSER_AGENT_LOOP` and `SessionConfig.agent_loop` are
  kept inert — the flag parses, but the native path always runs. `AUTOBROWSER_CONTEXT_MODE`
  still selects `legacy` vs `assembled` prompt rendering.

## Consequences

Positive:

- One source of truth for control flow; no graph/engine behavioral drift.
- No `langgraph` runtime dependency in the agent loop — easier to debug, replay,
  and extend (`AgentLoopResult` is a plain frozen dataclass).
- Transitional debt removed; `src/agent/` is gone; the ownership chain is flat and
  observable.
- Full test suite green (137 passed) after the removal, with the deleted
  graph-streaming/state tests replaced by native lifecycle coverage in
  `tests/test_harness_session.py`, `tests/test_harness_runtime.py`,
  `tests/test_goal_runner.py`, and `tests/test_main_cli.py`.

Tradeoffs and risks:

- LangGraph checkpoint semantics are gone from control flow. `MemoryManager` still
  owns a checkpoint saver and the session thread key (`configurable.thread_id`)
  for history attribution, but durable context now travels explicitly through
  `LoopState.messages`/`observation`/`browser` carried between tasks.
- `SessionConfig.agent_loop`/`AUTOBROWSER_AGENT_LOOP` are inert surface area; they
  should be cleaned up once external tooling stops referencing them.
- Docs that described the legacy graph as "the active engine" were rewritten in the
  same change (see Related); historical ADRs and research notes describing the
  migration remain untouched.

## Alternatives Considered

- **Keep LangGraph behind a flag indefinitely.** Rejected: two engines to
  maintain, behavior drifts, and the transitional adapter debt keeps growing.
- **Port node-by-node while retaining the graph as fallback.** Rejected: the
  graph was the source of test/behavior divergence, and scenario evals showed the
  native engine already matched or beat the baseline.
- **Keep `src/agent/` as a compatibility engine without owning control flow.**
  Rejected: it duplicated prompts, contracts, and invariants with no consumers
  left after the native port.

## Related

- Supersedes the LangGraph-thread ownership assumptions in:
  `2026-07-23-long-lived-session-runtime.md`,
  `2026-07-24-task-memory-isolation-and-session-persistence.md`,
  `2026-07-24-session-context-root-object.md`,
  `2026-07-25-session-scoped-agent-context-memory.md`,
  `2026-07-26-browser-provider-boundary.md` (the browser-provider boundary itself
  remains the architecture; its LangGraph references are historical).
- Migration plans: `docs/development/2026-08-08-agent-loop-engine-migration-touchpoints.md`,
  `docs/development/2026-08-08-agent-loop-engine-development-plan.md`,
  `docs/research/2026-08-06-agentloopengine-architecture-review.md`.
- Engine-native code: `src/agent_loop/execution/loop.py` (`AgentLoopEngine`,
  `TurnController`, `native_task_runner`), `src/agent_loop/execution/state.py`
  (`LoopState`), `src/agent_loop/execution/completion.py`,
  `src/agent_loop/execution/guards.py`, `src/agent_loop/goals.py` (`GoalRunner`),
  `src/harness/session.py` (`SessionRuntime.run_task`).
