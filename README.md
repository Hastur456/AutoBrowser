# AutoBrowser

AutoBrowser is a Python 3.12 browser automation agent with an engine-native
execution loop. It turns a natural-language task into a controlled plan, permission
check, tool execution, and observation loop. Browser interaction goes through
the tools of the configured MCP servers (Playwright MCP by default).

It runs in two modes over one core: an interactive session in the terminal (a
Claude-Code-style inline UI) and the stand, `autobrowser serve`, which runs configured jobs on
triggers without a terminal.

## What It Does

- Plans short browser tasks from natural-language instructions.
- Uses Ollama or any OpenAI-compatible chat service (OpenAI, aggregators, LM Studio, vLLM)
  through provider-neutral adapters (`llm.provider`).
- Works with the tools of any configured MCP server (Playwright MCP over CDP by default, or a
  server that launches its own headless browser); no logic is keyed on tool names.
- Executes tool calls through a harness-owned registry, lifecycle hooks and a permission
  engine (`allow | ask | deny`), and observes the output before the next action.
- Keeps a process-long session alive so multiple tasks run without restarting, and carries
  the useful context (dialogue, page state) from one task to the next.
- Records a completed run and replays it without the model; budgets cap calls, tokens and
  money per task; a model cascade can let a cheap model work and a stronger one step in.
- Runs configured jobs on a stand (`autobrowser serve`): cron, interval, webhooks and
  Telegram, a model-free probe, day budgets, an HTTP API, a pool of workers, Docker.
- Writes runtime session records under the AutoBrowser home
  (`<home>/sessions/<session_id>/`, `~/.autobrowser` by default).
- Supports dry runs without browser/MCP tools (`--no-mcp`) for development checks.

## Requirements

- [uv](https://docs.astral.sh/uv/) (it provides Python 3.12 and the dependencies from
  `pyproject.toml` / `uv.lock`)
- Node.js 18+ for `npx @playwright/mcp`
- Chrome or Chromium for browser-enabled runs
- A chat model for normal CLI execution: Ollama, or any OpenAI-compatible service

The default model is `gemma4:31b-cloud`. Override it with `--model`.

## Install

```powershell
uv sync
```

`uv sync` creates `.venv` from `uv.lock` (Python 3.12 is fetched if missing) and keeps it
exactly in line with the lock, removing packages that are not declared. Run everything
through `uv run`, no activation needed. `requirements.txt` is exported from the lock
(`uv export --format requirements.txt --no-hashes --no-annotate --no-header --no-emit-project
-o requirements.txt`) for a plain pip fallback.

For browser-enabled runs, make sure `npx` can start Playwright MCP. Without `mcp_servers`
the session uses `npx -y @playwright/mcp@latest` attached to its Chrome over CDP.
`uv run main.py doctor` checks the paths, the configuration, Node.js, Chrome and the
terminal (`doctor --llm` also the model provider).

## Configuration

All settings live in `src/config.py`. Names are `AUTOBROWSER_<SECTION>__<FIELD>` over
eighteen sections; the environment outranks `.env`, which is read from the repository root
(legacy) and then from `<home>/.env`.

`.env.example` documents every setting with its default — copy it to `.env` and uncomment
what you want to change. Common settings are:

```env
AUTOBROWSER_BROWSER__CHROME_PATH=C:/Program Files/Google/Chrome/Application/chrome.exe
AUTOBROWSER_BROWSER__USER_DATA_DIR=C:/temp/chrome_debug_profile
AUTOBROWSER_BROWSER__CDP_PORT=9222
AUTOBROWSER_LLM__MODEL=gemma4:31b-cloud
```

A YAML file can layer *over* the environment for values that are awkward to spell as
env vars — it works as a partial profile, so it only has to name what it changes.

The configuration file is resolved as follows:

1. If `AUTOBROWSER_CONFIG_FILE` is set, that path is used. If the file does not
   exist, startup fails.
2. Otherwise `<home>/config.yaml` is used when it exists, else `config.yaml` at the
   repository root (the legacy checkout profile).
3. If neither file exists, configuration continues using environment variables,
   `.env`, secret files, and Pydantic defaults.

The default files are therefore optional. An explicitly configured file is mandatory and
must exist.

To use a specific configuration file:

```powershell
$env:AUTOBROWSER_CONFIG_FILE="config.local.yaml"; uv run main.py
```

That name has to be a real environment variable — a line in `.env` will not work, because the path is resolved before settings are built. Precedence, highest first: `Settings(...)` kwargs → the resolved YAML file, if present → `AUTOBROWSER_*` environment variables → `.env` → secret files → Pydantic defaults. The file is a partial profile: it only has to name the fields it should decide, and each of those outranks every source below, merging per field — naming `llm.model` there settles that field and leaves the rest of the
`llm` block to the environment, `.env` and the defaults. Note that this also overrides the
`.env` in this repository, which pins `AUTOBROWSER_LLM__MODEL` and
`AUTOBROWSER_BROWSER__CDP_PORT`. `config.example.yaml` shows the shape and lists the accepted
values; a copy may hold `llm.api_key`, so the conventional names — `config.yaml`,
`config.local.yaml`, `secrets.yaml` — are git-ignored.

Write Windows paths with forward slashes: `python-dotenv` decodes escape sequences inside
double-quoted values, so `"C:\temp"` silently becomes a tab character.

`AUTOBROWSER_LLM__API_KEY` is passed to the provider as an `Authorization: Bearer` header;
leave it unset for a local Ollama daemon. `AUTOBROWSER_LLM__HOST` is likewise optional for
Ollama and required for `AUTOBROWSER_LLM__PROVIDER=openai_compat`; `config.example.yaml`
has commented profiles for common services, and `autobrowser doctor --llm` checks the
connection.

Each of these can also be overridden from the command line with `--chrome-path`,
`--user-data-dir`, and `--cdp-port`.

## Run

Start the interactive session (the inline terminal UI; plain line mode without a capable
terminal):

```powershell
uv run main.py                          # or `autobrowser` after `uv tool install --editable .`
uv run main.py --task "open the target page"
uv run main.py --continue               # resume the latest session with its dialogue
uv run main.py -p "task" --output-format json   # print mode: the result only, exit 0/1/2/3/130
uv run main.py --plain                  # line mode
uv run main.py --classic                # the old cmd2 shell
```

Type a task, or a slash command: `/help`, `/status`, `/model [id]`, `/tasks`, `/cancel`,
`/mcp [reconnect <name>]`, `/history [N]`, `/memory`, `/record [<name>|off]`, `/skills`,
`/url`, `/replay`, `/resume`, `/permissions`, `/detail`, `/theme`, `/doctor`, `/clear`,
`/exit`. Keys, terminals and print mode are in the
[terminal UI guide](docs/development/terminal-ui.md).

Subcommands: `doctor`, `sessions`, `replay`, `recordings`, `serve`, `completion`.

Use `--no-mcp` only for dry checks that do not need live browser access:

```powershell
uv run main.py --no-mcp
```

With `--no-mcp`, commands still work, but browser navigation and live website
extraction are unavailable.

Each session creates `<home>/sessions/<session_id>/` with `session.json`, `tasks.json`,
`events.jsonl`, `state.json` and a `workspace/` tree for runtime artifacts. These files are
local runtime output, outside the repository.

Within one interactive session, tasks share a session identity derived from
`SessionContext.session_id` and carried as `TaskRequest.session_id`. The runtime
carries forward only durable context such as messages, latest observation,
and browser progress. Task-local fields such as the prior
plan, terminal decision, final answer, errors, and retry counters are reset
before the next task starts. There is no compiled graph and no checkpoint
thread.

Run an initial task before entering the REPL:

```powershell
uv run main.py --task "open the target page"
```

Show per-step state updates while debugging:

```powershell
uv run main.py --show-state --task "inspect page"
```

Useful flags include `--show-state`, `--show-tools`, `--json`,
`--compress-tools`, `--model`, `--provider`, `--temperature`,
`--chrome-path`, `--user-data-dir`, `--cdp-port`, `--cdp-timeout`, `--turn-cap`,
`--permission-mode`, `--record <name>` and `--recording <name>`. `--loop` is still accepted
for compatibility.

`--permission-mode default|read_only|dont_ask|bypass` overrides `permissions.mode` for the
session. In a terminal the UI asks before a call the permission rules flag (once, for the
session on that tool and domain, or deny; a refusal ends the task `blocked`); without a
terminal (print mode, `run_batch.py`) a configured `default` mode becomes `dont_ask`, so
such calls are denied and the model continues. See the
[permissions guide](docs/development/permissions.md).

## The Stand

```powershell
$env:AUTOBROWSER_SERVE__TOKEN = "<a long random string>"
uv run main.py serve --check      # validate the configuration, start no browser
uv run main.py serve              # run the configured jobs until Ctrl+C (SIGTERM in Docker)
```

Jobs, triggers, the probe, budgets, the HTTP API, the worker pool, the model cascade, the
Telegram channel and the Docker image are in the [serve mode guide](docs/development/serve-mode.md).

## Test

Run the full test suite:

```powershell
uv run pytest
```

Run a targeted test file:

```powershell
uv run pytest tests\test_harness_session.py
```

Focused checks for recent harness/session work:

```powershell
uv run pytest tests\test_harness_session.py tests\test_harness_runtime.py tests\test_harness_tools.py tests\test_harness_telemetry.py
uv run pytest tests\test_main_cli.py
```

Focused checks for Agent Loop runtime contracts and observability:

```powershell
uv run pytest tests\test_agent_loop_events.py tests\test_agent_loop_replay.py tests\test_agent_loop_metrics.py tests\test_messages.py
uv run pytest tests\test_agent_loop_batch.py tests\test_agent_loop_export.py tests\test_agent_loop_evals.py
uv run pytest tests\test_context_assembler.py tests\test_goal_runner.py
```

Focused checks for MCP and tool-call adaptation:

```powershell
uv run pytest tests\test_browser_contracts.py tests\test_schema_args_normalizer.py tests\test_mcp_manager.py tests\test_mcp_tools_bridge.py
```

The terminal UI, the stand and the Telegram channel:

```powershell
uv run pytest tests\ui tests\test_ui_feed.py
uv run pytest tests\serve tests\channels tests\test_serve_boundaries.py
```

Scripted evals against their baselines (also run by the CI gate for `agent_loop_v2`):

```powershell
uv run scripts\run_evals.py --baseline tests\evals\baselines\agent_loop_v2.json --memory-seed tests\evals\memory_seed
uv run scripts\run_evals.py --scenarios tests\evals\playback_scenarios --baseline tests\evals\baselines\playback_v1.json
uv run scripts\run_evals.py --scenarios tests\evals\cascade_scenarios --baseline tests\evals\baselines\cascade_v1.json
```

After changing prompts, run:

```powershell
uv run pytest tests\test_prompts.py
```

## Project Layout

| Path | Purpose |
| --- | --- |
| `main.py` | Calls `src.cli.entry.main`, like the `autobrowser` console script. |
| `src/cli/` | The composition root: entry point and subcommands, parser, bootstrap of the session and the front end, the model cascade's tiers, the stand, and the classic `cmd2` shell. |
| `src/ui/` | Front ends over the session: `SessionController`, the inline terminal UI, plain and print mode. |
| `src/serve/` | The stand (`autobrowser serve`): dispatcher over a worker pool, scheduler, HTTP API, probe, ledger, notification channels. |
| `src/channels/` | Chat channel adapters of the stand (Telegram). |
| `src/contracts.py` | Provider-neutral typed tool/plan/observation contracts and loop thresholds (imports nothing from the loop, harness, or browser layers). |
| `src/messages.py` | Dependency-free provider-neutral chat `Message`/`ToolCall` types shared by the engine and providers. |
| `src/llm.py` | The provider-neutral `ChatModel`/`ModelResponse` chat contract and the model tiers (`ModelTier`, `ModelTiers`, `ModelPrice`). |
| `src/config.py` | Every tunable, as typed settings read from the environment, `.env` and an optional YAML profile. |
| `src/providers/` | Provider adapters (`ollama.py`, `openai_compat.py`) that map neutral `Message`/`ToolDef` objects to a wire protocol, and `registry.py`, which picks one by `llm.provider`. |
| `src/agent_loop/` | Runtime-facing action contracts and model parsing, events, trace replay/evals, metrics, batch/export helpers, context assembly, prompts, skills, and the `GoalRunner` lifecycle boundary around the engine-native `AgentLoopEngine`. |
| `src/browser/` | Browser adapters over text formats (never tool names): page URL, permission resources, memory scope and policy, hooks, playback, error codes. |
| `src/harness/` | Session runtime, harness composition root, MCP setup, tools, permissions, hooks, memory, skills, recordings and playback, approvals, telemetry. |
| `src/mcp/` | The universal MCP manager: server registry, connections, reconnects, discovery, shutdown. |
| `tests/` | Pytest coverage for engine-native behavior, harness boundaries, Agent Loop contracts, CLI, prompts, tools, batch/export, and deterministic eval scenarios. |
| `scripts/` | Utility scripts for batch runs and their comparison, session exports, trace replay, scripted evals and the Langfuse sync. |
| `scripts/hooks/` | Ready command hooks (stdlib only), registered from `config.example.yaml`. |
| `Dockerfile`, `compose.yaml`, `docker/` | The stand's Docker image and its example profile. |
| `docs/` | Architecture, setup, diagrams, decisions, research notes, and glossary. |

## Architecture

Control flow is engine-native. `AgentLoopEngine` in
`src/agent_loop/execution/loop.py` builds an initial plan and then drives a
bounded `while` loop of `TurnController` turns:

```text
plan (model call #0, or a recorded plan) -> while turn <= cap:
  tier escalation check (model cascade only)
  TurnController.run_turn(LoopState) ->
    step source (replay) or agent step -> decision
      done      -> terminal status via CompletionController
      replan    -> rebuild plan
      tool_call -> progress guard -> pre_tool_use hooks -> PermissionEngine
                -> ToolBroker.invoke -> post_tool_use -> observe
```

`settings.loop.turn_cap` (default 50) bounds the loop, and each run returns a terminal
`AgentLoopResult`. The runtime infrastructure is injected through
`SessionRuntime` and `BrowserHarness` in `src/harness/` and bundled for the
engine by `EngineResources.from_harness`. The engine owns reasoning, routing,
execution, and observation; there is no compiled graph.

Tool servers (Playwright MCP by default) are entries in `mcp_servers`, run by the
universal MCP Manager in `src/mcp/` and exposed to the engine through
`MCPToolSource`; stateless `ToolCallNormalizer`s adapt names and arguments. Tests
use `tests/mcp_fixtures/fake_server.py` without external services.

`SessionRuntime` owns the long-lived process lifecycle through `SessionContext`.
All tasks in one interactive session share a session identity derived from
`SessionContext.session_id` (carried as `TaskRequest.session_id`); each task
still receives a `TaskRecord.task_id` for persisted history and message
attribution, and `goal_id == task_id`. `SessionRuntime` carries session-useful
context into the next invocation and resets task-local fields at the task
boundary.

## Documentation

Start with [docs/README.md](docs/README.md). The most useful sections are:

- [Architecture Overview](docs/architecture/overview.md)
- [Development Setup](docs/development/setup.md)
- [Diagrams](docs/diagrams/index.md)
- [Architecture Decisions](docs/decisions/index.md)
- [Glossary](docs/glossary.md)
