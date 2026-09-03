# Glossary

| Term | Meaning |
| --- | --- |
| Agent Loop contracts | Runtime-facing contracts under `src/agent_loop/` for proposed actions, model turns, events, traces, metrics, context assembly, batch/export, evals, and goal lifecycle boundaries around the engine-native loop. |
| AgentLoopEngine | The explicit control-flow owner in `src/agent_loop/execution/loop.py`: builds the initial plan, then drives a bounded `while` loop of `TurnController` turns and returns a terminal `AgentLoopResult`. |
| AgentLoopResult | Frozen terminal result of one engine run: `status` (`done`/`blocked`/`cancelled`), `final_answer`, `session_state`, `state`, and `turns`. |
| Agent step | The reasoning phase of a `TurnController` turn that chooses a tool call, a replan, or a done decision. |
| AgentTraceSink | Event sink that writes compact human-readable trace projections beside durable event records. |
| Assembled context | Deterministic, ordered prompt blocks produced by `ContextAssembler` (`src/agent_loop/context.py`), the canonical prompt-construction path. |
| ArtifactRegistry | Session-owned registry for durable outputs such as screenshots, downloads, reports, or extracted files. |
| AutoBrowser | The browser automation agent implemented in this repository. |
| Batch run | Execution of JSONL Golden Set scenarios through fresh `SessionRuntime` instances, with metadata written under `.autobrowser/batches/<batch_id>/`. |
| BrowserAction | Provider-neutral typed browser request using canonical `browser.*` action names. |
| Browser error code | Shared browser-layer error vocabulary such as `invalid_ref`, `unknown_action`, and `action_failed`. |
| BrowserHarness | Runtime composition root that holds context, tools, policy, telemetry, events, and the reasoning `llm`; `EngineResources.from_harness` bundles them for the engine. |
| BrowserProvider | Protocol for browser backends that expose tools and normalize browser tool requests and results. |
| BrowserResult | Provider-neutral browser action result shape with status, content, error, and optional error code. |
| `browser_evaluate` | Browser tool fallback for cases where snapshots cannot expose required information. |
| `browser_find` | Browser tool for plain-text search; not reliable for structured link or attribute extraction. |
| `browser_snapshot` | Source-of-truth browser observation containing visible page state and element refs. |
| Canonical browser action | Provider-neutral browser action name such as `browser.snapshot`, mapped to backend-specific tool names by adapters. |
| ChatModel | Provider-neutral chat protocol in `src/llm.py`: `async complete(messages, *, tools, **params) -> ModelResponse`. Provider adapters implement it; the engine drives it and never sees provider objects. |
| Checkpointer | Removed. There is no checkpoint saver; durable history is carried on `LoopState.messages` and `SessionContext.state`, shaped by the functional `MemoryManager`. |
| Compact observation | Short observer output derived from a tool result and used by the next agent step. |
| CompletionStatus | Loop completion status (`continue`/`done`/`blocked`/`cancelled`) carried on `AgentLoopResult`; `GoalRunner` maps it to a terminal `GoalStatus` via `goal_status_from_completion()` in `src/contracts.py`. |
| ContextAssembler | The sole prompt-construction boundary in `src/agent_loop/context.py`: builds the durable system prompt, the per-turn user prompt, and the planner prompt from ordered `ContextBlock`s. |
| Direct search URL fallback | Navigating directly to a site's search results URL when UI search controls do not make progress. |
| EngineResources | Bundled runtime collaborators (`llm`, `tool_registry`, `browser_providers`, `policy`, `context`, `events`) built from `BrowserHarness` and passed to `AgentLoopEngine`. |
| EventRecord | Durable JSON-safe event envelope for session, goal, engine, model, action, policy, tool, observation, and terminal lifecycle events. |
| Executor | Engine phase that resolves and invokes approved tool requests through `ToolBroker`/`ToolRegistry`. |
| Export row | JSONL task-level analytics row produced by `scripts/export_sessions.py` from persisted session, task, event, feedback, and batch metadata. |
| FakeBrowserProvider | Deterministic browser provider used by tests to replay snapshots without Chrome, CDP, or MCP. |
| GoalRunRequest | Immutable input object for one `GoalRunner` execution, including task text, task id, goal id, session thread id, task config, and state overrides. |
| GoalRunResult | Immutable terminal object returned or internally constructed by `GoalRunner`, including raw task result or exception, latest state, and explicit terminal status. |
| GoalRunner | One-task lifecycle boundary between `SessionRuntime` and the engine; emits goal lifecycle events, delegates execution through `native_task_runner`, captures latest state through `LatestStateLoader`, and does not own the model/action loop. |
| GoalStatus | Terminal goal lifecycle status (`completed`/`failed`/`cancelled`/`blocked`) returned by `GoalRunner` in `GoalRunResult`, derived from the engine's `CompletionStatus`. |
| Harness | Runtime layer around the engine; owns infrastructure that should not be hardcoded into loop code. |
| Ineffective browser action | A successful browser action whose follow-up snapshot has the same visible fingerprint as the previous snapshot. |
| LatestStateLoader | Callable port injected into `GoalRunner` to load latest loop state from the current harness/config, with fallback to the task result's session state. |
| LoopState | The frozen dataclass (in `src/agent_loop/execution/state.py`) that carries all loop state; `apply()` is strict and rejects unknown keys. |
| MCP | Model Context Protocol, used here for browser tool integration. |
| MemoryManager | Functional (stateless) history service in `src/harness/memory.py` that shapes a `list[Message]` — seeding the user task, appending tool calls/results, compacting snapshots — and returns new lists; the durable history lives on `LoopState.messages`, not on the service. |
| Message | Provider-neutral chat message (`src/messages.py`) with a `system`/`user`/`assistant`/`tool` role; assistant messages may carry `tool_calls`, and a `tool` message pairs a result back to exactly one `ToolCall.id`. |
| ModelResponse | Canonical provider-neutral model reply (`content` and/or `tool_calls`, plus `finish_reason`) returned by a `ChatModel`. |
| Observer | Engine phase that translates tool results and snapshots into compact loop state updates. |
| Ollama provider | Thin `ChatModel` adapter in `src/providers/ollama.py` (`OllamaChatModel` / `ollama_llm_factory`) that maps neutral `Message`/`ToolDef` objects to Ollama's `/api/chat` shape and parses replies into `ModelResponse`. |
| Planner | Engine phase that creates or revises compact task plans. |
| Playwright MCP | Browser automation tool provider whose snapshot refs drive interactions. |
| PlaywrightMCPBrowserProvider | Browser provider adapter that wraps Playwright MCP tools and normalizes request/result schema differences. |
| PolicyEngine | Harness boundary that classifies tool requests as approved, needing human input, or blocked. |
| ProposedAction | Provider-neutral model action contract (`answer`/`tool_call`/`update_plan`/`ask_user`/`delegate`/`compact_memory`/`stop`) parsed from a model turn and mapped to `LoopState` updates by the engine. |
| Provider adapter | Thin adapter that implements `ChatModel` by serializing neutral `Message`/`ToolDef` objects to a backend wire format and parsing the reply back into a `ModelResponse`. |
| ref | Ephemeral Playwright MCP element identifier such as `e123`; valid only for the snapshot that produced it. |
| SessionConfig | Args-derived configuration used to initialize a long-lived `SessionRuntime` and shared task config. |
| SessionContext | Root object for one process-scoped session; owns session state, task history, workspace, artifacts, events, and runtime handles. |
| SessionEventBus | Minimal synchronous event bus for session lifecycle events such as task start, task finish, and session close. |
| SessionMetadata | Session-owned metadata such as started time, last activity, task count, and runtime version. |
| Session records | Runtime-local JSON files under `.autobrowser/sessions/<session_id>/`, currently `session.json` and `tasks.json`. |
| SessionRuntime | Process-lifetime coordinator that runs the session loop, delegates lifecycle state to `SessionContext`, and sends each task to `GoalRunner`. |
| Session-scoped thread ID | Stable `configurable.thread_id` derived from `SessionContext.session_id` and reused for all tasks in one interactive session; used for attribution, not a checkpoint thread. |
| SessionState | Mutable mapping wrapper for shared session-level state that should not require a dedicated typed field yet. |
| State override channel | Harness-internal config entry (`HARNESS_STATE_OVERRIDES_CONFIG_KEY`) used to inject carried session state into the next engine run; stripped before the engine sees the task config. |
| Session workspace | Runtime-local directory under `.autobrowser/sessions/<session_id>/workspace/` for downloads, screenshots, temp files, and artifacts. |
| Snapshot depth | Tool argument that controls how much visible hierarchy `browser_snapshot` returns. |
| Task boundary reset | Clearing task-local loop fields such as plan, final answer, errors, policy state, tool request/result, and retry counters before a new task starts. |
| Task ID | Generated identifier stored on `TaskRecord` and loop state to attribute one user request inside a session (`goal_id == task_id`). |
| Task thread ID | Deprecated term for the former per-task checkpoint thread ID; replaced by the session-scoped thread ID plus per-task `task_id`. |
| TaskRecord | Session history entry for one user task, including task ID, task text, result, start time, and finish time. |
| Task lifecycle | One user request delegated to the engine, ending when `AgentLoopEngine` reaches a terminal `AgentLoopResult`. |
| Tool | Provider-neutral executable tool (`src/contracts.py`): `name`, async `func`, `description`, and JSON-Schema `input_schema`; `to_def()` yields the model-visible `ToolDef`, and `invoke()` dispatches with `**args`. |
| ToolCall | A single tool invocation proposed by an assistant `Message` (`id`, `name`, `arguments`). |
| ToolDef | Provider-neutral, model-visible tool schema (`name`, `description`, `input_schema`) independent of any provider. |
| ToolRegistry | Lazy registry that exposes tools from static lists, generic providers, browser providers, or MCP clients. |
| Trace replay | Loading `events.jsonl` records to summarize terminal status and print compact action sequences for diagnostics or eval failures. |
| Turn cap | `DEFAULT_TURN_CAP` (50) upper bound on engine turns before the run is blocked. |
