# Architecture Overview

AutoBrowser is a Python 3.12 browser automation agent with an engine-native
execution loop. The command is `autobrowser` (`src/cli/entry.py`; `main.py` calls the same
entry point); the core agent loop lives under `src/agent_loop/execution/`; session and
runtime infrastructure are managed from `src/harness/`; front ends (`src/ui/`) and the stand
(`src/serve/`) are clients of the session.

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
| `main.py` | Calls `src.cli.entry.main`, like the `autobrowser` console script. |
| `src/cli/` | The composition root: `entry.py` (subcommands `doctor`, `sessions`, `replay`, `recordings`, `serve`, `completion`, or the agent), parser, `bootstrap.py` (`build_session`, the front end), `models.py` (the model cascade's tiers over the provider registry), `serve.py` (the stand), print mode output, and the `cmd2` shell kept as `--classic`. The only importer of `src.providers` and `src.channels`. |
| `src/ui/` | Front ends over the session: `SessionController` (the UI-neutral facade), `RuntimeLoop`, the event feed projection, the inline terminal UI (`src/ui/tui/`, the default), plain line mode and print mode. Guide: [terminal UI](../development/terminal-ui.md). |
| `src/serve/` | The stand (`autobrowser serve`), a `SessionController` client: dispatcher over a worker pool, scheduler, HTTP API, probe, ledger, notification channels, the chat command port. Guide: [serve mode](../development/serve-mode.md). |
| `src/channels/` | Chat channel adapters of the stand (`telegram.py`) over the neutral ports of `src/serve`; imported by `src/cli/` only. |
| `src/agent_loop/execution/` | The engine-native control loop: `AgentLoopEngine`, `TurnController`, the frozen `LoopState`, observation/guards helpers, `EngineResources`, and the `Agent` that runs one `TaskRequest`. |
| `src/agent_loop/` | Runtime-facing action contracts, model action parsing, lifecycle events, replay/evals, metrics, batch/export helpers, context assembly, prompts, skills, and the `GoalRunner` lifecycle boundary. |
| `src/contracts.py` | Provider-neutral typed tool/plan/observation contracts (no imports from the loop, harness, or browser layers). |
| `src/config.py` | Typed environment-driven settings — the single source of truth for every tunable. Neutral leaf like `src/contracts.py`; see [the settings ADR](../decisions/2026-09-16-typed-settings-module.md). |
| `src/state.py` | Type-only `AgentState` TypedDict kept for annotation. |
| `src/messages.py` | Dependency-free provider-neutral chat `Message`/`ToolCall` types shared by the engine and providers. |
| `src/llm.py` | The provider-neutral `ChatModel`/`ModelResponse` chat contract the engine drives, plus `ModelPrice`, `ModelTier` and `ModelTiers` (the session model and the cascade tiers above it). |
| `src/providers/` | Thin `ChatModel` adapters — `ollama.py` (`/api/chat`) and `openai_compat.py` (Chat Completions) — that serialize `Message`/`ToolDef` to a wire protocol and parse replies into `ModelResponse` (with `ModelUsage`); `registry.py` picks one by `llm.provider` and probes it for `doctor --llm`. Imported only by `src/cli/`. |
| `src/browser/` | Browser adapters that read text formats, never tool names: page URL (`pages.py`), permission resources, memory scope and policy, hooks, playback (`[ref=…]` rebinding), error codes. |
| `src/harness/` | Session runtime, harness composition root, MCP setup, tools, permissions, hooks, memory, skills, recordings and playback, approvals, the event stream for front ends, and telemetry (OTLP). |
| `src/mcp/` | The universal MCP manager: server registry (stdio, streamable HTTP), connections, reconnects, discovery and shutdown; no server is special-cased. |
| `tests/` | Pytest coverage for engine behavior, harness boundaries, Agent Loop contracts, CLI, prompts, tools, batch/export, and deterministic eval scenarios. |
| `scripts/` | Utility scripts for batch runs and their comparison, session exports, trace replay/export, scripted evals with their baselines, and the Langfuse sync. |

The legacy `src/agent/` compiled-graph runtime, its `planner`/`executor`/`observer`
stages, the `src/agent_loop/adapters/` bridge, and
`src/cli/task_runner.py` were removed; see
[docs/decisions/2026-08-31-native-agent-loop-engine.md](../decisions/2026-08-31-native-agent-loop-engine.md).

## Engine Loop

The loop is assembled and driven by `AgentLoopEngine.run` in
`src/agent_loop/execution/loop.py`. Its verified flow is:

```text
build initial plan (model call #0, or a recorded plan for a replay) -> while turn <= cap:
  tier escalation check (model cascade only)
  TurnController.run_turn(LoopState) ->
    step source (replay) or agent step on the current model tier -> decision
      done    -> terminal status via CompletionController (stop hooks first)
      replan  -> rebuild plan
      tool_call -> progress guard -> prepare -> pre_tool_use hooks
                -> PermissionEngine (deny | ask -> permission_request/human | allow)
                -> ToolBroker.invoke -> post_tool_use -> observe
```

A progress block, a hook deny or a permission deny short-circuits back to the loop: the model
reads the reason as the tool output. A refused approval ends the task `blocked`. Otherwise the
tool runs through `ToolBroker` and the observer compiles the result back into `LoopState`.
`settings.loop.turn_cap` (default 50) bounds the loop, and the run budget (`RunBudget`) is
checked right before every model call. See the [runtime flow](../diagrams/agent-runtime-flow.md).

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
      -> Agent.run(TaskRequest)
          -> AgentLoopEngine
              -> TurnController
```

`SessionRuntime.start()` asks `SessionContext.build_agent()` for the session's
`Agent` (`EngineResources` from `BrowserHarness` plus the model and the
session-scoped hooks, permissions, approval judge, memory and tool roles). For each
task it allocates the task id, builds a `TaskRequest` (task, ids, carried state, turn
cap), stores the result's `session_state` in `SessionContext`, and marks task
history finished or failed. `GoalRunner(task_runner=agent.run, events, GoalLimits)`
owns the goal lifecycle boundary for that task: it emits `goal.started`, runs the
agent under the wall-clock timeout and the progress watchdog, reads the terminal
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
- `context.py` and `prompts.py` implement the canonical prompt-construction path:
  `ContextAssembler` builds the durable system prompt (the agent prompt plus the session's
  `PromptSection`s: the skill catalog and the always-on skills such as the browser rules), the
  per-turn user prompt, and the planner prompt from typed context blocks. Skills themselves
  live in `src/harness/skills.py` ([guide](../development/skills.md)).

Front ends never own the session ([ADR](../decisions/2026-10-19-session-controller-and-local-web-ui.md)).
`SessionController` (`src/ui/controller.py`) is the UI-neutral facade over `SessionRuntime`:
submit, cancel, reset, reconnect, answer approvals, `SessionView`/`TaskView` read models. Its
coroutines run on `RuntimeLoop`, one dedicated asyncio loop, so MCP resources are always
closed on the loop that opened them. The terminal UI (default), plain mode, print mode
(`-p`), the classic `cmd2` shell (`--classic`) and the stand all drive the session through it.

Session files live in the AutoBrowser home (`AUTOBROWSER_HOME`, else `~/.autobrowser`):
`<home>/sessions/<session_id>/` holds `session.json` (config, metadata, workspace paths,
artifacts, task history), `tasks.json`, `events.jsonl`, `state.json` (the carried dialogue for
`--continue`/`--resume`) and `workspace/` (`downloads/`, `screenshots/`, `temp/`,
`artifacts/`). They are local runtime artifacts, not part of the repository.

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
(plus `tool_normalizers` from the registry) for `AgentLoopEngine`, together with the
session-scoped ones: hooks, permissions, the approval judge, the memory renderer, the step
sources of replay (`steps`) and the model tiers (`models`). This keeps the engine focused on
reasoning/control flow, keeps task lifecycle separate from session lifecycle, and keeps
runtime concerns replaceable in tests ([diagram](../diagrams/harness-boundaries.md)).

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
servers are configured), fills the `{cdp_port}`/`{cdp_endpoint}` placeholders and
`{profile_dir}` (the session's browser profile), and fails startup if the browser server is
not ready. With `browser.launch: external` the session starts no Chrome: the MCP server
launches its own browser, and a server still pointing at CDP is a startup error. `src/harness/mcp_tools.py`
exposes catalog tools through `MCPToolSource`: the browser server keeps its
unprefixed `browser_*` names; other servers use `server__tool`.

`src/browser/` holds browser adapters that read text formats, never tool names:
the page URL from the latest output and the kept snapshots (`pages.py`), permission
resources (`permissions.py`), memory scope and policy (`memory.py`), hooks and error
codes. Kept snapshots are per tool server and follow the MCP `readOnlyHint`
([name-free tool handling ADR](../decisions/2026-10-19-name-free-tool-handling.md)).

All tasks in one interactive session share a session identity derived from
`SessionContext.session_id`, carried as `TaskRequest.session_id`. Each `TaskRecord`
still receives its own `task_id` for
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

The carried state reaches the next run as `TaskRequest.state_overrides`; there is
no task-config dict and no harness-internal key.

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

- `SchemaArgsNormalizer` removes arguments the tool schema disallows, for every server
  alike; no normalizer looks at tool names.

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

`--compress-tools` (`SessionConfig.compress_tools`) compacts the text of tool outputs before
they reach the history; no model is called for it. Kept snapshots are per server and follow
the MCP `readOnlyHint`.

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

## Replay, Budgets and the Model Cascade

- **Replay** ([guide](../development/replay.md)): a completed run can be recorded and replayed
  without the model. The engine asks a neutral `StepSource` (`EngineResources.steps`) before
  the model; every replayed step goes through the same guard, hooks and permissions; a
  divergence goes to a one-step retarget, then to the agent. The cost ladder is
  `probe -> playback -> retarget -> agent -> human`, never above `RunProfile.max_stage`.
- **Budgets**: every model call is added to `LoopState.usage` (`TaskUsage`, by stage and by
  model); `RunBudget` (calls, tokens, money from `llm.prices`) ends a task
  `budget_exhausted` before the call that would exceed it.
- **Model cascade** (`cascade.*`, off by default): tier 0 is the session model, the cascade
  tiers sit above it. `guards.tier_escalation`, checked at the top of every turn, moves the
  agent up one tier when it is stuck (an empty answer, a progress block, repeated failures or
  replans); the tier never goes down within a task
  ([ADR](../decisions/2026-11-09-cascade-channels-and-worker-pool.md)).

## The Stand

`autobrowser serve` ([guide](../development/serve-mode.md),
[diagrams](../diagrams/serve-mode.md)) runs configured jobs on triggers (interval, cron,
webhook, Telegram) without a terminal. `src/serve/` drives a pool of workers, each a whole
session behind its own `SessionController`; it never imports the engine, the harness, the
browser layer, the providers or the channels.

## Hooks, Memory and Skills

- **Lifecycle hooks** ([guide](../development/lifecycle-hooks.md)): deterministic checks at
  `goal_start`, `pre_tool_use`, `permission_request`, `post_tool_use`, `stop` and `goal_end`;
  off by default.
- **Memory** ([guide](../development/memory.md), [diagram](../diagrams/memory-layers.md)):
  the history budget, working notes, the task digest and persistent memory files; everything
  past the compaction of superseded tool outputs is off by default.
- **Skills** ([guide](../development/skills.md)): Agent Skills `SKILL.md` folders; the
  built-in browser rules are an always-on skill sent whenever a browser server is configured.

## Browser Tools

The agent works with whatever tools the configured MCP servers expose
(Playwright MCP by default); the direction is a universal agent without
tool- or site-specific hardcodes in the engine, guards or harness. Code, prompts
and evals use the tool names the browser server exposes; there is no canonical
`browser.*` vocabulary to translate from.

## Known Operational Risks

- Dynamic commerce pages can expose a search button before the editable input.
  The browser rules (the built-in `browser-agent-rules` skill and the agent prompt, both the
  user's to revise) limit repeated search-button clicks and allow a direct search URL
  fallback.
- The turn cap (`settings.loop.turn_cap`, default 50) can be reached when the agent repeats
  tool calls without progress. Retry counters, observer hints, policy checks,
  and prompt rules are the current controls.
- Tool-output compression must preserve enough page detail for safe follow-up
  actions.
- `AUTOBROWSER_FLAGS__AGENT_LOOP`/`SessionConfig.agent_loop` are inert compatibility
  flags; they parse but do not change routing and can be removed once external
  tooling stops referencing them.
