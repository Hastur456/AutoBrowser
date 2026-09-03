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
python -m pytest tests\test_harness_session.py
```

Run prompt tests after changing agent, planner, or observer prompts:

```powershell
python -m pytest tests\test_prompts.py
```

Run browser provider boundary tests after changing `src/browser/`, executor
normalization, or Playwright MCP integration:

```powershell
python -m pytest tests\test_browser_contracts.py tests\test_fake_browser_provider.py tests\test_playwright_mcp_provider.py
```

Run Agent Loop contract and observability tests after changing `src/agent_loop/`
or scripts that consume session traces:

```powershell
python -m pytest tests\test_agent_loop_events.py tests\test_agent_loop_replay.py tests\test_agent_loop_metrics.py tests\test_messages.py
python -m pytest tests\test_agent_loop_batch.py tests\test_agent_loop_export.py tests\test_agent_loop_evals.py
python -m pytest tests\test_context_assembler.py tests\test_goal_runner.py
```

Run harness and CLI boundary tests after changing session/runtime/tool wiring:

```powershell
python -m pytest tests\test_harness_session.py tests\test_harness_runtime.py tests\test_harness_tools.py tests\test_harness_telemetry.py
python -m pytest tests\test_main_cli.py
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
Durable loop state is scoped to the active interactive session so follow-up
tasks can use prior observations, snapshots, and browser progress.
Task-local planning, terminal, policy, error, and retry fields are reset before
each new task.

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

Set `AUTOBROWSER_CONTEXT_MODE=assembled` to route per-turn prompt construction
through `src/agent_loop/context.py`. The default is `legacy`; use
`AUTOBROWSER_CONTEXT_MODE=legacy` as the rollback path while the assembled
context path is still being validated.

## Batch, Export, Replay, And Evals

Run JSONL Golden Set scenarios through fresh `SessionRuntime` instances:

```powershell
python scripts/run_batch.py --tasks tests\golden\tasks.jsonl --no-mcp --continue-on-error
```

Export persisted session task rows:

```powershell
python scripts/export_sessions.py --out .autobrowser\exports\runs.jsonl
```

Inspect a durable event trace:

```powershell
python scripts/replay_trace.py .autobrowser\sessions\<session_id>\events.jsonl
python scripts/export_agent_trace.py .autobrowser\sessions\<session_id>\events.jsonl
```

Compare deterministic fake-browser eval scenarios with the baseline:

```powershell
python scripts/run_evals.py --baseline tests\evals\baselines\agent_loop_v1.json
```

## Development Guidelines

- Keep engine, state, and prompt changes in `src/agent_loop/` (`execution/`,
  `state.py`, `prompts.py`).
- Keep Agent Loop action contracts, model action parsing, event records,
  replay/eval helpers, batch/export helpers, context assembly, skills,
  and goal lifecycle boundaries in `src/agent_loop/`.
- Keep the provider-neutral chat contract (`ChatModel`/`ModelResponse`) in
  `src/llm.py`, the chat `Message`/`ToolCall` types in `src/messages.py`, and
  backend wire-format adapters in `src/providers/`.
- Keep browser backend contracts, canonical browser names, error codes, and
  backend adapters in `src/browser/`.
- Keep session and runtime infrastructure changes in `src/harness/`.
- Keep `SessionRuntime` focused on interaction lifecycle and resource
  ownership; it may carry loop context between tasks, but it should still
  delegate task solving to `AgentLoopEngine` instead of solving tasks in the
  session layer.
- Register browser-specific tools through `BrowserProvider` and `ToolRegistry`
  or harness injection.
- Keep Playwright MCP schema adaptation in browser providers, not in executor
  or agent prompt code.
- Use `FakeBrowserProvider` for deterministic browser behavior in tests that
  should not require Chrome, CDP, or MCP.
- Preserve Playwright MCP snapshot/ref semantics in prompts, policies,
  observer changes, and browser providers.
- Prefer focused tests for loop decisions, state transitions, prompt
  constraints, policy decisions, tool registry behavior, and observer
  normalization.

## Prompt Change Checklist

When changing prompts:

1. Update the relevant prompt file under `src/agent_loop/prompts.py`.
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
