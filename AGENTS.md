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
- `src/state.py`: type-only `AgentState`/`BrowserState` TypedDicts kept for annotation.
- `src/messages.py`: dependency-free provider-neutral chat `Message`/`ToolCall` types shared by the engine and providers.
- `src/llm.py`: model defaults (`DEFAULT_OLLAMA_MODEL`) and the provider-neutral `ChatModel`/`ModelResponse` chat contract.
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
- `context.py`: context and initial state construction, including system prompt injection.
- `memory.py`: functional conversation-history shaping over `Message` lists (no checkpoint saver; the durable history lives on `LoopState.messages`, not on a memory service).
- `tools.py`: pluggable tool registry for static tools, generic providers, browser providers, and MCP clients.
- `policy.py`: policy checks and policy engine boundary.
- `telemetry.py`: local trace-metadata and error logging boundary.

`ContextBuilder` defaults to legacy prompt rendering. Set `AUTOBROWSER_CONTEXT_MODE=assembled` to use the assembled context path backed by `src/agent_loop/context.py`; set `AUTOBROWSER_CONTEXT_MODE=legacy` for rollback while validating prompt changes.

The LangGraph migration is complete: `src/agent/`, `src/agent_loop/adapters/langgraph.py`,
`src/cli/task_runner.py`, and the legacy `LegacyAgentStateObservationCompiler` are removed, and
`AgentLoopEngine` is the sole runtime (see
[docs/decisions/2026-08-31-native-agent-loop-engine.md](docs/decisions/2026-08-31-native-agent-loop-engine.md)).
`AUTOBROWSER_AGENT_LOOP`/`SessionConfig.agent_loop` are inert compatibility surface.

The LangChain/LangGraph/LangSmith dependency stack is fully removed: model access goes through the
provider-neutral `ChatModel` contract in `src/llm.py`, implemented by thin provider adapters in
`src/providers/` (for example `ollama.py`); tools are neutral `Tool`/`ToolDef` objects defined in
`src/contracts.py`; and conversation history is carried as provider-neutral `Message` lists
(`src/messages.py`, on `LoopState.messages`) with no checkpoint saver and no LangSmith tracing.

Do not hardcode Playwright MCP behavior into the agent loop. Browser-specific backends should be registered through `BrowserProvider` and `ToolRegistry` or injected through `BrowserHarness` so tools can be swapped or mocked in CI. Keep the engine-native contracts and the frozen `LoopState` stable unless a change explicitly requires touching them.

## Browser Provider Architecture

`src/browser/` is the provider-neutral browser boundary. Production browser access is still Playwright MCP, but Playwright-specific schema adaptation belongs in browser providers, not in the executor, policy, or agent prompts.

Browser boundary responsibilities:

- `provider.py`: `BrowserProvider` protocol for backends that expose tools and normalize tool requests/results.
- `contracts.py`: provider-neutral `BrowserAction` and `BrowserResult` typed contracts.
- `names.py`: canonical browser names such as `browser.snapshot` and mappings to Playwright MCP names such as `browser_snapshot`.
- `errors.py`: shared browser error codes such as `invalid_ref`, `unknown_action`, and `action_failed`.
- `adapters/playwright_mcp.py`: `PlaywrightMCPBrowserProvider`, the production adapter around loaded Playwright MCP tools.
- `fake.py`: `FakeBrowserProvider` for deterministic tests without Chrome, CDP, or MCP.

The executor should resolve tools through `ToolRegistry`, pass browser requests through registered provider normalizers before invocation, and pass raw results through provider result normalizers before returning loop state. Raw non-provider tools should remain unadapted.

Use canonical `browser.*` names in provider-neutral tests when helpful. The Playwright adapter maps them to runtime MCP tool names.

## Session Context Architecture

AutoBrowser is a long-lived interactive session, not a single-shot task runner. `SessionRuntime` owns the process lifecycle and delegates session-owned state to `SessionContext`.

All tasks in one interactive session share a session identity derived from `SessionContext.session_id`, passed into each task config as `configurable.thread_id`. Each user request still gets its own `TaskRecord.task_id` for task history and message attribution, and `goal_id == task_id`.

After each task, `SessionRuntime` remembers the latest loop state in `SessionContext.state` (from the terminal `AgentLoopResult.session_state`). The next task carries forward only session-useful context:

- durable `messages`;
- latest `observation`;
- current `snapshot` and browser state;
- last browser action metadata needed for stale-snapshot and ineffective-action checks.

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
python -m pytest tests\test_browser_contracts.py tests\test_fake_browser_provider.py tests\test_playwright_mcp_provider.py
python -m pytest tests\test_agent_loop_events.py tests\test_agent_loop_replay.py tests\test_agent_loop_metrics.py tests\test_messages.py
python -m pytest tests\test_agent_loop_batch.py tests\test_agent_loop_export.py tests\test_agent_loop_evals.py
python -m pytest tests\test_context_assembler.py tests\test_goal_runner.py
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
python scripts/run_evals.py --baseline tests\evals\baselines\langgraph_v1.json
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

Useful CLI flags include `--loop`, `--show-state`, `--hide-snapshot`, `--show-tools`, `--json`, `--no-mcp`, `--compress-tools`, `--model`, `--temperature`, `--chrome-path`, `--user-data-dir`, `--cdp-port`, `--cdp-timeout`, and `--recursion-limit`.

## Coding Style & Naming Conventions

Use Python 3.12-compatible code. Follow PEP 8 with 4-space indentation, snake_case for functions and modules, PascalCase for classes, and UPPER_SNAKE_CASE for constants. Add type hints for public functions, loop state structures, and browser boundary contracts.

Keep engine, state, and prompt code in the `src/agent_loop/execution/` and `src/agent_loop/prompts.py` patterns. Put runtime-facing Agent Loop contracts, durable event/trace helpers, replay/eval helpers, batch/export helpers, context assembly, skills, and goal lifecycle boundaries in `src/agent_loop/`. Put infrastructure abstractions in `src/harness/` instead of expanding engine modules. Put browser backend contracts, canonical names, shared errors, and backend adapters in `src/browser/`. Prefer strict `LoopState` updates and typed contracts over ad hoc dictionaries when changing loop or browser boundaries.

## Testing Guidelines

Use `pytest` and `pytest-asyncio` for asynchronous engine, harness, and MCP behavior. Name test files `test_*.py` and test functions `test_*`.

Prefer focused unit tests for loop decisions, policy decisions, state transitions, tool registry behavior, browser provider normalization, observer normalization, Agent Loop event/action contracts, context assembly, goal lifecycle, metrics, replay, batch, and export behavior. Add integration tests for engine/harness wiring, harness injection, tool execution boundaries, provider-backed browser execution, and scenario eval coverage. Use `FakeBrowserProvider` when tests need browser behavior without external services. Do not require external services in default tests unless they are skipped or mocked.

## Playwright MCP Development Rules

This project is not a traditional Playwright project. The browser agent must follow Playwright MCP semantics.

Source of truth:

- `browser_snapshot`

Element identity:

- `ref=e123`

Preferred interaction:

- `browser_click(ref)`
- `browser_type(ref)`
- `browser_hover(ref)`

Provider-neutral tests may use canonical names such as `browser.snapshot`, `browser.click`, `browser.type`, and `browser.hover`; production execution maps them to Playwright MCP names through `PlaywrightMCPBrowserProvider`.

Ref freshness:

- Ref-based actions require a current `browser_snapshot`.
- If there is no current snapshot, request `browser_snapshot` before clicking, typing, or hovering.
- If the requested ref is not present in the latest snapshot, replan from visible refs instead of reusing refs from history or a prior page.

Do not:

- guess CSS selectors
- generate XPath
- rely on class names
- assume DOM structure
- put Playwright MCP schema adaptation in executor or prompt code

If the snapshot does not expose the needed element:

1. Capture another snapshot.
2. Increase snapshot depth if appropriate.
3. Use `browser_evaluate` only if the snapshot cannot answer the question.

Reuse the current snapshot and refs when they are still valid. The policy layer blocks redundant `browser_snapshot` calls when a current snapshot is already available and no fresh snapshot is required.

The agent is snapshot-driven, not selector-driven.

## Commit & Pull Request Guidelines

Recent history uses short messages and Conventional Commit-style prefixes such as `feat(agent): ...` and `fix(execute): ...`. Use imperative, scoped commit subjects when possible.

Pull requests should describe the behavioral change, list tests run, mention MCP/Ollama assumptions, and include screenshots or logs only when UI or browser-observation behavior changes.
