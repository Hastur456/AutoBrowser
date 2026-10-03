# Architecture Overview

AutoBrowser is a Python 3.12 browser automation agent with an engine-native
execution loop. The command-line entry point is `main.py`; the core agent loop
lives under `src/agent_loop/execution/`; session and runtime infrastructure are
managed from `src/harness/`.

## Goals

AutoBrowser turns a natural-language browser task into a controlled loop:

1. Plan a small set of task steps.
2. Select the next browser or non-browser tool action.
3. Apply policy checks before execution.
4. Execute the selected tool.
5. Observe the result and update compact state.
6. Repeat until a final answer is available.

## Source Layout

| Path | Responsibility |
| --- | --- |
| `main.py` | CLI parsing and wiring the process into `SessionRuntime`. |
| `src/cli/` | `cmd2` interactive REPL, parser, output formatting, and session bootstrap. |
| `src/agent_loop/execution/` | The engine-native control loop: `AgentLoopEngine`, `TurnController`, the frozen `LoopState`, completion/observation/guards/policy helpers, `EngineResources`, and `native_task_runner`. |
| `src/agent_loop/` | Runtime-facing action contracts, model action parsing, lifecycle events, replay/evals, metrics, batch/export helpers, context assembly, prompts, skills, and the `GoalRunner` lifecycle boundary. |
| `src/contracts.py` | Provider-neutral typed tool/plan/observation contracts (no imports from the loop, harness, or browser layers). |
| `src/config.py` | Typed environment-driven settings — the single source of truth for every tunable. Neutral leaf like `src/contracts.py`; see [the settings ADR](../decisions/2026-09-16-typed-settings-module.md). |
| `src/state.py` | Type-only `AgentState` TypedDict kept for browser-layer annotation. |
| `src/messages.py` | Dependency-free provider-neutral chat `Message`/`ToolCall` types shared by the engine and providers. |
| `src/llm.py` | The provider-neutral `ChatModel`/`ModelResponse` chat contract the engine drives. |
| `src/providers/` | Thin `ChatModel` adapters (for example `ollama.py`) that serialize `Message`/`ToolDef` to a backend wire format and parse replies into `ModelResponse`. |
| `src/browser/` | Provider-neutral browser contracts, canonical browser names, backend adapters, and fake browser tools for tests. |
| `src/harness/` | Session runtime, harness composition root, context, memory, tools, policy, and telemetry boundaries. |
| `src/mcp/` | Playwright MCP process/session lifecycle helpers and provider loading. |
| `tests/` | Pytest coverage for engine behavior, harness boundaries, Agent Loop contracts, CLI, prompts, tools, batch/export, and deterministic eval scenarios. |
| `scripts/` | Utility scripts for batch runs, session exports, trace replay/export, and eval baseline checks. |

The legacy `src/agent/` compiled-graph runtime, its `planner`/`executor`/`observer`
stages, the `src/agent_loop/adapters/` bridge, and
`src/cli/task_runner.py` were removed; see
[docs/decisions/2026-08-31-native-agent-loop-engine.md](../decisions/2026-08-31-native-agent-loop-engine.md).

## Engine Loop

The loop is assembled and driven by `AgentLoopEngine.run` in
`src/agent_loop/execution/loop.py`. Its verified flow is:

```text
build initial plan (model call #0) -> while turn <= cap:
  TurnController.run_turn(LoopState) ->
    agent step -> decision
      done    -> terminal status via CompletionController
      replan  -> rebuild plan
      tool_call -> policy -> (human_input?) -> ToolBroker.execute -> observe
```

A turn with a `tool_call` decision runs policy before execution. `policy` can
route to `human_input` when a tool needs human approval; a blocked or denied
tool short-circuits back to the loop with a status-prefixed final answer.
Otherwise the approved tool executes through `ToolBroker` and the observer
compiles the result back into `LoopState`, which continues or terminates.
`settings.loop.turn_cap` (default 50) bounds the loop.

## Runtime Boundary

`SessionRuntime` in `src/harness/session.py` owns the long-lived application
lifecycle and delegates session-owned state to `SessionContext`. The context is
the root object for one process session: it holds `SessionConfig`, `session_id`,
task history, current task, workspace, artifact registry, event bus, session
state, metadata, telemetry, tool registry, memory, LLM, and `BrowserHarness`.
Each user request is tracked as a `TaskRecord` and delegated as a task inside
the active session. When the loop reaches a terminal state, only that task
ends; the session returns to the input prompt and keeps the same runtime
resources and session-scoped context alive until the process exits.

The session layer is intentionally not a task solver. It manages interaction
lifecycle, resource ownership, and context handoff between tasks, while the
engine-native loop handles one task execution at a time. AutoBrowser models
session activity as tasks, with each task represented as a distinct user turn in
durable message history.

One-task execution passes through `GoalRunner` in `src/agent_loop/goals.py`:

```text
SessionRuntime
  -> GoalRunner
      -> native_task_runner
          -> AgentLoopEngine
              -> TurnController
```

`SessionRuntime` starts the session, allocates task and goal ids, builds task
config, injects carried session state, persists latest state into
`SessionContext`, and marks task history finished or failed. `GoalRunner` owns
the goal lifecycle boundary for that task: it emits `goal.started`, delegates to
`native_task_runner` (which composes `EngineResources` from `BrowserHarness`),
captures carry-forward state through a `LatestStateLoader`, reads the terminal
`AgentLoopResult.status` directly, emits `goal.completed`, `goal.blocked`,
`goal.cancelled`, or `goal.failed`, and returns a `GoalRunResult` with explicit
terminal status. The current `goal_id` is equal to the task id.

`GoalRunner` is not an agent engine. It does not choose actions, inspect model
messages for semantic completion, call tools, manage retry counters, enforce
browser policy, or consume stream chunks. `AgentLoopEngine` is the real
control-flow owner: it builds the initial plan, then drives a bounded `while`
loop of `TurnController` turns and returns a terminal `AgentLoopResult` with
`status`/`final_answer`/`session_state`/`state`/`turns`.

The transitional `src/agent_loop/outcomes.py` module is deleted. `GoalRunner`
consumes the engine's terminal `AgentLoopResult` directly — there is no
observation-compiler or completion-guard indirection between the engine result
and the goal lifecycle. The surviving provider-neutral status literals
(`CompletionStatus`/`GoalStatus`) and `goal_status_from_completion()` live in
`src/contracts.py`; the legacy `LegacyAgentStateObservationCompiler` was removed
once `AgentLoopResult` became the terminal contract.

Other `src/agent_loop/` modules are runtime-facing contracts and diagnostics
around the engine:

- `actions.py` and `model.py` define provider-neutral proposed actions and a
  model response parser/driver that drives the provider-neutral `ChatModel`
  contract.
- `events.py`, `replay.py`, and `metrics.py` provide durable event records
  (including the `AgentTraceSink` projection), replay summaries, and metric
  extraction.
- `batch.py`, `export.py`, and `evals.py` power Golden Set runs, session export
  rows, and deterministic fake-browser scenario checks.
- `context.py`, `prompts.py`, and `skills.py` implement the canonical prompt-construction
  path: `ContextAssembler` builds the durable system prompt, the per-turn user prompt, and the
  planner prompt from typed context blocks.

The interactive CLI in `src/cli/agent_cli.py` wraps a prepared
`SessionRuntime`. It keeps all asynchronous session operations on one dedicated
runtime event loop, including task execution, browser status helpers, reset,
and close. This avoids closing MCP resources from a different loop than the one
that created them.

Session workspace files live under
`.autobrowser/sessions/<session_id>/workspace/`, with standard subdirectories
for `downloads/`, `screenshots/`, `temp/`, and `artifacts/`. This directory is
runtime-local and ignored by git.

Session metadata is persisted next to the workspace under
`.autobrowser/sessions/<session_id>/`. `session.json` records the current
session view, including config, metadata, workspace paths, artifacts, and task
history. `tasks.json` stores the task records directly for simpler inspection.
These files are runtime artifacts and are ignored by git.

`BrowserHarness` in `src/harness/runtime.py` is the composition root for runtime
infrastructure consumed by one task execution. It holds:

- `ContextAssembler`: the sole prompt-construction boundary — durable system
  prompt injection, per-turn user prompt, and planner prompt.
- `ToolRegistry`: lazily loads static tools, generic providers, and live tool
  sources such as `MCPToolSource`, and exposes provider-neutral `Tool` objects
  plus the registered `ToolCallNormalizer`s.
- `TelemetryObserver`: logs local trace metadata and errors.
- `EventEmitter`: durable goal/model/action/policy/tool/observation events.

`EngineResources.from_harness(harness, llm=...)` bundles these collaborators
(plus `tool_normalizers` from the registry) for `AgentLoopEngine`. This keeps
the engine focused on reasoning/control flow, keeps task lifecycle separate from
session lifecycle, and keeps runtime concerns replaceable in tests.

Conversation history is not stored on the harness. The functional `MemoryManager`
in `src/harness/memory.py` shapes a `list[Message]` — seeding the user task,
appending assistant tool calls and tool results, compacting superseded tool
outputs, and formatting tool-message bodies — through module-level helpers the
engine calls. The durable history itself lives on `LoopState.messages` and in the
cross-task `SessionContext.state` carry-forward; there is no checkpoint saver.

## MCP Runtime and Browser Boundary

All tool servers go through the universal MCP Manager
([ADR](../decisions/2026-09-28-universal-mcp-manager.md),
[diagram](../diagrams/mcp-runtime.md)). `src/mcp/` is server-agnostic:
`ServerRegistry` holds the declarative `mcp_servers` entries (stdio or
streamable HTTP), `MCPManager` owns connections, reconnects, discovery and
shutdown, and the catalog holds discovered tools and `ServerStatus`.

`src/harness/mcp_setup.py` builds the session's `MCPRuntime` from settings
(falling back to Playwright MCP attached to the session Chrome over CDP when no
servers are configured), fills `{cdp_port}`/`{cdp_endpoint}` placeholders, and
fails startup if the browser server is not ready. `src/harness/mcp_tools.py`
exposes catalog tools through `MCPToolSource`: the browser server keeps its
unprefixed `browser_*` names; other servers use `server__tool`.

`src/browser/` now holds only the shared browser vocabulary: canonical
`browser.*` names (`names.py`), error codes (`errors.py`), typed contracts
(`contracts.py`), and `BrowserToolNormalizer` (`normalization.py`).
`BrowserProvider`/`FakeBrowserProvider` remain only as test scaffolding.

All tasks in one interactive session share a session identity derived from
`SessionContext.session_id`, passed into each task config as
`configurable.thread_id`. Each `TaskRecord` still receives its own `task_id` for
persisted task history and message attribution; there is no checkpoint
thread anymore, and `goal_id == task_id`.

After each task, `SessionRuntime` remembers the latest loop state in
`SessionContext.state` (from the terminal `AgentLoopResult.session_state`). The
next task receives only the context that is useful across task boundaries:
durable messages, latest observation, browser state, and last browser action
metadata. Task-local fields such as plan, terminal decision,
final answer, tool request/result, policy state, errors, and retry counters are
reset before the next invocation. This lets follow-up tasks use prior results
and browser progress without inheriting stale completion or failure state.

`BrowserHarness` owns the internal state-override channel
(`HARNESS_STATE_OVERRIDES_CONFIG_KEY`) used to inject this carried session state
into the next `AgentLoopEngine.run` call. That internal key is stripped from the
task config before the engine sees it.

## Planner

The planner is intentionally compact. It should create practical plans rather
than long procedural scripts. Current prompt constraints prefer one to three
steps and explicitly avoid splitting a search task into separate locate,
inspect, type, and submit microsteps unless fallback handling is needed.

For search tasks, the plan must preserve this contract:

- locate an editable search input;
- inspect whether it already contains the requested query;
- type or replace the query only if needed;
- submit the search;
- verify and extract visible results.

## Agent Reasoning

The reasoning step uses the current task, task ID, plan, latest observation,
retry counters, and durable message history.
It must return either a native tool call, a JSON replan decision, or a JSON done
decision. `TurnController._agent_step` maps a parsed `ProposedAction` to a flat
`LoopState` update; terminal status is derived from the resulting state by
`CompletionController`, never decided by the model step itself.

The system prompt emphasizes:

- the fewest useful browser actions;
- early fallback to direct search URLs when repeated search affordance clicks
  do not expose an input;
- immediate completion once extracted data satisfies the user request.

## Executor

The executor resolves the requested tool through `ToolRegistry` (via
`ToolBroker`). Browser request and result normalization is delegated to
stateless `ToolCallNormalizer`s ([diagram](../diagrams/browser-provider-boundary.md)):

- `BrowserToolNormalizer` resolves canonical `browser.*` names to the tool the
  browser server exposes;
- `SchemaArgsNormalizer` removes arguments the tool schema disallows.

A tool that reports `isError` raises `MCPToolExecutionError`, and the broker propagates any
`error_code` into the `ToolResult`. Every executed call is recorded in the
server-neutral action journal
([ADR](../decisions/2026-09-28-server-neutral-progress-journal.md)), which feeds
repeat detection, the `Action History` context block, and policy blocking of
identical repeats.

Tool success and failure are normalized into `ToolResult` state.

## Observer

The observer translates a single tool result into compact loop state and
detects ineffective browser actions (an action that leaves the observed page
unchanged).

When compression is enabled, an observer LLM can summarize tool output, but it
must use only the latest `ToolResult` JSON.

## Policy

Two separate checks run before a tool call executes:

- The **progress guard** (`src/agent_loop/execution/guards.py`, event `policy.decided`)
  blocks a missing tool request and a call whose identical outcome already repeated
  `loop.max_ineffective_actions` times. It is quality control, not authorization.
- The session-scoped **`PermissionEngine`** (`src/harness/permissions.py`, event
  `permission.decided`) authorizes the final normalized call after `pre_tool_use` hooks:
  the configured `permissions.rules` (`deny > ask > allow`; no rule ships with the code and
  the engine names no tool, so out of the box nothing asks), the mode (`default`, `read_only`, `dont_ask`, `bypass`) and session grants. A deny is returned
  to the model as the tool output; an ask goes to `permission_request` hooks and the human.
  See [ADR](../decisions/2026-10-01-permission-engine.md) and
  [Name-Free Permission Defaults](../decisions/2026-10-01-name-free-permission-defaults.md).

## Browser Tools

The agent works with whatever tools the configured MCP servers expose
(Playwright MCP by default); the direction is a universal agent without
tool- or site-specific hardcodes in the engine, guards or harness. Code, prompts
and evals use the tool names the browser server exposes; there is no canonical
`browser.*` vocabulary to translate from.

## Known Operational Risks

- Dynamic commerce pages can expose a search button before the editable input.
  Prompt rules now limit repeated search-button clicks and allow direct search
  URL fallback, especially for Ozon.
- The turn cap (`settings.loop.turn_cap`, default 50) can be reached when the agent repeats
  tool calls without progress. Retry counters, observer hints, policy checks,
  and prompt rules are the current controls.
- Tool-output compression must preserve enough page detail for safe follow-up
  actions.
- `AUTOBROWSER_FLAGS__AGENT_LOOP`/`SessionConfig.agent_loop` are inert compatibility
  flags; they parse but do not change routing and can be removed once external
  tooling stops referencing them.
