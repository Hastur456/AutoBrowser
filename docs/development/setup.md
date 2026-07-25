# Development Setup

## Requirements

- Python 3.12
- Dependencies from `requirements.txt`
- Ollama-compatible chat model for normal CLI runs
- Chrome/Chromium and Playwright MCP tooling for browser-enabled runs

Runtime folders such as `.venv/`, `.pytest_cache/`, `.playwright-mcp/`,
`.autobrowser/`, `profile/`, `baseline/`, `node_modules/`, and `__pycache__/`
are local or generated state and should not be treated as source.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Test Commands

Run the full test suite:

```powershell
python -m pytest
```

Run a targeted file:

```powershell
python -m pytest tests\test_agent_graph.py
```

Run prompt tests after changing agent, planner, or observer prompts:

```powershell
python -m pytest tests\test_prompts.py
```

## CLI Usage

Start the interactive `cmd2` REPL:

```powershell
python main.py
```

The REPL exposes the base commands documented by `help`. Free-form input is
treated as a task, so both of these are valid:

```text
autobrowser> run open ozon.ru
autobrowser> open ozon.ru
```

Type `quit` or `exit`, or press Ctrl+C/EOF, to end the session.

Session metadata and task history are written under
`.autobrowser/sessions/<session_id>/` as `session.json` and `tasks.json`.
Task-specific LangGraph checkpoints are cleaned up after each task when the
configured saver supports thread deletion.

Run an initial task before entering the REPL:

```powershell
python main.py --task "open the target page"
```

Dry check without MCP browser tools:

```powershell
python main.py --no-mcp
```

`--no-mcp` keeps the REPL available but disables live browser access, including
navigation, snapshots, and current URL lookup.

Debug state updates:

```powershell
python main.py --show-state --task "inspect page"
```

Useful flags include `--show-state`, `--show-tools`, `--json`,
`--hide-snapshot`, `--compress-tools`, `--model`, `--temperature`,
`--chrome-path`, `--user-data-dir`, `--cdp-port`, `--cdp-timeout`, and
`--recursion-limit`. `--loop` remains accepted as a compatibility flag, but the
CLI now uses the long-lived session loop by default.

## Development Guidelines

- Keep reasoning and routing changes in `src/agent/`.
- Keep session and runtime infrastructure changes in `src/harness/`.
- Keep `SessionRuntime` focused on interaction lifecycle and resource
  ownership; delegate each task to `BrowserHarness` instead of solving tasks in
  the session layer.
- Register browser-specific tools through `ToolRegistry` or harness injection.
- Preserve Playwright MCP snapshot/ref semantics in prompts, policies, and
  executor changes.
- Prefer focused tests for routers, state transitions, prompt constraints,
  policy decisions, tool registry behavior, and observer normalization.

## Prompt Change Checklist

When changing prompts:

1. Update the relevant prompt file under `src/agent/` or a subgraph.
2. Add or adjust assertions in `tests/test_prompts.py`.
3. Run `python -m pytest tests\test_prompts.py`.
4. For behavior-sensitive browser changes, run at least one CLI trace with
   `--show-state` and inspect the tool sequence for loops.

## Browser-Agent Debugging

Use snapshots as the first debugging artifact. A healthy search flow should
normally look like:

1. `browser_navigate`
2. `browser_snapshot`
3. `browser_type` into an editable search control, or one search affordance
   click followed by a fresh snapshot and `browser_type`
4. result snapshot or direct result extraction
5. final answer

Repeated identical clicks, repeated identical snapshots, and `browser_find`
calls for implementation words such as `input` or `textbox` usually indicate a
prompt, policy, or observer-hint problem.

See [Browser Agent Rules](browser-agent-rules.md) for the detailed interaction
contract.
