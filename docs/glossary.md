# Glossary

| Term | Meaning |
| --- | --- |
| Agent graph | The compiled LangGraph state machine assembled by `build_agent_graph`. |
| Agent Loop contracts | Runtime-facing contracts under `src/agent_loop/` for proposed actions, model turns, events, traces, metrics, context assembly, batch/export, evals, and goal lifecycle boundaries around the current graph engine. |
| AgentLoopEngine | Transitional explicit loop shell that wraps `GoalRunner` behind the `AUTOBROWSER_AGENT_LOOP` feature flag. |
| Agent node | The reasoning node that chooses a tool call, replans, or returns a final answer. |
| AgentTraceSink | Event sink that writes compact human-readable trace projections beside durable event records. |
| Assembled context | Deterministic prompt representation produced by `ContextAssembler` when `AUTOBROWSER_CONTEXT_MODE=assembled`. |
| ArtifactRegistry | Session-owned registry for durable outputs such as screenshots, downloads, reports, or extracted files. |
| AutoBrowser | The browser automation agent implemented in this repository. |
| Batch run | Execution of JSONL Golden Set scenarios through fresh `SessionRuntime` instances, with metadata written under `.autobrowser/batches/<batch_id>/`. |
| BrowserAction | Provider-neutral typed browser request using canonical `browser.*` action names. |
| Browser error code | Shared browser-layer error vocabulary such as `invalid_ref`, `unknown_action`, and `action_failed`. |
| BrowserHarness | Runtime composition wrapper that injects context, tools, memory, policy, and telemetry into the graph. |
| BrowserProvider | Protocol for browser backends that expose tools and normalize browser tool requests and results. |
| BrowserResult | Provider-neutral browser action result shape with status, content, error, and optional error code. |
| `browser_evaluate` | Browser tool fallback for cases where snapshots cannot expose required information. |
| `browser_find` | Browser tool for plain-text search; not reliable for structured link or attribute extraction. |
| `browser_snapshot` | Source-of-truth browser observation containing visible page state and element refs. |
| Canonical browser action | Provider-neutral browser action name such as `browser.snapshot`, mapped to backend-specific tool names by adapters. |
| Checkpointer | LangGraph persistence component owned by `MemoryManager`. |
| Compact observation | Short observer output derived from a tool result and used by the next agent step. |
| ContextAssembler | Runtime-facing context builder in `src/agent_loop/context.py` that renders ordered prompt blocks for the assembled context path. |
| Direct search URL fallback | Navigating directly to a site's search results URL when UI search controls do not make progress. |
| EventRecord | Durable JSON-safe event envelope for session, goal, graph, model, action, policy, tool, observation, and terminal lifecycle events. |
| Executor | Subgraph that resolves and invokes approved tool requests. |
| Export row | JSONL task-level analytics row produced by `scripts/export_sessions.py` from persisted session, task, event, feedback, and batch metadata. |
| FakeBrowserProvider | Deterministic browser provider used by tests to replay snapshots without Chrome, CDP, or MCP. |
| GraphRecursionError | LangGraph error raised when the graph exceeds the configured recursion limit. |
| GoalState | Provider-neutral terminal state used by `GoalRunner` to determine explicit goal completion status during the migration to the new Agent Loop. |
| GoalRunRequest | Immutable input object for one `GoalRunner` execution, including task text, task id, goal id, session thread id, graph config, and state overrides. |
| GoalRunResult | Immutable terminal object returned or internally constructed by `GoalRunner`, including raw task result or exception, latest state, and explicit terminal status. |
| GoalRunner | One-task lifecycle boundary between `SessionRuntime` and the current task runner; emits goal lifecycle events, delegates execution, captures latest state through `LatestStateLoader`, and does not own the model/action loop. |
| Harness | Runtime layer around the graph; owns infrastructure that should not be hardcoded into agent nodes. |
| Ineffective browser action | A successful browser action whose follow-up snapshot has the same visible fingerprint as the previous snapshot. |
| LangGraph | Graph runtime used for the AutoBrowser agent loop and subgraphs. |
| LatestStateLoader | Callable port injected into `GoalRunner` to load latest graph state from the current harness/config, with fallback to task result state when no checkpoint is available. |
| LegacyAgentStateObservationCompiler | Transitional adapter in `src/agent_loop/outcomes.py` that maps old LangGraph `AgentState` fields such as `final_answer` and `decision` into `GoalState`; remove after the new Agent Loop emits provider-neutral terminal state. |
| MCP | Model Context Protocol, used here for browser tool integration. |
| New Agent Loop terminal state | Future provider-neutral completion result returned directly by the agent engine, replacing legacy `AgentState` inspection in `outcomes.py`. |
| Observer | Subgraph that translates tool results and snapshots into compact state updates. |
| Planner | Subgraph that creates or revises compact task plans. |
| Playwright MCP | Browser automation tool provider whose snapshot refs drive interactions. |
| PlaywrightMCPBrowserProvider | Browser provider adapter that wraps Playwright MCP tools and normalizes request/result schema differences. |
| PolicyEngine | Harness boundary that classifies tool requests as approved, needing human input, or blocked. |
| ProposedAction | Provider-neutral model action contract used during the migration from legacy graph state updates to explicit Agent Loop actions. |
| ref | Ephemeral Playwright MCP element identifier such as `e123`; valid only for the snapshot that produced it. |
| SessionConfig | Args-derived configuration used to initialize a long-lived `SessionRuntime` and shared task config. |
| SessionContext | Root object for one process-scoped session; owns session state, task history, workspace, artifacts, events, and runtime handles. |
| SessionEventBus | Minimal synchronous event bus for session lifecycle events such as task start, task finish, and session close. |
| SessionMetadata | Session-owned metadata such as started time, last activity, task count, and runtime version. |
| Session records | Runtime-local JSON files under `.autobrowser/sessions/<session_id>/`, currently `session.json` and `tasks.json`. |
| SessionRuntime | Process-lifetime coordinator that runs the session loop, delegates lifecycle state to `SessionContext`, and sends each task to `BrowserHarness`. |
| Session-scoped thread ID | Stable `configurable.thread_id` derived from `SessionContext.session_id` and reused for all tasks in one interactive session. |
| SessionState | Mutable mapping wrapper for shared session-level state that should not require a dedicated typed field yet. |
| State override channel | Harness-internal config entry used to inject carried session state into a new graph invocation; stripped before LangGraph receives config. |
| Session workspace | Runtime-local directory under `.autobrowser/sessions/<session_id>/workspace/` for downloads, screenshots, temp files, and artifacts. |
| Snapshot depth | Tool argument that controls how much visible hierarchy `browser_snapshot` returns. |
| Task boundary reset | Clearing task-local graph fields such as plan, final answer, errors, policy state, tool request/result, and retry counters before a new task starts. |
| Task ID | Generated identifier stored on `TaskRecord` and graph state to attribute one user request inside a session. |
| Task thread ID | Deprecated term for the former per-task LangGraph checkpoint thread ID; replaced by the session-scoped thread ID plus per-task `task_id`. |
| TaskRecord | Session history entry for one user task, including task ID, task text, result, start time, and finish time. |
| Task lifecycle | One user request delegated to the agent, ending when the compiled graph reaches a terminal task state. |
| ToolRegistry | Lazy registry that exposes tools from static lists, generic providers, browser providers, or MCP clients. |
| Trace replay | Loading `events.jsonl` records to summarize terminal status and print compact action sequences for diagnostics or eval failures. |
