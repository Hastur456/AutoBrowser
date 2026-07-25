# Repository Guidelines

<!-- CODEGRAPH_START -->
## CodeGraph

This repository is indexed by CodeGraph (`.codegraph/` exists at the repo root). Reach for CodeGraph before `rg`, `find`, or direct file reads when you need to understand or locate code.

- Prefer `codegraph explore "<symbol names or question>"` for architecture, flows, and symbol lookup.
- Prefer `codegraph node <symbol-or-file>` when you need one symbol or one file with line numbers.
- Use raw shell search only for non-code assets, docs, generated files, or details CodeGraph does not cover.
<!-- CODEGRAPH_END -->

## Project Structure & Module Organization

This is a Python 3.12 repository for an AutoBrowser/LangGraph agent. The CLI entry point is `main.py`. Core code lives in `src/`:

- `src/agent/`: LangGraph agent loop, state, prompts, routers, graph assembly, and history helpers.
- `src/agent/subgraphs/planner/`: planning graph pieces.
- `src/agent/subgraphs/executor/`: tool execution graph pieces.
- `src/agent/subgraphs/observer/`: tool-result and snapshot observation/compression pieces.
- `src/cli/`: `cmd2` interactive CLI, command catalog, output formatting, and task runner adapter.
- `src/harness/`: session runtime and runtime infrastructure injected into the graph.
- `src/mcp/`: MCP session and Playwright MCP integration helpers.
- `docs/`: architecture, development setup, decisions, diagrams, research notes, and glossary.

Tests live in `tests/`. Utility scripts live in `scripts/`, including graph visualization and trace export helpers. Runtime or local-only folders such as `.venv/`, `.pytest_cache/`, `node_modules/`, `.codegraph/`, `.playwright-mcp/`, `profile/`, `baseline/`, and `__pycache__/` should not be treated as source.

## Harness Architecture

LangGraph should own only the agent loop: planning, reasoning, routing, execution, and observation nodes. Infrastructure belongs in `src/harness/` and is injected into the compiled graph.

Harness responsibilities:

- `session.py`: owns the process-long session lifecycle through `SessionRuntime` and `SessionContext`.
- `runtime.py`: assembles harness components and compiles/runs/streams the graph through `BrowserHarness`.
- `context.py`: context and initial state construction, including system prompt injection.
- `memory.py`: checkpoint saver ownership and durable conversation history helpers.
- `tools.py`: pluggable tool registry for generic tool providers and MCP clients.
- `policy.py`: policy checks and policy engine boundary.
- `telemetry.py`: tracing/logging boundary.

Do not hardcode Playwright MCP behavior into the agent loop. Browser-specific MCP clients and toolsets should be registered through `ToolRegistry` or injected through `BrowserHarness` so tools can be swapped or mocked in CI. Keep planner, observer, executor, and core state contracts stable unless a migration step explicitly requires changing them.

## Session Context Architecture

AutoBrowser is a long-lived interactive session, not a single-shot task runner. `SessionRuntime` owns the process lifecycle and delegates session-owned state to `SessionContext`.

All tasks in one interactive session share a session-scoped LangGraph `thread_id` derived from `SessionContext.session_id`. Each user request still gets its own `TaskRecord.task_id` for task history and message attribution, but that ID is not the checkpoint thread.

After each task, `SessionRuntime` remembers the latest graph state in `SessionContext.state`. The next task carries forward only session-useful context:

- durable `messages`;
- latest `observation`;
- current `snapshot` and browser state;
- last browser action metadata needed for stale-snapshot and ineffective-action checks.

Before a new task starts, task-local fields must be reset so stale completion or retry state is not inherited:

- `plan`, `current_step`, `decision`, and `final_answer`;
- `tool_request`, `tool_result`, `policy_decision`, and `policy_event`;
- `error`, retry counters, replan counters, repeat counters, and ineffective-action counters.

`BrowserHarness` owns the internal state-override channel used to inject carried session state into the next graph invocation. Strip harness-internal config before passing config to LangGraph.

Preserve this boundary: the session layer manages lifecycle and context handoff, while the compiled graph still owns planning, reasoning, tool execution, observation, and task completion.

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
python -m pytest tests\test_agent_graph.py
python -m pytest tests\test_main_cli.py
python -m pytest tests\test_prompts.py
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

Use Python 3.12-compatible code. Follow PEP 8 with 4-space indentation, snake_case for functions and modules, PascalCase for classes, and UPPER_SNAKE_CASE for constants. Add type hints for public functions and graph state structures.

Keep graph node, router, state, and prompt code in the existing `nodes.py`, `routers.py`, `state.py`, and `prompts.py` pattern. Put infrastructure abstractions in `src/harness/` instead of expanding graph nodes. Prefer structured state updates and typed contracts over ad hoc dictionaries when changing graph boundaries.

## Testing Guidelines

Use `pytest` and `pytest-asyncio` for asynchronous graph, harness, and MCP behavior. Name test files `test_*.py` and test functions `test_*`.

Prefer focused unit tests for routers, policy decisions, state transitions, tool registry behavior, and observer normalization. Add integration tests for graph assembly, harness injection, streaming behavior, and tool execution boundaries. Do not require external services in default tests unless they are skipped or mocked.

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

Do not:

- guess CSS selectors
- generate XPath
- rely on class names
- assume DOM structure

If the snapshot does not expose the needed element:

1. Capture another snapshot.
2. Increase snapshot depth if appropriate.
3. Use `browser_evaluate` only if the snapshot cannot answer the question.

Reuse the current snapshot and refs when they are still valid. The policy layer blocks redundant `browser_snapshot` calls when a current snapshot is already available and no fresh snapshot is required.

The agent is snapshot-driven, not selector-driven.

## Commit & Pull Request Guidelines

Recent history uses short messages and Conventional Commit-style prefixes such as `feat(agent): ...` and `fix(execute): ...`. Use imperative, scoped commit subjects when possible.

Pull requests should describe the behavioral change, list tests run, mention MCP/Ollama assumptions, and include screenshots or logs only when UI or browser-observation behavior changes.
