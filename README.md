# AutoBrowser

AutoBrowser is a Python 3.12 browser automation agent built with LangGraph. It
turns a natural-language task into a controlled plan, policy check, tool
execution, and observation loop. Browser interaction is driven by Playwright MCP
snapshots and element refs instead of CSS selectors or DOM assumptions.

## What It Does

- Plans short browser tasks from natural-language instructions.
- Uses an Ollama-compatible chat model through LangChain.
- Loads Playwright MCP tools and connects them to Chrome over CDP.
- Wraps browser tools behind a provider boundary so Playwright-specific schema
  adaptation stays out of the executor.
- Provides runtime-adjacent Agent Loop contracts for actions, events, tracing,
  replay, scenario evals, batch runs, exports, context assembly, and goal
  lifecycle boundaries.
- Executes tool calls through a harness-owned registry and policy layer.
- Observes tool output and browser snapshots before deciding the next action.
- Keeps a process-long session alive so multiple tasks can run without
  restarting the application.
- Preserves useful context between tasks in one session, including prior
  observations, current snapshots, browser state, and dialogue history.
- Writes runtime session records under `.autobrowser/sessions/<session_id>/`.
- Supports dry CLI runs without browser/MCP tools for development checks.

## Requirements

- Python 3.12
- Dependencies from `requirements.txt`
- Node.js 18+ for `npx @playwright/mcp`
- Chrome or Chromium for browser-enabled runs
- An Ollama-compatible chat model for normal CLI execution

The default model is `gpt-oss:20b-cloud`. Override it with `--model`.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

For browser-enabled runs, make sure `npx` can start Playwright MCP. The CLI uses
`npx -y @playwright/mcp@latest` internally.

## Configuration

The CLI reads `.env` automatically. Common settings are:

```env
CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe
USER_DATA_DIR=C:\temp\chrome_debug_profile
PORT=9222
```

Each of these can also be overridden from the command line with `--chrome-path`,
`--user-data-dir`, and `--cdp-port`.

## Run

Start the interactive `cmd2` REPL:

```powershell
python main.py
```

The prompt accepts either explicit commands or free-form tasks:

```text
autobrowser> help
autobrowser> run open the target page
autobrowser> open ozon.ru and find autumn jackets
autobrowser> status
autobrowser> exit
```

Useful REPL commands:

| Command | Purpose |
| --- | --- |
| `run <text>` | Run a new browser-agent task. |
| free-form input | Run the input as a task without typing `run`. |
| `tasks` | Show task history for the current session. |
| `cancel` | Cancel the currently running task. |
| `session` | Show current session information. |
| `status` | Show short session, browser, and task status. |
| `history [N]` | Show the last N dialogue messages. |
| `clear` | Clear dialogue history. |
| `reset` | Reset the current session by clearing history and state. |
| `browser` | Show browser/tool status. |
| `snapshot` | Save a screenshot to the session workspace. |
| `url` | Show the current page URL. |
| `help [command]` | Show command help. |
| `exit` / `quit` | Exit the CLI. |

Use `--no-mcp` only for dry checks that do not need live browser access:

```powershell
python main.py --no-mcp
```

With `--no-mcp`, commands still work, but browser navigation, snapshots, and
live website extraction are unavailable.

Each session creates `.autobrowser/sessions/<session_id>/` with `session.json`,
`tasks.json`, and a `workspace/` tree for runtime artifacts. These files are
local runtime output and are ignored by git.

Within one interactive session, tasks share a session-scoped LangGraph thread.
The runtime carries forward useful graph state such as durable messages, latest
observation, current snapshot, and browser progress. Task-local fields such as
the prior plan, terminal decision, final answer, errors, and retry counters are
reset before the next task starts.

Run an initial task before entering the REPL:

```powershell
python main.py --task "open the target page"
```

Show graph state updates while debugging:

```powershell
python main.py --show-state --hide-snapshot --task "inspect page"
```

Useful flags include `--show-state`, `--show-tools`, `--json`,
`--hide-snapshot`, `--compress-tools`, `--model`, `--temperature`,
`--chrome-path`, `--user-data-dir`, `--cdp-port`, `--cdp-timeout`, and
`--recursion-limit`. `--loop` is still accepted for compatibility.

## Test

Run the full test suite:

```powershell
python -m pytest
```

Run a targeted test file:

```powershell
python -m pytest tests\test_agent_graph.py
```

Focused checks for recent harness/session work:

```powershell
python -m pytest tests\test_harness_session.py tests\test_harness_runtime.py
python -m pytest tests\test_main_cli.py
```

Focused checks for Agent Loop runtime contracts and observability:

```powershell
python -m pytest tests\test_agent_loop_events.py tests\test_agent_loop_tracing.py tests\test_agent_loop_replay.py tests\test_agent_loop_metrics.py
python -m pytest tests\test_agent_loop_batch.py tests\test_agent_loop_export.py tests\test_agent_loop_evals.py
python -m pytest tests\test_context_assembler.py tests\test_goal_runner.py
```

Focused checks for browser provider work:

```powershell
python -m pytest tests\test_browser_contracts.py tests\test_fake_browser_provider.py tests\test_playwright_mcp_provider.py
```

After changing prompts, run:

```powershell
python -m pytest tests\test_prompts.py
```

## Project Layout

| Path | Purpose |
| --- | --- |
| `main.py` | CLI parsing and wiring the process into the session runtime. |
| `src/agent/` | LangGraph graph assembly, state, prompts, reasoning node, and routers. |
| `src/agent_loop/` | Runtime-facing action contracts, eventing, tracing, replay/evals, batch/export helpers, context assembly, and goal lifecycle boundaries around the current graph engine. |
| `src/agent/subgraphs/planner/` | Planning graph and planner prompt. |
| `src/agent/subgraphs/executor/` | Tool execution graph and provider-backed request/result normalization. |
| `src/agent/subgraphs/observer/` | Tool-result observation, snapshot handling, and compact summaries. |
| `src/browser/` | Browser provider contracts, canonical names, Playwright MCP adapter, and fake browser backend. |
| `src/harness/` | Session runtime, graph harness, context, memory, tools, policy, and telemetry boundaries. |
| `src/mcp/` | Playwright MCP process/session lifecycle helpers and provider loading. |
| `tests/` | Pytest coverage for graph behavior, harness boundaries, Agent Loop contracts, CLI, prompts, tools, batch/export, and deterministic eval scenarios. |
| `scripts/` | Utility scripts for graph visualization, batch runs, session exports, trace replay/export, LangSmith trace export, and eval baseline checks. |
| `docs/` | Architecture, setup, diagrams, decisions, research notes, and glossary. |

## Architecture

The compiled graph is assembled in `src/agent/agent.py`:

```text
START -> plan -> agent -> policy -> executor -> observe -> agent
```

The `agent` node can route back to `plan` for replanning or finish when a final
answer is available. The runtime infrastructure is injected through
`SessionRuntime` and `BrowserHarness` in `src/harness/`, keeping browser
tooling, memory, policy, telemetry, context, and interaction lifecycle outside
the core agent loop.

Browser-specific request and result adaptation lives under `src/browser/`.
Production runs use `PlaywrightMCPBrowserProvider`; tests can use
`FakeBrowserProvider` to exercise browser behavior without external services.

`SessionRuntime` owns the long-lived process lifecycle through `SessionContext`.
All tasks in one interactive session share a session-scoped checkpoint thread;
each task still receives a `TaskRecord.task_id` for persisted history and
message attribution. `SessionRuntime` carries session-useful graph state into
the next invocation and resets task-local graph fields at the task boundary.

## Browser Interaction Rules

AutoBrowser follows Playwright MCP semantics:

- `browser_snapshot` is the source of truth.
- Element identity comes from snapshot refs such as `ref=e123`.
- Preferred interactions are `browser_click(ref)`, `browser_type(ref)`, and
  `browser_hover(ref)`.
- Do not rely on CSS selectors, XPath, class names, or assumed DOM structure.

See [docs/development/browser-agent-rules.md](docs/development/browser-agent-rules.md)
for the full interaction contract.

## Documentation

Start with [docs/README.md](docs/README.md). The most useful sections are:

- [Architecture Overview](docs/architecture/overview.md)
- [Development Setup](docs/development/setup.md)
- [Diagrams](docs/diagrams/index.md)
- [Architecture Decisions](docs/decisions/index.md)
- [Glossary](docs/glossary.md)
