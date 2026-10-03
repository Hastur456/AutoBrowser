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
engine-native execution loop. The CLI entry point is `main.py`. Core code lives in `src/`:

- `src/agent_loop/execution/`: the engine-native control loop — `AgentLoopEngine`,
  `TurnController`, the frozen `LoopState`, completion/observation/guards/policy
  helpers, `EngineResources`, and `native_task_runner`.
- `src/agent_loop/`: runtime-facing action contracts, model action parsing, eventing, replay/evals, metrics, batch/export helpers, context assembly, prompts, skills, and the `GoalRunner` lifecycle boundary around the engine.
- `src/contracts.py`: provider-neutral typed tool/plan/observation contracts and loop thresholds (no imports from the loop, harness, or browser layers).
- `src/state.py`: type-only `AgentState` TypedDict kept for browser-layer annotation.
- `src/messages.py`: dependency-free provider-neutral chat `Message`/`ToolCall` types shared by the engine and providers.
- `src/llm.py`: the provider-neutral `ChatModel`/`ModelResponse` chat contract. Model
  defaults live in `src/config.py` (`settings.llm`).
- `src/providers/`: provider adapters (e.g. `ollama.py`) that implement `ChatModel` by mapping neutral `Message`/`ToolDef` objects to a backend wire format.
- `src/browser/`: provider-neutral browser contracts, canonical browser names, backend adapters, shared browser errors, and fake browser tools for tests.
- `src/cli/`: `cmd2` interactive CLI, command catalog, output formatting, parser, and bootstrap wiring.
- `src/harness/`: session runtime and runtime infrastructure bundled into `EngineResources` for the engine.
- `src/mcp/`: Playwright MCP process/session lifecycle helpers and provider loading.
- `docs/`: architecture, development setup, decisions, diagrams, research notes, and glossary.

Tests live in `tests/`. Utility scripts live in `scripts/`, including batch runs, session exports, event-trace replay, agent-trace export, and eval baseline helpers. Runtime or local-only folders such as `.venv/`, `.pytest_cache/`, `node_modules/`, `.codegraph/`, `.playwright-mcp/`, `.autobrowser/`, `profile/`, `baseline/`, and `__pycache__/` should not be treated as source.

## Harness Architecture

The engine-native `AgentLoopEngine` owns the agent loop: planning, reasoning,
routing, execution, and observation. Infrastructure belongs in `src/harness/`
and is bundled into `EngineResources` for the engine by `BrowserHarness`.

Harness responsibilities:

- `session.py`: owns the process-long session lifecycle through `SessionRuntime` and `SessionContext`.
- `runtime.py`: composition root that holds the infrastructure collaborators `EngineResources.from_harness` reads; it no longer compiles/runs/streams a graph.
- `context.py` no longer exists in `src/harness/`: prompt construction lives in `ContextAssembler` (`src/agent_loop/context.py`), the sole boundary injected as `harness.context`.
- `memory.py`: functional conversation-history shaping over `Message` lists (no checkpoint saver; the durable history lives on `LoopState.messages`, not on a memory service).
- `tools.py`: pluggable tool registry for static tools, generic providers, browser providers, and MCP clients.
- `permissions.py`: session-scoped `PermissionEngine` — tool authorization rules, modes and approval grants.
- `telemetry.py`: local trace-metadata and error logging boundary.

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
`src/providers/` (for example `ollama.py`); tools are neutral `Tool`/`ToolDef` objects defined in
`src/contracts.py`; and conversation history is carried as provider-neutral `Message` lists
(`src/messages.py`, on `LoopState.messages`) with no checkpoint saver.

Do not hardcode Playwright MCP behavior into the agent loop. Tool servers are entries in `mcp_servers`, run by the universal MCP Manager, and reach `ToolRegistry` through `MCPToolSource`, so they can be swapped or mocked in CI. Keep the engine-native contracts and the frozen `LoopState` stable unless a change explicitly requires touching them.

## MCP and Browser Architecture

See `docs/decisions/2026-09-28-universal-mcp-manager.md`.

- `src/mcp/`: server-agnostic MCP Manager (`MCPManager`, `ServerRegistry`, stdio/streamable HTTP configs, catalog, typed errors, `server__tool` naming). Nothing here may special-case a server.
- `src/harness/mcp_setup.py`: builds the session `MCPRuntime` from settings (default: Playwright MCP over CDP), fills `{cdp_port}`/`{cdp_endpoint}`, fails startup when the browser server is not ready.
- `src/harness/mcp_tools.py`: `MCPToolSource`/`MCPTool` bridge; the browser server keeps unprefixed `browser_*` names.
- `src/harness/normalization.py`: `ToolCallNormalizer` protocol and `SchemaArgsNormalizer`.
- `src/browser/`: shared browser vocabulary only: `names.py` (canonical `browser.*` names), `errors.py`, `contracts.py`, and `normalization.py` (`BrowserToolNormalizer`). `provider.py`/`fake.py` remain as test scaffolding.

`ToolBroker` folds every call through the registered normalizers (request before, result after).

Use canonical `browser.*` names in provider-neutral tests when helpful. The Playwright adapter maps them to runtime MCP tool names.

## Session Context Architecture

AutoBrowser is a long-lived interactive session, not a single-shot task runner. `SessionRuntime` owns the process lifecycle and delegates session-owned state to `SessionContext`.

All tasks in one interactive session share a session identity derived from `SessionContext.session_id`, passed into each task config as `configurable.thread_id`. Each user request still gets its own `TaskRecord.task_id` for task history and message attribution, and `goal_id == task_id`.

After each task, `SessionRuntime` remembers the latest loop state in `SessionContext.state` (from the terminal `AgentLoopResult.session_state`). The next task carries forward only session-useful context:

- durable `messages`;
- latest `observation`;
- browser state;
- last browser action metadata needed for ineffective-action checks.

Before a new task starts, task-local fields must be reset so stale completion or retry state is not inherited:

- `plan`, `current_step`, `decision`, and `final_answer`;
- `tool_request`, `tool_result`, `policy_decision`, and `policy_event`;
- `error`, retry counters, replan counters, repeat counters, and ineffective-action counters.

`BrowserHarness` owns the internal state-override channel
(`HARNESS_STATE_OVERRIDES_CONFIG_KEY`) used to inject carried session state into
the next `AgentLoopEngine.run` call. Strip harness-internal config before the
engine sees the task config.

Preserve this boundary: the session layer manages lifecycle and context handoff,
while the engine-native loop still owns planning, reasoning, tool execution,
observation, and task completion.

## Build, Test, and Development Commands

Create and activate a virtual environment, then install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Run the test suite with:

```powershell
python -m pytest
```

Run targeted tests with `python -m pytest tests\path\to_test.py`.

Useful focused test commands:

```powershell
python -m pytest tests\test_harness_session.py tests\test_harness_runtime.py
python -m pytest tests\test_main_cli.py
python -m pytest tests\test_prompts.py
python -m pytest tests\test_browser_contracts.py tests\test_browser_normalization.py tests\test_mcp_manager.py tests\test_mcp_tools_bridge.py
python -m pytest tests\test_agent_loop_events.py tests\test_agent_loop_replay.py tests\test_agent_loop_metrics.py tests\test_messages.py
python -m pytest tests\test_agent_loop_batch.py tests\test_agent_loop_export.py tests\test_agent_loop_evals.py
python -m pytest tests\test_context_assembler.py tests\test_goal_runner.py
python -m pytest tests\test_harness_hooks.py tests\test_agent_loop_hooks.py tests\test_browser_hooks.py tests\test_builtin_hooks.py tests\test_command_hooks.py tests\test_hook_scripts.py
python -m pytest tests\test_harness_memory.py tests\test_memory_store.py tests\test_memory_tool.py tests\test_memory_consolidation.py tests\test_working_notes.py tests\test_browser_memory.py tests\test_memory_boundaries.py
python scripts/run_evals.py --memory-seed tests\evals\memory_seed   # evals with and without seed memory
```

Check docs-only diffs with:

```powershell
git diff --check -- docs
```

Run the CLI without browser/MCP tools for dry checks:

```powershell
python main.py --no-mcp --task "inspect page"
```

Run the CLI with Playwright MCP tools enabled:

```powershell
python main.py --task "open the target page"
```

Run Golden Set JSONL scenarios:

```powershell
python scripts/run_batch.py --tasks tests\golden\tasks.jsonl --no-mcp --continue-on-error
```

Export session rows and inspect traces:

```powershell
python scripts/export_sessions.py --out .autobrowser\exports\runs.jsonl
python scripts/replay_trace.py .autobrowser\sessions\<session_id>\events.jsonl
python scripts/export_agent_trace.py .autobrowser\sessions\<session_id>\events.jsonl
```

Run deterministic fake-browser eval baselines:

```powershell
python scripts/run_evals.py --baseline tests\evals\baselines\agent_loop_v1.json
```

Start the interactive REPL with:

```powershell
python main.py
```

REPL commands include:

- `run <text>` or free-form input: run a new browser-agent task.
- `tasks`: show task history for the current session.
- `cancel`: cancel the currently running task.
- `session`: show current session information.
- `status`: show short session, browser, and task status.
- `history [N]`: show the last N dialogue messages.
- `clear`: clear dialogue history.
- `reset`: reset the current session by clearing history and state.
- `browser`: show browser/tool status.
- `snapshot`: save a screenshot to `workspace/screenshots/`.
- `url`: show the current page URL.
- `help [command]`: show command help.
- `exit` or `quit`: exit the CLI.

Useful CLI flags include `--loop`, `--show-state`, `--hide-snapshot`, `--show-tools`, `--json`, `--no-mcp`, `--compress-tools`, `--model`, `--temperature`, `--chrome-path`, `--user-data-dir`, `--cdp-port`, `--cdp-timeout`, `--turn-cap`, and `--permission-mode`.

## Coding Style & Naming Conventions

Use Python 3.12-compatible code. Follow PEP 8 with 4-space indentation, snake_case for functions and modules, PascalCase for classes, and UPPER_SNAKE_CASE for constants. Add type hints for public functions, loop state structures, and browser boundary contracts.

Every tunable value — a limit, budget, character cap, count, threshold, timeout, retry count, default model or path — is a field in `src/config.py` (a pydantic section, read through `get_settings()` or an injected `*Settings` object), documented in `.env.example` and `config.example.yaml` and covered by `tests/test_config.py`. Never add it as a module constant (`MAX_ENTRIES = 3`) or an inline literal (`text[:300]`) in a project file. UPPER_SNAKE_CASE constants are only for fixed vocabulary that is not a tuning knob: file and directory names, prefixes and markers, tool and server names, regular expressions, prompt text, schema keys.

Keep engine, state, and prompt code in the `src/agent_loop/execution/` and `src/agent_loop/prompts.py` patterns. Put runtime-facing Agent Loop contracts, durable event/trace helpers, replay/eval helpers, batch/export helpers, context assembly, skills, and goal lifecycle boundaries in `src/agent_loop/`. Put infrastructure abstractions in `src/harness/` instead of expanding engine modules. Put browser tool-name helpers, shared errors, the request normalizer, the permission resource resolver, and browser hooks in `src/browser/`; tool names are the exposed MCP names (no canonical vocabulary). Prefer strict `LoopState` updates and typed contracts over ad hoc dictionaries when changing loop or browser boundaries.

## Testing Guidelines

Use `pytest` and `pytest-asyncio` for asynchronous engine, harness, and MCP behavior. Name test files `test_*.py` and test functions `test_*`.

Prefer focused unit tests for loop decisions, policy decisions, state transitions, tool registry behavior, browser provider normalization, observer normalization, Agent Loop event/action contracts, context assembly, goal lifecycle, metrics, replay, batch, and export behavior. Add integration tests for engine/harness wiring, harness injection, tool execution boundaries, provider-backed browser execution, and scenario eval coverage. Use `tests/mcp_fixtures/fake_server.py` or `FakeBrowserProvider` when tests need tool/browser behavior without external services. Do not require external services in default tests unless they are skipped or mocked.

## Browser Tool Rules

The MVP is moving away from deterministic, browser-specific rules towards a universal agent without hardcodes: the model decides how to use whatever tools the configured MCP servers expose. Do not add tool-, site- or page-structure-specific rules to the engine, guards, harness or memory policy. The prompts still carry older browser rules; they are revised separately — leave them unchanged unless asked.

Tests, evals and prompts use the exposed tool names (`browser_navigate`, `browser_click`, …); there is no canonical `browser.*` vocabulary and `BrowserToolNormalizer` never translates names. Do not put Playwright MCP schema adaptation in executor or prompt code: add servers through `mcp_servers` and adapt calls with `ToolCallNormalizer`s.

## Commit & Pull Request Guidelines

Recent history uses short messages and Conventional Commit-style prefixes such as `feat(agent): ...` and `fix(execute): ...`. Use imperative, scoped commit subjects when possible.

Pull requests should describe the behavioral change, list tests run, mention MCP/Ollama assumptions, and include screenshots or logs only when UI or browser-observation behavior changes.
