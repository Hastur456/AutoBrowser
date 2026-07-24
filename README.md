# AutoBrowser

AutoBrowser is a Python 3.12 browser automation agent built with LangGraph. It
turns a natural-language task into a controlled plan, policy check, tool
execution, and observation loop. Browser interaction is driven by Playwright MCP
snapshots and element refs instead of CSS selectors or DOM assumptions.

## What It Does

- Plans short browser tasks from natural-language instructions.
- Uses an Ollama-compatible chat model through LangChain.
- Loads Playwright MCP tools and connects them to Chrome over CDP.
- Executes tool calls through a harness-owned registry and policy layer.
- Observes tool output and browser snapshots before deciding the next action.
- Keeps a process-long session alive so multiple tasks can run without
  restarting the application.
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

Dry run without MCP browser tools:

```powershell
python main.py --no-mcp --task "inspect page"
```

After the startup task completes, the CLI remains in the session prompt for the
next task. Exit with `quit`, `exit`, Ctrl+C, or EOF.

Each session creates `.autobrowser/sessions/<session_id>/` with `session.json`,
`tasks.json`, and a `workspace/` tree for runtime artifacts. These files are
local runtime output and are ignored by git.

Run with browser tools enabled:

```powershell
python main.py --task "open the target page"
```

Start directly at the session prompt:

```powershell
python main.py
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

After changing prompts, run:

```powershell
python -m pytest tests\test_prompts.py
```

## Project Layout

| Path | Purpose |
| --- | --- |
| `main.py` | CLI parsing and wiring the process into the session runtime. |
| `src/agent/` | LangGraph graph assembly, state, prompts, reasoning node, and routers. |
| `src/agent/subgraphs/planner/` | Planning graph and planner prompt. |
| `src/agent/subgraphs/executor/` | Tool execution graph and Playwright MCP argument normalization. |
| `src/agent/subgraphs/observer/` | Tool-result observation, snapshot handling, and compact summaries. |
| `src/harness/` | Session runtime, graph harness, context, memory, tools, policy, and telemetry boundaries. |
| `src/mcp/` | MCP session and Playwright MCP integration helpers. |
| `tests/` | Pytest coverage for graph behavior, harness boundaries, CLI, prompts, and tools. |
| `scripts/` | Utility scripts, including graph visualization helpers. |
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
