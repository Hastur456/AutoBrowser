# Repository Guidelines


## Action-First Rule
For actionable requests such as run, test, check, inspect, search, open, verify, fix, or debug:

- Execute the first relevant tool call in the same turn.
- Do not end the turn with commentary only.
- Do not reply with text like "I’ll check", "I’ll run", "Let me inspect", or "Проверю" unless the tool call has
already been made in that same turn.
- If a short progress update is sent, it must be immediately followed by a tool call in the same turn.
- If execution is impossible, state a concrete blocker instead of a status update.
- For actionable requests, tool execution takes priority over narration.

This rule overrides any instruction that suggests sending a progress update before doing the work.

<!-- CODEGRAPH_START -->
## CodeGraph

This repository is indexed by CodeGraph (`.codegraph/` exists at the repo root). Reach for CodeGraph before `rg`, `find`, or direct file reads when you need to understand or locate code.

- Prefer `codegraph explore "<symbol names or question>"` for architecture, flows, and symbol lookup.
- Prefer `codegraph node <symbol-or-file>` when you need one symbol or one file with line numbers.
- Use raw shell search only for non-code assets, docs, generated files, or details CodeGraph does not cover.
<!-- CODEGRAPH_END -->

## Project Structure & Module Organization

This is a Python 3.12 repository for an AutoBrowser browser automation agent with an
engine-native execution loop. The command is `autobrowser` (`src/cli/entry.py`; `main.py` calls
the same entry point). Core code lives in `src/`:

- `src/agent_loop/execution/`: the engine-native control loop — `AgentLoopEngine`,
  `TurnController`, the frozen `LoopState`, guards (completion, progress, tier escalation),
  observation, the tool broker, usage and budgets, the approval judge, working notes,
  `EngineResources`, and the `Agent` that runs one `TaskRequest`.
- `src/agent_loop/`: runtime-facing action contracts, model action parsing, eventing, replay/evals, metrics, batch/export helpers, context assembly, prompts, skills, and the `GoalRunner` lifecycle boundary around the engine.
- `src/contracts.py`: provider-neutral typed tool/plan/observation contracts and loop thresholds (no imports from the loop, harness, or browser layers).
- `src/state.py`: the type-only `AgentState` TypedDict kept for annotation.
- `src/messages.py`: dependency-free provider-neutral chat `Message`/`ToolCall` types shared by the engine and providers.
- `src/llm.py`: the provider-neutral `ChatModel`/`ModelResponse` chat contract and the model
  tiers (`ModelPrice`, `ModelTier`, `ModelTiers`). Model defaults live in `src/config.py`
  (`settings.llm`, `settings.cascade`).
- `src/providers/`: provider adapters that implement `ChatModel` by mapping neutral `Message`/`ToolDef` objects to a wire protocol — `ollama.py` (`/api/chat`) and `openai_compat.py` (any Chat Completions service) — plus `registry.py` (`build_chat_model`, picked by `llm.provider`; `probe_chat_provider` for `doctor --llm`). Only `src/cli/` imports this package; no vendor names or URLs live in `src/` (see `docs/decisions/2026-10-22-openai-compatible-chat-provider.md`).
- `src/browser/`: browser adapters that read text formats, never tool names: page URL (`pages.py`), permission resources, memory scope and content policy, browser hooks, playback (`[ref=…]` rebinding), error codes.
- `src/cli/`: the `autobrowser` entry point (`entry.py`), parser, bootstrap wiring that picks the front end, and the `cmd2` shell kept as `--classic`.
- `src/ui/`: front ends over the session — `SessionController`, `RuntimeLoop`, the event feed projection, the inline terminal UI (`src/ui/tui/`), plain line mode and the launcher. Guide: `docs/development/terminal-ui.md`.
- `src/harness/`: session runtime and runtime infrastructure bundled into `EngineResources` for the engine.
- `src/serve/`: the stand (`autobrowser serve`), a `SessionController` client: dispatcher over a worker pool, scheduler, HTTP API, probe, ledger, channels, the chat command port. It imports no engine, browser, harness, CLI, provider or channel module; `src/cli/serve.py` composes it. Guide: `docs/development/serve-mode.md`.
- `src/channels/`: chat channel adapters of the stand (`telegram.py`), over the neutral ports of `src/serve` (`commands`, `notify`, `clock`); only `src/cli/` imports this package, and vendor names and URLs live here only.
- `src/cli/models.py`: builds the model cascade's tiers (`cascade.*`) with the provider registry; the session gets them as a `CascadeFactory`, the engine as `EngineResources.models`.
- `src/mcp/`: the universal MCP manager (server registry, connections, reconnects, discovery, shutdown); nothing here special-cases a server.
- `docs/`: architecture, development setup, decisions, diagrams, research notes, and glossary.

Tests live in `tests/`. Utility scripts live in `scripts/`, including batch runs and their comparison, session exports, event-trace replay, agent-trace export, scripted evals with baselines, the Langfuse sync and the ready command hooks (`scripts/hooks/`). The stand's Docker image is `Dockerfile`, `compose.yaml` and `docker/`. Runtime or local-only folders such as `.venv/`, `.pytest_cache/`, `node_modules/`, `.codegraph/`, `.playwright-mcp/`, `.autobrowser/`, `profile/`, `baseline/`, and `__pycache__/` should not be treated as source.

## Harness Architecture

The engine-native `AgentLoopEngine` owns the agent loop: planning, reasoning,
routing, execution, and observation. Infrastructure belongs in `src/harness/`
and is bundled into `EngineResources` for the engine by `BrowserHarness`.

Harness responsibilities:

- `session.py`: owns the process-long session lifecycle through `SessionRuntime` and `SessionContext`.
- `runtime.py`: composition root that holds the infrastructure collaborators `EngineResources.from_harness` reads; it no longer compiles/runs/streams a graph.
- `context.py` no longer exists in `src/harness/`: prompt construction lives in `ContextAssembler` (`src/agent_loop/context.py`), the sole boundary injected as `harness.context`.
- `memory.py`: functional conversation-history shaping over `Message` lists (no checkpoint saver; the durable history lives on `LoopState.messages`, not on a memory service).
- `tools.py`: pluggable tool registry for static tools, generic providers and live sources such as `MCPToolSource`.
- `permissions.py`: session-scoped `PermissionEngine` — tool authorization rules, modes, task scope and approval grants.
- `hooks.py`, `command_hooks.py`, `builtin_hooks.py`: lifecycle hooks.
- `approvals.py`: `ApprovalBroker`, the ticket-based human-approval channel every front end answers.
- `event_stream.py`: `EventBroadcaster`, the session event sink front ends subscribe to.
- `memory_store.py`, `memory_tool.py`, `memory_consolidation.py`: persistent memory; `skills.py`, `skill_tool.py`: Agent Skills.
- `recorder.py`, `recording_store.py`, `playback.py`: recording and replaying runs.
- `otel_sink.py`, `otel_spans.py`: OTLP trace export; `telemetry.py`: local trace-metadata and error logging.

`ContextAssembler` in `src/agent_loop/context.py` is the only prompt-construction
path — it builds the durable system prompt, the assembled per-turn user prompt, and
the planner prompt. The former context-mode switch (`legacy` vs `assembled`) and the
legacy `ContextBuilder` (with its `.format(...)`-based user prompt) were removed.

The engine-native migration is complete: the legacy `src/agent/` compiled-graph runtime, the
`src/agent_loop/adapters/` bridge, `src/cli/task_runner.py`, and the transitional
`src/agent_loop/outcomes.py` compatibility layer (the `GoalState` compile/guard indirection
and the legacy `LegacyAgentStateObservationCompiler`) are removed — `GoalRunner` now consumes
the terminal `AgentLoopResult` directly — and `AgentLoopEngine` is the sole runtime (see
[docs/decisions/2026-08-31-native-agent-loop-engine.md](docs/decisions/2026-08-31-native-agent-loop-engine.md)).
`AUTOBROWSER_FLAGS__AGENT_LOOP`/`SessionConfig.agent_loop` are inert compatibility surface.

Model access goes through the
provider-neutral `ChatModel` contract in `src/llm.py`, implemented by thin provider adapters in
`src/providers/` (`ollama.py`, `openai_compat.py`); tools are neutral `Tool`/`ToolDef` objects defined in
`src/contracts.py`; and conversation history is carried as provider-neutral `Message` lists
(`src/messages.py`, on `LoopState.messages`) with no checkpoint saver.

Do not hardcode Playwright MCP behavior into the agent loop. Tool servers are entries in `mcp_servers`, run by the universal MCP Manager, and reach `ToolRegistry` through `MCPToolSource`, so they can be swapped or mocked in CI. Keep the engine-native contracts and the frozen `LoopState` stable unless a change explicitly requires touching them.

## MCP and Browser Architecture

See `docs/decisions/2026-09-28-universal-mcp-manager.md`.

- `src/mcp/`: server-agnostic MCP Manager (`MCPManager`, `ServerRegistry`, stdio/streamable HTTP configs, catalog, typed errors, `server__tool` naming). Nothing here may special-case a server.
- `src/harness/mcp_setup.py`: builds the session `MCPRuntime` from settings (default: Playwright MCP over CDP), fills `{cdp_port}`/`{cdp_endpoint}` and `{profile_dir}`, refuses a server still on CDP under `browser.launch: external`, fails startup when the browser server is not ready.
- `src/harness/mcp_tools.py`: `MCPToolSource`/`MCPTool` bridge; the browser server keeps unprefixed `browser_*` names.
- `src/harness/normalization.py`: `ToolCallNormalizer` protocol and `SchemaArgsNormalizer`.
- `src/browser/`: browser adapters that read text formats, never tool names: `pages.py` (page URL from the latest output and kept snapshots), `permissions.py` (permission resources), `memory.py` (memory scope and content policy), `hooks.py`, `errors.py`.

`ToolBroker` folds every call through the registered normalizers (request before, result after). There is no canonical `browser.*` vocabulary: tests and evals use the names the browser server exposes.

## Session Context Architecture

AutoBrowser is a long-lived interactive session, not a single-shot task runner. `SessionRuntime` owns the process lifecycle and delegates session-owned state to `SessionContext`.

All tasks in one interactive session share a session identity derived from `SessionContext.session_id`, carried as `TaskRequest.session_id`. Each user request still gets its own `TaskRecord.task_id` for task history and message attribution, and `goal_id == task_id`.

After each task, `SessionRuntime` remembers the latest loop state in `SessionContext.state` (from the terminal `AgentLoopResult.session_state`). The next task carries forward only session-useful context:

- durable `messages`;
- latest `observation`;
- browser state;
- last browser action metadata needed for ineffective-action checks.

Before a new task starts, task-local fields must be reset so stale completion or retry state is not inherited:

- `plan`, `current_step`, `decision`, and `final_answer`;
- `tool_request`, `tool_result`, `policy_decision`, and `policy_event`;
- `error`, retry counters, replan counters, repeat counters, and ineffective-action counters.

The carried state reaches the next run as `TaskRequest.state_overrides`; there is no
task-config dict and no harness-internal key. The production agent is composed once per
session by `SessionContext.build_agent()`.

Preserve this boundary: the session layer manages lifecycle and context handoff,
while the engine-native loop still owns planning, reasoning, tool execution,
observation, and task completion.

## Build, Test, and Development Commands

Install dependencies with [uv](https://docs.astral.sh/uv/):

```powershell
uv sync
```

`uv sync` creates `.venv` from `uv.lock` (Python 3.12 is fetched if missing) and keeps it
exactly in line with the lock, removing packages that are not declared. Run everything
through `uv run`, no activation needed. `requirements.txt` is exported from the lock
(`uv export --format requirements.txt --no-hashes --no-annotate --no-header --no-emit-project
-o requirements.txt`) for a plain pip fallback.

Run the test suite with:

```powershell
uv run pytest
```

Run targeted tests with `uv run pytest tests\path\to_test.py`.

The default run is offline: `pyproject.toml` registers the markers `live` (real model, browser
or network), `bench` (timings), `conpty` (Windows console only) and `ui` (everything under
`tests/ui`), runs with `--strict-markers` and deselects `live` and `bench`. Select them
explicitly, e.g. `uv run pytest -m live` (a `-m` on the command line replaces the default).
The merge gate `.github/workflows/gate.yml` runs `uv sync --locked`, `uv run pytest` and
`scripts/run_evals.py` against `tests/evals/baselines/agent_loop_v2.json` (with and without
`tests/evals/memory_seed`) on `windows-latest`.

Useful focused test commands:

```powershell
uv run pytest tests\test_harness_session.py tests\test_harness_runtime.py
uv run pytest tests\test_main_cli.py
uv run pytest tests\test_prompts.py
uv run pytest tests\test_browser_contracts.py tests\test_schema_args_normalizer.py tests\test_mcp_manager.py tests\test_mcp_tools_bridge.py
uv run pytest tests\test_agent_loop_events.py tests\test_agent_loop_replay.py tests\test_agent_loop_metrics.py tests\test_messages.py
uv run pytest tests\test_agent_loop_batch.py tests\test_agent_loop_export.py tests\test_agent_loop_evals.py
uv run pytest tests\test_context_assembler.py tests\test_goal_runner.py
uv run pytest tests\test_harness_hooks.py tests\test_agent_loop_hooks.py tests\test_browser_hooks.py tests\test_builtin_hooks.py tests\test_command_hooks.py tests\test_hook_scripts.py
uv run pytest tests\test_harness_memory.py tests\test_memory_store.py tests\test_memory_tool.py tests\test_memory_consolidation.py tests\test_working_notes.py tests\test_browser_memory.py tests\test_memory_boundaries.py
uv run pytest tests\test_skills.py tests\test_skill_tool.py tests\test_session_skills.py tests\test_skill_boundaries.py
uv run pytest tests\serve tests\channels tests\test_serve_boundaries.py tests\test_cli_serve_channels_and_pool.py   # the stand, Telegram, the worker pool
uv run pytest tests\test_model_tiers.py tests\test_tier_escalation.py tests\test_session_cascade.py tests\test_cli_models.py   # the model cascade
uv run pytest tests\ui tests\test_ui_feed.py tests\test_ui_controller.py   # the terminal UI
uv run pytest -m live tests\pipeline\test_docker_smoke.py      # the Docker image (needs a Docker daemon)
uv run scripts/run_evals.py --memory-seed tests\evals\memory_seed   # evals with and without seed memory
```

Check docs-only diffs with:

```powershell
git diff --check -- docs
```

Run the CLI without browser/MCP tools for dry checks:

```powershell
uv run main.py --no-mcp --task "inspect page"
```

Run the CLI with Playwright MCP tools enabled:

```powershell
uv run main.py --task "open the target page"
```

Run Golden Set JSONL scenarios:

```powershell
uv run scripts/run_batch.py --tasks tests\golden\tasks.jsonl --no-mcp --continue-on-error
uv run scripts/compare_batches.py              # latest batch vs the previous one of the same set
```

Sync batch results to Langfuse (datasets, experiments, scores; `eval_platform`, off by default;
`docs/development/eval-platform.md`). Vendor code lives only in `scripts/langfuse_bridge/`;
`src/` holds neutral records and never names the platform:

```powershell
uv run scripts/langfuse_sync.py doctor                    # server, keys, project, score configs
uv run scripts/langfuse_sync.py setup                     # score configs (+ annotation queue)
uv run scripts/langfuse_sync.py dataset push              # Golden Set -> autobrowser/golden
uv run scripts/langfuse_sync.py experiment push --pending # catch up unfinished syncs
uv run scripts/langfuse_sync.py scores pull --batch <id>  # judge and reviewer scores -> the batch
```

Export session rows and inspect traces:

```powershell
uv run scripts/export_sessions.py --out runs.jsonl
uv run scripts/replay_trace.py <home>\sessions\<session_id>\events.jsonl
uv run scripts/export_agent_trace.py <home>\sessions\<session_id>\events.jsonl
```

Run deterministic fake-browser eval baselines (agent scenarios; replay scenarios; model cascade scenarios):

```powershell
uv run scripts/run_evals.py --baseline tests\evals\baselines\agent_loop_v2.json --memory-seed tests\evals\memory_seed
uv run scripts/run_evals.py --scenarios tests\evals\playback_scenarios --baseline tests\evals\baselines\playback_v1.json
uv run scripts/run_evals.py --scenarios tests\evals\cascade_scenarios --baseline tests\evals\baselines\cascade_v1.json
```

Start the interactive terminal UI with:

```powershell
uv run main.py            # terminal UI; plain mode without a capable terminal
uv run main.py --classic  # the cmd2 shell described below
```

The terminal UI takes free-form tasks and slash commands (`/help`, `/status`, `/model [id]`, `/tasks`,
`/cancel`, `/mcp [reconnect <name>]`, `/history [N]`, `/memory`, `/record [<name>|off]`,
`/skills`, `/url`, `/replay`, `/permissions`, `/detail`,
`/theme`, `/doctor`, `/resume`, `/clear` = new session (aliases `/reset`, `/new`), `/exit`); keys and terminals are listed in
`docs/development/terminal-ui.md`.

`--classic` (cmd2) commands include:

- `run <text>` or free-form input: run a new browser-agent task.
- `tasks`: show task history for the current session.
- `cancel`: cancel the currently running task.
- `session`: show current session information.
- `status`: show short session, browser, and task status.
- `history [N]`: show the last N dialogue messages.
- `clear`: clear dialogue history.
- `reset`: reset the current session by clearing history and state.
- `browser`: show browser/tool status.
- `url`: show the page URL the agent last saw (read from session state, no tool call).
- `help [command]`: show command help.
- `exit` or `quit`: exit the CLI.

Useful CLI flags include `--loop` (inert), `--show-state`, `--show-tools`, `--json`, `--no-mcp`, `--compress-tools`, `--model`, `--provider`, `--temperature`, `--chrome-path`, `--user-data-dir`, `--cdp-port`, `--cdp-timeout`, `--turn-cap`, `--permission-mode`, the front-end flags `--classic`,
`--plain`, `--ascii`, `--no-color`, `--theme`, session flags `--continue`/`-c` and
`--resume [id]`/`-r`, and print mode `-p` with `--output-format text|json|stream-json` and
`--verbose`, and replay flags `--record NAME` (record the first task) and, in print mode,
`--recording NAME` with `--answer agent|output|summary` (`docs/development/replay.md`).
Subcommands: `autobrowser doctor`, `sessions [--all]`, `replay <id|path>` (trace viewer),
`recordings list|show|rm`, `serve [--check]` (the stand, `docs/development/serve-mode.md`), `completion <shell>`. Runtime data lives in the AutoBrowser home (`AUTOBROWSER_HOME`, else
`~/.autobrowser`); see `docs/development/terminal-ui.md`.

## Coding Style & Naming Conventions

Use Python 3.12-compatible code. Follow PEP 8 with 4-space indentation, snake_case for functions and modules, PascalCase for classes, and UPPER_SNAKE_CASE for constants. Add type hints for public functions, loop state structures, and browser boundary contracts.

Every tunable value — a limit, budget, character cap, count, threshold, timeout, retry count, default model or path — is a field in `src/config.py` (a pydantic section, read through `get_settings()` or an injected `*Settings` object), documented in `.env.example` and `config.example.yaml` and covered by `tests/test_config.py`. Never add it as a module constant (`MAX_ENTRIES = 3`) or an inline literal (`text[:300]`) in a project file. UPPER_SNAKE_CASE constants are only for fixed vocabulary that is not a tuning knob: file and directory names, prefixes and markers, tool and server names, regular expressions, prompt text, schema keys.

Keep engine, state, and prompt code in the `src/agent_loop/execution/` and `src/agent_loop/prompts.py` patterns. Put runtime-facing Agent Loop contracts, durable event/trace helpers, replay/eval helpers, batch/export helpers, context assembly, skills, and goal lifecycle boundaries in `src/agent_loop/`. Put infrastructure abstractions in `src/harness/` instead of expanding engine modules. Put browser adaptation that reads text formats (page URL, `[ref=…]`, snapshot lines) — the permission resource resolver, memory scope and policy, browser hooks, playback, shared errors — in `src/browser/`; no code there decides by a tool name. Front ends go in `src/ui/`, the stand in `src/serve/`, vendor chat adapters in `src/channels/`. Prefer strict `LoopState` updates and typed contracts over ad hoc dictionaries when changing loop or browser boundaries.

## Testing Guidelines

Use `pytest` and `pytest-asyncio` for asynchronous engine, harness, and MCP behavior. Name test files `test_*.py` and test functions `test_*`.

Prefer focused unit tests for loop decisions, policy decisions, state transitions, tool registry behavior, tool-call normalization, observer normalization, Agent Loop event/action contracts, context assembly, goal lifecycle, metrics, replay, batch, and export behavior. Add integration tests for engine/harness wiring, harness injection, tool execution boundaries, MCP-backed execution, and scenario eval coverage. Use `tests/mcp_fixtures/fake_server.py` (a real MCP server) or the `_FakeBrowserTools` helper of `src/agent_loop/evals.py` when tests need tool/browser behavior without external services; the stand's tests use the fakes in `tests/serve/serve_fakes.py`. Do not require external services in default tests unless they are skipped or mocked.

## Browser Tool Rules

The MVP is moving away from deterministic, browser-specific rules towards a universal agent without hardcodes: the model decides how to use whatever tools the configured MCP servers expose. Do not add tool-, site- or page-structure-specific rules to the engine, guards, harness or memory policy. The prompts are server-neutral; browser rules live in the built-in `browser-agent-rules` skill, and each MCP server can explain its own tools through its `instructions` and the prompts its `mcp_servers` entry names (`src/harness/server_context.py`).

Tests, evals and the browser-rules skill use the exposed tool names (`browser_navigate`, `browser_click`, …), but no production code decides by a name: snapshots follow `readOnlyHint` per server and arguments follow the tool schema (`docs/decisions/2026-10-19-name-free-tool-handling.md`). Do not put Playwright MCP schema adaptation in executor or prompt code: add servers through `mcp_servers` and adapt calls with `ToolCallNormalizer`s.

## Commit & Pull Request Guidelines

History uses Conventional Commits in English with a scope from `.claude/conventions/commit.md` (`agent-loop`, `serve`, `channels`, `config`, `cli`, `harness`, `permissions`, `hooks`, `docs`, ...), e.g. `feat(serve): run jobs on a pool of workers`, and a detailed body. Use imperative subjects.

Pull requests should describe the behavioral change, list tests run, mention MCP/model-provider assumptions, and include screenshots or logs only when UI or browser-observation behavior changes.
