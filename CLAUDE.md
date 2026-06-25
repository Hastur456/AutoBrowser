# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## CodeGraph First Policy

Tool budget:

- Maximum 2 codegraph_search calls
- Maximum 1 codegraph_files call
- Maximum 1 file read

After finding the file:
implement immediately.

Do not continue exploration.

ALWAYS use CodeGraph before reading files.

Workflow:
1. Query CodeGraph.
2. Identify exact target symbols/files.
3. Read only files that will be modified.
4. Never scan the repository with grep/glob/read when CodeGraph can answer the question.

Do not read files for architecture discovery.
Use CodeGraph for repository exploration.


## Plans Sync

After any change to project structure — adding/removing files or directories, renaming modules, changing graph nodes/edges, adding dependencies — update the relevant files in `plans/` to reflect the new state. Do not skip this step.

## Testing

Do NOT run tests autonomously. Always ask the user before executing any test commands (`pytest`, `python -m pytest`, etc.).

## Running the project

Requires a `.env` file with:
```
CHROME_PATH=<path to chrome executable>
USER_DATA_DIR=<path to chrome user data dir>
PORT=<remote debugging port, e.g. 9222>
```

Run the entry point:
```bash
python main.py
```

The `main.py` script launches Chrome with CDP remote debugging, waits for the port to open, then connects via MCP and invokes the planner.

The MCP server is a Node.js process started via `npx @playwright/mcp@latest`. Node >=18 is required.

## Architecture

The system is a LangGraph-based browser automation agent that separates planning from execution.

**MCP bridge** (`src/mcp/mcp_setup.py`): connects to a `@playwright/mcp` server over stdio using `langchain-mcp-adapters`. Returns LangChain-compatible tool objects. The playwright MCP server in turn connects to a Chrome instance via CDP (`--cdp-endpoint`).

**Planner subgraph** (`src/subgraphs/planner/`): a `StateGraph` over `PlannerState`. Two nodes:
- `task_decomposition_node` — calls the LLM with a system prompt to produce a free-text plan (`current_plan`)
- `get_list_of_tools_node` — calls the LLM with structured output (`PlanSteps`) to parse the plan into typed `PlanStep` objects with fields: `step_id`, `description`, `action_type`, `estimated_tool`, `is_sensitive`

**Executor subgraph** (`src/subgraphs/executor/`): a `StateGraph` over `ExecutorState`. Two nodes with retry logic:
- `mcp_invoke_node` — extracts `tool_calls` from the last message and invokes each tool; classifies errors as `fatal` (unknown tool) or `retryable` (TimeoutError, ConnectionError, RuntimeError)
- `backoff_node` — exponential backoff with jitter (capped at 10s) before retry
- `retry_router` — conditional edge: routes to `backoff` on retryable errors within `max_retries`, otherwise `abort` (END)

**Agent layer** (`src/agent/`): thin wrappers — `AgentState` extends `ExecutorState` with the same fields; `agent.py` imports `ExecutorState` and `ExecutorWorkflow` for composition.

**Allowed browser tools** (filtered in `main.py`):
`browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_fill_form`, `browser_wait_for`, `browser_tabs`, `browser_navigate_back`

## Key dependencies

- `langgraph` — state machine / graph orchestration
- `langchain-mcp-adapters` — wraps MCP servers as LangChain tools (`MultiServerMCPClient`)
- `langchain-ollama` (`ChatOllama`) — LLM backend; currently wired to `gpt-oss:20b-cloud`
- `@playwright/mcp` (npm) — MCP server that drives the browser via Playwright/CDP
