# Glossary

| Term | Meaning |
| --- | --- |
| Action History | Context block rendered by `ContextAssembler` from the action journal: recent tool calls with argument/result previews and repeat counts. |
| Action journal | Server-neutral per-task record of executed tool calls (`ActionRecord`: `args_key`, `outcome_key`, `occurrence`) in `src/agent_loop/execution/progress.py`, used to detect repeats. |
| Agent Loop contracts | Runtime-facing contracts under `src/agent_loop/` for proposed actions, model turns, events, traces, metrics, context assembly, batch/export, evals, and goal lifecycle boundaries around the engine-native loop. |
| Agent step | The reasoning phase of a `TurnController` turn that chooses a tool call, a replan, or a done decision. |
| AgentLoopEngine | The explicit control-flow owner in `src/agent_loop/execution/loop.py`: builds the initial plan, then drives a bounded `while` loop of `TurnController` turns and returns a terminal `AgentLoopResult`. |
| AgentLoopResult | Frozen terminal result of one engine run: `status` (`done`/`blocked`/`cancelled`), `final_answer`, `session_state`, `state`, and `turns`. |
| AgentTraceSink | Event sink that writes compact human-readable trace projections beside durable event records. |
| ArtifactRegistry | Session-owned registry for durable outputs such as screenshots, downloads, reports, or extracted files. |
| Assembled context | Deterministic, ordered prompt blocks produced by `ContextAssembler` (`src/agent_loop/context.py`), the canonical prompt-construction path. |
| AutoBrowser | The browser automation agent implemented in this repository. |
| Batch run | Execution of JSONL Golden Set scenarios through fresh `SessionRuntime` instances, with metadata written under `.autobrowser/batches/<batch_id>/`. |
| Browser error code | Shared browser-layer error vocabulary such as `invalid_ref` and `action_failed`. |
| `browser_evaluate` | Playwright MCP page-JavaScript tool; it asks only if a configured rule says so (see the opt-in `page-js` example in `config.example.yaml`). |
| `browser_find` | Browser tool for plain-text search; not reliable for structured link or attribute extraction. |
| BrowserAction | Provider-neutral typed browser request using canonical `browser.*` action names. |
| BrowserHarness | Runtime composition root that holds context, tools, policy, telemetry, events, and the reasoning `llm`; `EngineResources.from_harness` bundles them for the engine. |
| BrowserProvider | Legacy protocol (`src/browser/provider.py`) kept only for test scaffolding; superseded in production by `MCPToolSource` and `ToolCallNormalizer`. |
| BrowserResult | Provider-neutral browser action result shape with status, content, error, and optional error code. |
| BrowserToolNormalizer | `ToolCallNormalizer` in `src/browser/normalization.py` that maps canonical `browser.*` names to the tool the browser server actually exposes. |
| ChatModel | Provider-neutral chat protocol in `src/llm.py`: `async complete(messages, *, tools, **params) -> ModelResponse`. Provider adapters implement it; the engine drives it and never sees provider objects. |
| Checkpointer | Removed. There is no checkpoint saver; durable history is carried on `LoopState.messages` and `SessionContext.state`, shaped by the functional `MemoryManager`. |
| Command hook | Hook with `type: command` (`src/harness/command_hooks.py`): an external process that gets the `HookEvent` as JSON on stdin and answers with its exit code (`2` blocks, stderr is the reason) and optional JSON on stdout, following the Claude Code/Codex protocol. |
| Compact observation | Short observer output derived from a tool result and used by the next agent step. |
| CompletionStatus | Loop completion status (`continue`/`done`/`blocked`/`cancelled`) carried on `AgentLoopResult`; `GoalRunner` maps it to a terminal `GoalStatus` via `goal_status_from_completion()` in `src/contracts.py`. |
| Config section | One of the ten frozen pydantic sub-models on `Settings` (`llm`, `browser`, `loop`, `observation`, `memory`, `events`, `storage`, `flags`, `hooks`, `permissions`). Each owns an `AUTOBROWSER_<SECTION>__<FIELD>` environment namespace and rejects unknown keys. |
| ContextAssembler | The sole prompt-construction boundary in `src/agent_loop/context.py`: builds the durable system prompt, the per-turn user prompt, and the planner prompt from ordered `ContextBlock`s. |
| Direct search URL fallback | Navigating directly to a site's search results URL when UI search controls do not make progress. |
| EngineResources | Bundled runtime collaborators (`llm`, `tool_registry`, `tool_normalizers`, `context`, `events`, `hooks`, `permissions`, `approval`, `memory`) built from `BrowserHarness` (plus the session's `HookEngine`, `PermissionEngine`, `ApprovalJudge` and `MemoryContext`) and passed to `AgentLoopEngine`. |
| EventRecord | Durable JSON-safe event envelope for session, goal, engine, model, action, policy, tool, observation, and terminal lifecycle events. |
| Executor | Engine phase that resolves and invokes approved tool requests through `ToolBroker`/`ToolRegistry`. |
| Export row | JSONL task-level analytics row produced by `scripts/export_sessions.py` from persisted session, task, event, feedback, and batch metadata. |
| Fail-closed | Hook failure mode where a timeout or exception counts as `deny`. The default for `goal_start` and `pre_tool_use`; other events fail open (no decision). `HookSpec.fail_closed` overrides it. |
| FakeBrowserProvider | Legacy deterministic browser provider used by tests to replay browser responses without Chrome, CDP, or MCP. |
| GoalRunner | One-task lifecycle boundary between `SessionRuntime` and the engine; emits goal lifecycle events, delegates execution through `native_task_runner`, captures latest state through `LatestStateLoader`, and does not own the model/action loop. |
| GoalRunRequest | Immutable input object for one `GoalRunner` execution, including task text, task id, goal id, session thread id, task config, and state overrides. |
| GoalRunResult | Immutable terminal object returned or internally constructed by `GoalRunner`, including raw task result or exception, latest state, and explicit terminal status. |
| GoalStatus | Terminal goal lifecycle status (`completed`/`failed`/`cancelled`/`blocked`) returned by `GoalRunner` in `GoalRunResult`, derived from the engine's `CompletionStatus`. |
| Harness | Runtime layer around the engine; owns infrastructure that should not be hardcoded into loop code. |
| Hook | Deterministic check registered in `hooks.registry` (`src/harness/hooks.py`) that can deny/ask/rewrite a tool call, rewrite its output, add model context, or reject a premature completion at a fixed lifecycle point. Disabled by default. |
| Hook event | One lifecycle point a hook runs on (`goal_start`, `pre_tool_use`, `permission_request`, `post_tool_use`, `post_tool_use_failure`, `stop`, `goal_end`), and the neutral `HookEvent` object handed to it. |
| Hook handler | Async callable `HookEvent -> HookResult \| None`: for `type: python` named by `package.module:attr` (a factory when `options` are set), for `type: command` a `CommandHook` wrapping an external process; `None` means no opinion. |
| HookEngine | Session-scoped runner of the hook registry: sequential handlers, `deny > ask > allow` aggregation, per-hook timeouts, one `hook.decided` event per handler via the loop. `NullHookEngine` is the disabled no-op. |
| Ineffective browser action | A successful browser action after which the observed page is unchanged. |
| LatestStateLoader | Callable port injected into `GoalRunner` to load latest loop state from the current harness/config, with fallback to the task result's session state. |
| LoopState | The frozen dataclass (in `src/agent_loop/execution/state.py`) that carries all loop state; `apply()` is strict and rejects unknown keys. |
| MCP | Model Context Protocol; every tool server (Playwright included) is an entry in `Settings.mcp_servers` managed by `MCPManager`. |
| MCPManager | Server-agnostic host-side pool of MCP clients in `src/mcp/manager.py`: connection owner tasks, reconnect, discovery, `list_changed`, liveness, shutdown. |
| MCPRuntime | Session-level bundle from `src/harness/mcp_setup.py`: manager, `MCPToolSource`, browser server name, and normalizers; started once per session. |
| MCPToolSource | Bridge in `src/harness/mcp_tools.py` that exposes the manager catalog as invocable tools (browser server unprefixed, others `server__tool`). |
| History budget | `memory.history_budget_chars`: past it `MemoryManager.apply_history_budget` replaces the oldest tool outputs with a `[cleared]` placeholder (keeping the `tool_call_id`), never the newest `keep_recent_tool_results` or a non-tool message. Off (`0`) by default. |
| Memory block | The `Memory` context block (user role, priority 15) rendered by `MemoryContext`: a header, the generated index and the bodies of the entries for the current site; absent when persistent memory is off. |
| Memory entry | One persistent memory file (`MemoryEntry` in `src/contracts.py`): `sites/<domain>.md` or `procedures/<name>.md` with harness-owned frontmatter (`scope`, `status`, `source`, `description`, `verified_at`, `uses`, `failures`) and a markdown body. |
| Memory tools | `memory_view` (`readOnlyHint`) and `memory_write` (`create`/`str_replace`/`delete`) on `server: memory` (`src/harness/memory_tool.py`), registered when `memory.tool_enabled`; they pass through hooks and the `PermissionEngine` like any MCP tool. |
| MemoryConsolidator | Opt-in (`memory.consolidate_on_goal_end`) session-side model call after a `done` task that sees the current bodies of the visited sites' entries and proposes at most `memory.consolidation_max_entries` merged entries, written `unverified` through the content policy with their trust counters kept (`src/harness/memory_consolidation.py`). |
| MemoryContentPolicy | Protocol (`src/contracts.py`) that refuses text which must never be persisted; the browser implementation `BrowserMemoryPolicy` rejects page-specific hints, prompt-injection phrases and secret-like pairs. |
| MemoryContext | Session-scoped renderer of the `Memory` block (`render(state) -> str`), reaching the engine as `EngineResources.memory`; `NullMemoryContext` renders nothing. |
| MemoryManager | Functional (stateless) history service in `src/harness/memory.py` that shapes a `list[Message]` — seeding the user task, appending tool calls/results, compacting superseded tool outputs, applying the history budget, digesting finished tasks — and returns new lists; the durable history lives on `LoopState.messages`, not on the service. |
| MemoryStore | Persistent memory files under `<storage.root_dir>/<memory.dir>/` (`src/harness/memory_store.py`): cached reads, path safety, the generated index, scope lookup, policy-checked writes and staged trust. |
| Message | Provider-neutral chat message (`src/messages.py`) with a `system`/`user`/`assistant`/`tool` role; assistant messages may carry `tool_calls`, and a `tool` message pairs a result back to exactly one `ToolCall.id`. |
| ModelResponse | Canonical provider-neutral model reply (`content` and/or `tool_calls`, plus `finish_reason`) returned by a `ChatModel`. |
| Observer | Engine phase that translates tool results into compact loop state updates. |
| Ollama provider | Thin `ChatModel` adapter in `src/providers/ollama.py` (`OllamaChatModel` / `ollama_llm_factory`) that maps neutral `Message`/`ToolDef` objects to Ollama's `/api/chat` shape and parses replies into `ModelResponse`. |
| Planner | Engine phase that creates or revises compact task plans. |
| Playwright MCP | Browser automation MCP server the agent uses by default. |
| PlaywrightMCPBrowserProvider | Removed. Former Playwright adapter; replaced by `MCPManager` + `MCPToolSource` + `BrowserToolNormalizer`. |
| Policy | The gate result of a tool turn stored on `LoopState.policy_decision` (`approved`, `needs_human`, `blocked`). `blocked` comes from the progress guard, a hook deny or a permission deny; `policy.decided` is emitted only by the progress guard. |
| PermissionEngine | Session-scoped, deterministic tool authorization in `src/harness/permissions.py`: `PermissionCheck` → `allow`/`ask`/`deny` from rules (`deny > ask > allow`), the mode and session grants; fails closed; emits `permission.decided`. |
| Permission mode | `permissions.mode`: `default` (no rule → allow), `read_only` (only `readOnlyHint` tools), `dont_ask` (every ask → deny), `bypass` (asks granted; deny rules and `always_ask` hold). |
| Grant | A session-only approval of `(server, tool, domain)` stored by the `PermissionEngine` after a human answers "session"; never covers `always_ask` rules or hook asks. |
| ProposedAction | Provider-neutral model action contract (`answer`/`tool_call`/`update_plan`/`ask_user`/`delegate`/`compact_memory`/`stop`) parsed from a model turn and mapped to `LoopState` updates by the engine. |
| Provider adapter | Thin adapter that implements `ChatModel` by serializing neutral `Message`/`ToolDef` objects to a backend wire format and parsing the reply back into a `ModelResponse`. |
| Staged trust | Memory entry lifecycle `unverified → verified → stale` driven by the outcomes of the tasks that loaded the entry (`MemoryStore.record_outcome`) and a `stale_after_days` TTL; the user's entries never change. |
| Task digest | One `[harness] Previous task digest` message (request, final answer, tool counts) replacing a finished task older than `memory.keep_recent_tasks` at the task boundary (`MemoryManager.digest_tasks`). |
| Working notes | Task-local notes the model keeps through the optional `notes` tool argument (`memory.working_notes_max_chars`), stored on `LoopState.working_notes` and rendered as the `Working Notes` block. |
| Qualified tool name | `server__tool` name produced by `src/mcp/naming.py` (sanitized, max 64 chars) for tools of non-browser servers. |
| readOnlyHint | MCP tool annotation used by `tool_is_read_only` to decide whether a call can change the page. |
| SchemaArgsNormalizer | `ToolCallNormalizer` in `src/harness/normalization.py` that drops arguments the tool schema forbids. |
| ServerRegistry | Declarative MCP server list (`StdioServerConfig` / `StreamableHttpServerConfig`) in `src/mcp/config.py`; holds no connections. |
| Session records | Runtime-local JSON files under `.autobrowser/sessions/<session_id>/`, currently `session.json` and `tasks.json`. |
| Session workspace | Runtime-local directory under `.autobrowser/sessions/<session_id>/workspace/` for downloads, screenshots, temp files, and artifacts. |
| Session-scoped thread ID | Stable `configurable.thread_id` derived from `SessionContext.session_id` and reused for all tasks in one interactive session; used for attribution, not a checkpoint thread. |
| SessionConfig | Args-derived configuration used to initialize a long-lived `SessionRuntime` and shared task config. |
| SessionContext | Root object for one process-scoped session; owns session state, task history, workspace, artifacts, events, and runtime handles. |
| SessionEventBus | Minimal synchronous event bus for session lifecycle events such as task start, task finish, and session close. |
| SessionMetadata | Session-owned metadata such as started time, last activity, task count, and runtime version. |
| SessionRuntime | Process-lifetime coordinator that runs the session loop, delegates lifecycle state to `SessionContext`, and sends each task to `GoalRunner`. |
| SessionState | Mutable mapping wrapper for shared session-level state that should not require a dedicated typed field yet. |
| Settings | The pydantic-settings root in `src/config.py`, composed of eight config sections and read through `get_settings()`. A neutral leaf like `src/contracts.py`: it imports nothing from the loop, harness, or browser layers. |
| State override channel | Harness-internal config entry (`HARNESS_STATE_OVERRIDES_CONFIG_KEY`) used to inject carried session state into the next engine run; stripped before the engine sees the task config. |
| Stateful server | MCP server declared `stateful: true`; its reconnect is surfaced as `ServerConnectionLostError(stateful=True)` rather than hidden. |
| Task boundary reset | Clearing task-local loop fields such as plan, final answer, errors, policy state, tool request/result, and retry counters before a new task starts. |
| Task ID | Generated identifier stored on `TaskRecord` and loop state to attribute one user request inside a session (`goal_id == task_id`). |
| Task lifecycle | One user request delegated to the engine, ending when `AgentLoopEngine` reaches a terminal `AgentLoopResult`. |
| Task thread ID | Deprecated term for the former per-task checkpoint thread ID; replaced by the session-scoped thread ID plus per-task `task_id`. |
| TaskRecord | Session history entry for one user task, including task ID, task text, result, start time, and finish time. |
| Tool | Provider-neutral executable tool (`src/contracts.py`): `name`, async `func`, `description`, and JSON-Schema `input_schema`; `to_def()` yields the model-visible `ToolDef`, and `invoke()` dispatches with `**args`. |
| ToolCall | A single tool invocation proposed by an assistant `Message` (`id`, `name`, `arguments`). |
| ToolCallNormalizer | Stateless protocol folded by `ToolBroker` around every call (`normalize_request` before, `normalize_result` after). |
| ToolDef | Provider-neutral, model-visible tool schema (`name`, `description`, `input_schema`) independent of any provider. |
| ToolRegistry | Lazy registry that exposes tools from static lists, generic providers, browser providers, or MCP clients. |
| Trace replay | Loading `events.jsonl` records to summarize terminal status and print compact action sequences for diagnostics or eval failures. |
| Turn cap | `settings.loop.turn_cap` (default 50) upper bound on engine turns before the run is blocked; set via `AUTOBROWSER_LOOP__TURN_CAP`. |
